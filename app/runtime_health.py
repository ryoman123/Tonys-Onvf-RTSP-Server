"""Production readiness and acceptance inventory for the unified runtime."""

from __future__ import annotations

from datetime import datetime, timezone


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _thread_alive(thread):
    try:
        return bool(thread and thread.is_alive())
    except Exception:
        return False


def _mediamtx_running(manager):
    process = getattr(getattr(manager, "mediamtx", None), "process", None)
    if process is None:
        return False
    try:
        return process.poll() is None
    except Exception:
        return False


def camera_readiness(camera):
    onvif = getattr(camera, "onvif_service", None)
    discovery_thread = getattr(onvif, "_discovery_thread", None) if onvif else None
    event_health = onvif.event_health() if onvif else None
    identity = camera.get_identity_manifest()
    stream_probe = getattr(camera, "stream_probe", {}) or {}
    stream_mismatch = bool(stream_probe.get("mismatch", False))

    running = getattr(camera, "status", None) == "running"
    http_ready = bool(
        running
        and getattr(camera, "server", None)
        and _thread_alive(getattr(camera, "flask_thread", None))
    )
    discovery_ready = bool(running and onvif and _thread_alive(discovery_thread))
    event_ready = bool(running and onvif)
    effective_ip = camera.get_effective_ip()

    issues = []
    if not running:
        issues.append("camera-not-running")
    if running and not http_ready:
        issues.append("onvif-http-not-ready")
    if running and not discovery_ready:
        issues.append("ws-discovery-not-ready")
    if running and not event_ready:
        issues.append("event-service-not-ready")
    if not effective_ip:
        issues.append("missing-effective-ip")
    if stream_mismatch:
        issues.append("stream-metadata-mismatch")

    return {
        "id": camera.id,
        "name": camera.name,
        "pathName": camera.path_name,
        "autoStart": bool(getattr(camera, "auto_start", False)),
        "status": camera.status,
        "ready": len(issues) == 0,
        "issues": issues,
        "effectiveIp": effective_ip,
        "onvifPort": camera.onvif_port,
        "rtspPort": camera.rtsp_port,
        "identity": identity,
        "streams": {
            "main": {
                "width": camera.main_width,
                "height": camera.main_height,
                "framerate": camera.main_framerate,
            },
            "sub": None if getattr(camera, "disable_substream", False) else {
                "width": camera.sub_width,
                "height": camera.sub_height,
                "framerate": camera.sub_framerate,
            },
            "probe": stream_probe,
        },
        "services": {
            "http": http_ready,
            "discovery": discovery_ready,
            "events": event_ready,
        },
        "events": event_health,
        "analytics": getattr(camera, "analytics_state", None).health()
        if getattr(camera, "analytics_state", None)
        else None,
    }


def build_readiness(manager, *, boot_id=None):
    cameras = [camera_readiness(camera) for camera in manager.cameras]
    required = [item for item in cameras if item["autoStart"]]
    # If no camera is marked Auto Start, report inventory without arbitrarily
    # declaring deliberately stopped cameras a deployment failure.
    required_for_core = required if required else [
        item for item in cameras if item["status"] == "running"
    ]

    identity_fields = ("uuid", "mac", "serialNumber", "hardwareId", "fingerprint")
    duplicate_identity = []
    for field in identity_fields:
        seen = {}
        for item in cameras:
            value = item["identity"].get(field)
            key = str(value).lower() if value is not None else ""
            if not key:
                duplicate_identity.append({
                    "field": field,
                    "value": value,
                    "cameras": [item["name"]],
                    "reason": "missing",
                })
                continue
            if key in seen:
                duplicate_identity.append({
                    "field": field,
                    "value": value,
                    "cameras": [seen[key], item["name"]],
                    "reason": "duplicate",
                })
            else:
                seen[key] = item["name"]

    mediamtx_running = _mediamtx_running(manager)
    core_ready = (
        mediamtx_running
        and all(item["ready"] for item in required_for_core)
        and not duplicate_identity
    )

    external = manager.external_analytics_health()
    frigate_cfg = getattr(manager, "external_analytics_config", {}).get("frigate", {})
    frigate_required = bool(frigate_cfg.get("enabled"))
    frigate_health = external.get("frigate") or {}
    frigate_ready = (
        not frigate_required
        or (
            frigate_health.get("state") == "connected"
            and frigate_health.get("available") is not False
        )
    )

    recorder_configs = getattr(manager, "external_analytics_config", {}).get("recorders", [])
    enabled_recorders = {
        str(item.get("name"))
        for item in recorder_configs
        if isinstance(item, dict) and item.get("enabled")
    }
    recorder_health = {
        str(item.get("name")): item
        for item in external.get("recorders", [])
        if isinstance(item, dict)
    }
    recorder_ready = all(
        recorder_health.get(name, {}).get("state") == "connected"
        for name in enabled_recorders
    )

    return {
        "status": "healthy" if core_ready else "degraded",
        "ready": core_ready,
        "timestamp": _utc_now(),
        "bootId": boot_id,
        "mediaMTX": {
            "running": mediamtx_running,
        },
        "cameras": {
            "total": len(cameras),
            "autoStart": len(required),
            "requiredForCore": len(required_for_core),
            "ready": sum(1 for item in cameras if item["ready"]),
            "items": cameras,
        },
        "identity": {
            "valid": not duplicate_identity,
            "conflicts": duplicate_identity,
        },
        "analytics": {
            **external,
            "ready": bool(frigate_ready and recorder_ready),
            "frigateRequired": frigate_required,
            "enabledRecorders": sorted(enabled_recorders),
        },
    }


def evaluate_acceptance(
    status,
    *,
    expected_cameras=None,
    require_pullpoint_subscribers=False,
    require_analytics=False,
    expected_manifest=None,
):
    failures = []
    camera_block = status.get("cameras") or {}
    cameras = camera_block.get("items") or []

    if not status.get("ready"):
        failures.append("core runtime is not ready")

    if expected_cameras is not None and len(cameras) != expected_cameras:
        failures.append(
            f"expected {expected_cameras} cameras, found {len(cameras)}"
        )

    if camera_block.get("ready") != len(cameras):
        failures.append(
            f"only {camera_block.get('ready', 0)} of {len(cameras)} cameras are ready"
        )

    if not (status.get("identity") or {}).get("valid", False):
        failures.append("camera identity inventory contains conflicts")

    seen_names = set()
    by_name = {}
    for camera in cameras:
        name = camera.get("name")
        if name in seen_names:
            failures.append(f"duplicate camera name: {name}")
        seen_names.add(name)
        by_name[name] = camera

        if require_pullpoint_subscribers:
            subscriptions = (camera.get("events") or {}).get("subscriptions", 0)
            if subscriptions < 1:
                failures.append(f"{name}: no active PullPoint subscriber")

        if (camera.get("streams") or {}).get("probe", {}).get("mismatch"):
            failures.append(f"{name}: source stream metadata mismatch")

    if require_analytics and not (status.get("analytics") or {}).get("ready"):
        failures.append("required external analytics producer is not ready")

    expected_items = []
    if expected_manifest:
        expected_items = (
            expected_manifest.get("cameras", [])
            if isinstance(expected_manifest, dict)
            else expected_manifest
        )

    if expected_items and len(expected_items) != len(cameras):
        failures.append(
            f"identity manifest contains {len(expected_items)} cameras, found {len(cameras)}"
        )

    for expected in expected_items:
        name = expected.get("name")
        actual = by_name.get(name)
        if not actual:
            failures.append(f"manifest camera '{name}' is missing")
            continue
        identity = actual.get("identity") or {}
        for field in ("uuid", "mac", "serialNumber", "hardwareId", "fingerprint"):
            expected_value = expected.get(field)
            if expected_value is None:
                continue
            if identity.get(field) != expected_value:
                failures.append(
                    f"{name}: {field} expected '{expected_value}', "
                    f"found '{identity.get(field)}'"
                )

    return {
        "passed": not failures,
        "failures": failures,
        "cameras": {
            "expected": expected_cameras,
            "found": len(cameras),
            "ready": camera_block.get("ready", 0),
        },
    }


def build_identity_manifest(status):
    cameras = (status.get("cameras") or {}).get("items") or []
    manifest = []
    for camera in cameras:
        identity = camera.get("identity") or {}
        manifest.append({
            "name": camera.get("name"),
            "uuid": identity.get("uuid"),
            "mac": identity.get("mac"),
            "serialNumber": identity.get("serialNumber"),
            "hardwareId": identity.get("hardwareId"),
            "fingerprint": identity.get("fingerprint"),
        })
    manifest.sort(key=lambda item: str(item.get("name") or "").lower())
    return {
        "generatedAt": status.get("timestamp") or _utc_now(),
        "cameras": manifest,
    }
