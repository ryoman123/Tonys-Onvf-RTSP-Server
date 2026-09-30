#!/usr/bin/env python3
"""Migrate onvif-virtual-camera YAML into the unified Tony JSON configuration.

This is intentionally a one-way, offline conversion. It does not modify the
source file and it never writes credentials anywhere except the requested local
output file.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
import uuid
from pathlib import Path
from urllib.parse import quote, urlparse

try:
    import yaml
except ImportError as exc:
    raise SystemExit("PyYAML is required: python -m pip install pyyaml") from exc


def resolve_secret(value, label):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and set(value) == {"env"}:
        name = str(value["env"]).strip()
        resolved = os.environ.get(name)
        if not resolved:
            raise ValueError(
                f"{label} references environment variable {name!r}, which is not set"
            )
        return resolved
    raise ValueError(f"{label} must be a string or {{env: VARIABLE}} reference")


def normalize_encoding(value):
    text = str(value or "H264").strip().upper().replace(".", "")
    if text in {"H265", "HEVC"}:
        return "H265"
    if text in {"H264", "AVC"}:
        return "H264"
    raise ValueError(f"unsupported video encoding {value!r}")


def legacy_uuid_from_mac(mac):
    """Reproduce the existing bridge's WS-Discovery EndpointReference UUID."""
    compact = "".join(ch for ch in str(mac).lower() if ch in "0123456789abcdef")
    if len(compact) != 12:
        raise ValueError(f"invalid MAC address {mac!r}")
    padded = (compact + ("0" * 32))[:32]
    return str(uuid.UUID(
        f"{padded[:8]}-{padded[8:12]}-{padded[12:16]}-"
        f"{padded[16:20]}-{padded[20:32]}"
    ))


def parse_ip(value):
    text = str(value or "dhcp").strip()
    if text.lower() == "dhcp":
        return {
            "ipMode": "dhcp",
            "staticIp": "",
            "netmask": "24",
            "gateway": "",
        }

    interface = ipaddress.ip_interface(text)
    if interface.version != 4:
        raise ValueError("the unified field profile currently expects IPv4 camera VNICs")
    return {
        "ipMode": "static",
        "staticIp": str(interface.ip),
        "netmask": str(interface.network.prefixlen),
        "gateway": "",
    }


def stream_values(camera, key, defaults):
    raw = camera.get(key)
    raw = raw if isinstance(raw, dict) else {}
    return {
        "encoding": normalize_encoding(raw.get("encoding", defaults["encoding"])),
        "width": int(raw.get("width", defaults["width"])),
        "height": int(raw.get("height", defaults["height"])),
        "framerate": int(raw.get("framerate", defaults["framerate"])),
    }


def build_rtsp_url(source, path, username, password):
    auth = ""
    if username:
        auth = quote(username, safe="")
        if password:
            auth += ":" + quote(password, safe="")
        auth += "@"
    clean_path = str(path or "").strip()
    if not clean_path.startswith("/"):
        clean_path = "/" + clean_path
    return (
        f"rtsp://{auth}{source['hostname']}:{int(source.get('rtsp_port', 554))}"
        f"{clean_path}"
    )


def migrate(source_config, *, parent_interface, onvif_username, onvif_password,
            first_onvif_port, preserve_port_80):
    host_sources = {}
    for raw in source_config.get("host_sources") or []:
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ValueError("every host_source requires a name")
        auth = raw.get("auth") if isinstance(raw.get("auth"), dict) else {}
        host_sources[name] = {
            "name": name,
            "hostname": str(raw.get("hostname") or "").strip(),
            "rtsp_port": int(raw.get("rtsp_port", 554)),
            "http_port": int(raw.get("http_port", 80)),
            "username": resolve_secret(auth.get("username"), f"{name}.username"),
            "password": resolve_secret(auth.get("password"), f"{name}.password"),
        }

    cameras = []
    for index, raw in enumerate(source_config.get("virtual_cameras") or [], start=1):
        name = str(raw.get("name") or "").strip()
        source_name = str(raw.get("host_source") or "").strip()
        source = host_sources.get(source_name)
        if not name or not source:
            raise ValueError(
                f"virtual camera {index} requires a name and valid host_source"
            )

        mac = str(raw.get("mac") or "").strip().lower()
        network = parse_ip(raw.get("ip", "dhcp"))
        main = stream_values(raw, "stream_hq", {
            "encoding": "H264", "width": 1920, "height": 1080, "framerate": 15,
        })
        sub = stream_values(raw, "stream_lq", {
            "encoding": "H264", "width": 640, "height": 360, "framerate": 10,
        })

        manufacturer = str(raw.get("manufacturer") or "VirtualCam").strip()
        model = str(raw.get("model") or name).strip()
        serial = str(
            raw.get("serial_number")
            or mac.replace(":", "").upper()
        ).strip()
        hardware = str(
            raw.get("hardware_id")
            or "-".join(part for part in (model, serial) if part)
            or serial
        ).strip()
        firmware = str(raw.get("firmware_version") or "1.0").strip()

        camera_uuid = str(raw.get("uuid") or legacy_uuid_from_mac(mac))
        token_suffix = re.sub(r'[^a-z0-9]+', '_', serial.lower()).strip('_') or 'camera'
        onvif_port = 80 if preserve_port_80 else first_onvif_port + index - 1

        path_name = "".join(
            ch for ch in name.lower().replace(" ", "_").replace("-", "_")
            if ch.isalnum() or ch == "_"
        ) or f"camera{index}"

        cameras.append({
            "id": index,
            "uuid": camera_uuid,
            "name": name,
            "identity": {
                "manufacturer": manufacturer,
                "model": model,
                "firmwareVersion": firmware,
                "serialNumber": serial,
                "hardwareId": hardware,
                "location": source["hostname"] or "virtual",
                "discoveryName": f"{manufacturer} {model}".strip(),
            },
            "mainStreamUrl": build_rtsp_url(
                source, raw.get("rtsp_path_hq"), source["username"], source["password"]
            ),
            "subStreamUrl": build_rtsp_url(
                source, raw.get("rtsp_path_lq"), source["username"], source["password"]
            ),
            "rtspPort": 8554,
            "onvifPort": onvif_port,
            "pathName": path_name,
            "mediaTokens": {
                "mainProfile": f"profile_hq_{token_suffix}",
                "subProfile": f"profile_lq_{token_suffix}",
                "mainEncoder": f"video_encoder_hq_{token_suffix}",
                "subEncoder": f"video_encoder_lq_{token_suffix}",
            },
            "username": source["username"],
            "password": source["password"],
            "autoStart": True,
            "mainWidth": main["width"],
            "mainHeight": main["height"],
            "subWidth": sub["width"],
            "subHeight": sub["height"],
            "mainFramerate": main["framerate"],
            "subFramerate": sub["framerate"],
            "mainEncoding": main["encoding"],
            "subEncoding": sub["encoding"],
            "onvifUsername": onvif_username if onvif_username is not None else source["username"],
            "onvifPassword": onvif_password if onvif_password is not None else source["password"],
            "transcodeSub": False,
            "transcodeMain": False,
            "disableSubstream": False,
            "useMainAsSubstream": False,
            "enableAudio": False,
            "transcodeMainAudio": False,
            "transcodeSubAudio": False,
            "useVirtualNic": True,
            "vnicKeepalive": bool(
                (source_config.get("runtime") or {})
                .get("macvlan_keepalive", {})
                .get("enabled", False)
            ),
            "parentInterface": parent_interface,
            "nicMac": mac,
            **network,
            "debugMode": bool(
                (source_config.get("runtime") or {}).get("enable_debug_logs", False)
            ),
            # Prefer recorder-native/Frigate producers in the unified build;
            # avoid consuming scarce physical-camera PullPoint slots by default.
            "enableEventForwarding": False,
            "physicalOnvifPort": source["http_port"],
            "onvifForwardingUsername": source["username"],
            "onvifForwardingPassword": source["password"],
            "eventSource": "onvif",
            "aiTargets": ["person", "vehicle", "animal", "package"],
            "aiModel": "yolov8n.pt",
            "aiMotionDetectionEnabled": False,
            "sendSmartOnvifTopics": True,
        })

    analytics = source_config.get("analytics")
    analytics = analytics if isinstance(analytics, dict) else {}
    external = {
        "frigate": {
            "enabled": False,
            "host": "",
            "port": 1883,
            "username": "",
            "password": "",
            "topicPrefix": "frigate",
            "clientId": "tonys-onvif-unified",
            "keepaliveSeconds": 30,
            "cameraMap": {},
            "autoMap": True,
        },
        "recorders": [],
    }

    frigate = analytics.get("frigate")
    if isinstance(frigate, dict):
        broker = str(frigate.get("broker") or "").strip()
        parsed = urlparse(broker) if broker else None
        camera_map = frigate.get("camera_map")
        external["frigate"] = {
            "enabled": bool(frigate.get("enabled", False)),
            "host": parsed.hostname if parsed else "",
            "port": int(parsed.port or 1883) if parsed else 1883,
            "username": resolve_secret(frigate.get("username"), "frigate.username"),
            "password": resolve_secret(frigate.get("password"), "frigate.password"),
            "topicPrefix": str(frigate.get("topic_prefix") or "frigate"),
            "clientId": str(frigate.get("client_id") or "tonys-onvif-unified"),
            "keepaliveSeconds": int(frigate.get("keepalive_seconds", 30)),
            "cameraMap": {} if camera_map == "auto" else dict(camera_map or {}),
            "autoMap": camera_map == "auto" or not camera_map,
        }

    for raw in analytics.get("recorders") or []:
        if not isinstance(raw, dict):
            continue
        source = host_sources.get(str(raw.get("host_source") or "").strip())
        if not source:
            continue
        external["recorders"].append({
            "name": str(raw.get("name") or "recorder"),
            "enabled": bool(raw.get("enabled", False)),
            "host": source["hostname"],
            "port": source["http_port"],
            "protocol": str(raw.get("protocol") or "http"),
            "path": str(
                raw.get("path")
                or "/cgi-bin/eventManager.cgi?action=attach&codes=[All]&heartbeat=5"
            ),
            "username": source["username"],
            "password": source["password"],
            "source": str(raw.get("source") or raw.get("name") or "dahua"),
            "reconnectSeconds": max(
                1, int(raw.get("reconnect_period_ms", 5000)) // 1000
            ),
            "connectTimeoutSeconds": max(
                1, int(raw.get("connect_timeout_ms", 10000)) // 1000
            ),
            "inactivityTimeoutSeconds": max(
                1, int(raw.get("inactivity_timeout_ms", 20000)) // 1000
            ),
            "tlsVerify": bool(raw.get("tls_reject_unauthorized", True)),
            "channelMap": dict(raw.get("channel_map") or {}),
        })

    return {
        "cameras": cameras,
        "next_id": len(cameras) + 1,
        "next_onvif_port": first_onvif_port + len(cameras),
        "settings": {
            "serverIp": "localhost",
            "globalUsername": onvif_username if onvif_username is not None else "admin",
            "globalPassword": onvif_password if onvif_password is not None else "admin",
            "rtspAuthEnabled": False,
            "rtspPort": 8554,
            "webPort": 5552,
            "autoBoot": False,
            "openBrowser": False,
            "theme": "dracula",
            "gridColumns": 3,
            "watchdogEnabled": False,
            "debugMode": bool(
                (source_config.get("runtime") or {}).get("enable_debug_logs", False)
            ),
        },
        "gridFusion": {"layouts": [], "looks": []},
        "notifications": {"enabled_events": [], "providers": {}},
        "protectListener": {
            "monitorEnabled": False,
            "monitorIntervalMinutes": 30,
            "nvrs": [],
        },
        "externalAnalytics": external,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("source", help="Existing onvif-virtual-camera YAML config")
    parser.add_argument(
        "--output",
        default="camera_config.unified.json",
        help="Destination Tony camera_config.json",
    )
    parser.add_argument(
        "--parent-interface",
        default="ens19",
        help="Parent interface used for virtual camera NICs",
    )
    parser.add_argument("--onvif-username", help="Override all cameras; otherwise preserve each recorder username")
    parser.add_argument("--onvif-password", help="Override all cameras; otherwise preserve each recorder password")
    parser.add_argument("--first-onvif-port", type=int, default=8001)
    parser.add_argument(
        "--preserve-port-80",
        action="store_true",
        help="Use ONVIF HTTP port 80 on every distinct VNIC IP",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the output file if it exists",
    )
    args = parser.parse_args(argv)

    source_path = Path(args.source)
    output_path = Path(args.output)
    if output_path.exists() and not args.force:
        raise SystemExit(
            f"Refusing to overwrite {output_path}; use --force if intended"
        )

    raw = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit("source YAML must contain an object")

    migrated = migrate(
        raw,
        parent_interface=args.parent_interface,
        onvif_username=args.onvif_username,
        onvif_password=args.onvif_password,
        first_onvif_port=args.first_onvif_port,
        preserve_port_80=args.preserve_port_80,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(migrated, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(output_path, 0o600)
    except OSError:
        pass

    print(
        f"Wrote {len(migrated['cameras'])} cameras to {output_path} "
        f"(mode 0600 where supported)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
