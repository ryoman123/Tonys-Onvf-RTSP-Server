"""Dahua/Lorex native recorder event producer.

Many Lorex NVRs expose Dahua-compatible eventManager.cgi streams. This module
keeps that integration independent of ONVIF subscription limits and feeds the
same multi-producer state engine used by local AI, physical ONVIF and Frigate.
"""

from __future__ import annotations

import json
import threading
import time
from copy import deepcopy
from datetime import datetime, timezone

import requests
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

from .analytics_events import normalize_analytics_event


EVENT_TYPE_MAP = {
    "VideoMotion": "motion",
    "SmartMotionHuman": "person",
    "SmartMotionVehicle": "vehicle",
}

DEFAULT_RECORDER_PATH = (
    "/cgi-bin/eventManager.cgi?action=attach&codes=[All]&heartbeat=5"
)
DEFAULT_RECORDER_CONFIG = {
    "name": "recorder",
    "enabled": False,
    "host": "",
    "port": 80,
    "protocol": "http",
    "path": DEFAULT_RECORDER_PATH,
    "username": "",
    "password": "",
    "source": "dahua",
    "reconnectSeconds": 5,
    "connectTimeoutSeconds": 10,
    "inactivityTimeoutSeconds": 20,
    "tlsVerify": True,
    "channelMap": {},
}


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_recorder_config(config=None):
    merged = deepcopy(DEFAULT_RECORDER_CONFIG)
    if isinstance(config, dict):
        merged.update(config)

    merged["name"] = str(merged.get("name") or "recorder").strip() or "recorder"
    merged["enabled"] = bool(merged.get("enabled", False))
    merged["host"] = str(merged.get("host") or "").strip()
    protocol = str(merged.get("protocol") or "http").lower().rstrip(":/")
    merged["protocol"] = protocol if protocol in {"http", "https"} else "http"

    try:
        merged["port"] = int(merged.get("port") or (443 if protocol == "https" else 80))
    except (TypeError, ValueError):
        merged["port"] = 443 if protocol == "https" else 80
    merged["port"] = min(max(1, merged["port"]), 65535)

    path = str(merged.get("path") or DEFAULT_RECORDER_PATH).strip()
    merged["path"] = path if path.startswith("/") else "/" + path
    merged["username"] = str(merged.get("username") or "")
    merged["password"] = str(merged.get("password") or "")
    merged["source"] = str(merged.get("source") or "dahua").strip().lower() or "dahua"

    for key, default, minimum, maximum in (
        ("reconnectSeconds", 5, 1, 3600),
        ("connectTimeoutSeconds", 10, 1, 300),
        ("inactivityTimeoutSeconds", 20, 1, 3600),
    ):
        try:
            merged[key] = int(merged.get(key, default))
        except (TypeError, ValueError):
            merged[key] = default
        merged[key] = min(max(minimum, merged[key]), maximum)

    merged["tlsVerify"] = bool(merged.get("tlsVerify", True))
    channel_map = merged.get("channelMap")
    merged["channelMap"] = dict(channel_map) if isinstance(channel_map, dict) else {}
    return merged


def parse_dahua_event_line(line):
    text = str(line or "").strip()
    if not text.startswith("Code="):
        return None

    fields = {}
    for part in text.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key:
            fields[key] = value

    event_type = EVENT_TYPE_MAP.get(fields.get("Code"))
    if not event_type:
        return None

    action = str(fields.get("action") or "").lower()
    if action not in {"start", "stop", "pulse"}:
        return None

    try:
        channel = int(fields.get("index"))
    except (TypeError, ValueError):
        return None
    if channel < 0:
        return None

    data = None
    if fields.get("data"):
        try:
            data = json.loads(fields["data"])
        except (TypeError, ValueError):
            data = None

    return {
        "channel": channel,
        "type": event_type,
        "active": action != "stop",
        "action": action,
        "data": data,
    }


class DahuaEventStreamParser:
    def __init__(self, callback):
        if not callable(callback):
            raise ValueError("Dahua event parser requires a callback")
        self.callback = callback
        self.buffer = ""

    def push(self, chunk):
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8", errors="replace")
        self.buffer += str(chunk)
        lines = self.buffer.splitlines(keepends=True)
        self.buffer = ""

        for item in lines:
            if item.endswith("\n") or item.endswith("\r"):
                event = parse_dahua_event_line(item.strip())
                if event:
                    self.callback(event)
            else:
                self.buffer += item

    def reset(self):
        self.buffer = ""


class RecorderEventRuntime:
    def __init__(self, manager, config=None, session_factory=None):
        self.manager = manager
        self.config = normalize_recorder_config(config)
        self.session_factory = session_factory or requests.Session
        self.session = None
        self.response = None
        self.thread = None
        self.stop_event = threading.Event()
        self.parser = DahuaEventStreamParser(self._handle_event)
        self.state = "stopped"
        self.started = False
        self.connected_at = None
        self.last_data_at = None
        self.last_event_at = None
        self.connections = 0
        self.events_received = 0
        self.events_dispatched = 0
        self.dropped_events = 0
        self.errors = 0
        self.auth_challenges = 0
        self.last_error = None
        self.unmapped_channels = set()
        self._lock = threading.RLock()

    @property
    def source(self):
        return self.config["source"]

    @property
    def url(self):
        return (
            f"{self.config['protocol']}://{self.config['host']}:"
            f"{self.config['port']}{self.config['path']}"
        )

    def _record_error(self, error):
        self.errors += 1
        self.last_error = str(error)
        print(f"  [Recorder {self.config['name']}] {error}")

    def _resolve_camera(self, channel):
        mapping = self.config.get("channelMap") or {}
        target = mapping.get(str(channel))
        if target is None:
            target = mapping.get(channel)
        return self.manager.resolve_camera_reference(target) if target is not None else None

    def _clear_source(self):
        for camera in self.manager.cameras:
            try:
                camera.clear_analytics_source(self.source)
            except Exception as exc:
                self._record_error(
                    f"failed clearing {self.source} state for {camera.name}: {exc}"
                )

    def _handle_event(self, raw):
        self.events_received += 1
        self.last_event_at = _utc_now()

        camera = self._resolve_camera(raw["channel"])
        if not camera:
            self.unmapped_channels.add(raw["channel"])
            self.dropped_events += 1
            return

        if camera.status != "running" or not camera.onvif_service:
            self.dropped_events += 1
            return

        object_id = None
        if raw["action"] == "pulse":
            object_id = f"pulse-{raw['channel']}-{time.time_ns()}"

        event = normalize_analytics_event({
            "source": self.source,
            "camera": camera.name,
            "type": raw["type"],
            "active": raw["active"],
            "objectId": object_id,
            "metadata": {
                "channel": raw["channel"],
                "action": raw["action"],
                "data": raw.get("data"),
                "recorder": self.config["name"],
            },
        })

        result = camera.publish_analytics_event(event)
        if result:
            self.events_dispatched += 1

        if raw["action"] == "pulse":
            cleared = camera.publish_analytics_event({
                **event,
                "active": False,
            })
            if cleared:
                self.events_dispatched += 1

    def _open_response(self, session):
        kwargs = {
            "stream": True,
            "timeout": (
                self.config["connectTimeoutSeconds"],
                self.config["inactivityTimeoutSeconds"],
            ),
            "verify": self.config["tlsVerify"],
            "headers": {
                "Accept": "*/*",
                "Connection": "keep-alive",
            },
        }

        username = self.config.get("username")
        password = self.config.get("password")
        if username:
            # Digest is the common Dahua/Lorex mode. Requests performs the
            # challenge automatically.
            kwargs["auth"] = HTTPDigestAuth(username, password)

        response = session.get(self.url, **kwargs)
        if response.status_code == 401 and username:
            challenge = response.headers.get("WWW-Authenticate", "")
            if challenge.lower().startswith("basic"):
                self.auth_challenges += 1
                response.close()
                kwargs["auth"] = HTTPBasicAuth(username, password)
                response = session.get(self.url, **kwargs)

        response.raise_for_status()
        return response

    def _run(self):
        while not self.stop_event.is_set():
            self.parser.reset()
            self.state = "connecting"
            session = self.session_factory()
            self.session = session

            try:
                response = self._open_response(session)
                self.response = response
                self.connections += 1
                self.connected_at = _utc_now()
                self.state = "connected"
                print(
                    f"  [Recorder {self.config['name']}] Connected to {self.url}"
                )

                for chunk in response.iter_content(chunk_size=4096):
                    if self.stop_event.is_set():
                        break
                    if not chunk:
                        continue
                    self.last_data_at = _utc_now()
                    self.parser.push(chunk)

            except Exception as exc:
                if not self.stop_event.is_set():
                    self._record_error(exc)
            finally:
                self.parser.reset()
                current_response = self.response
                self.response = None
                if current_response is not None:
                    try:
                        current_response.close()
                    except Exception:
                        pass
                try:
                    session.close()
                except Exception:
                    pass
                self.session = None

                if not self.stop_event.is_set():
                    self.state = "reconnecting"
                    self._clear_source()
                    self.stop_event.wait(self.config["reconnectSeconds"])

        self.state = "stopped"

    def start(self):
        with self._lock:
            if self.started:
                return
            if not self.config.get("enabled"):
                self.state = "disabled"
                return
            if not self.config.get("host"):
                raise RuntimeError(
                    f"Recorder {self.config['name']} host is required when enabled"
                )

            self.started = True
            self.stop_event.clear()
            self.thread = threading.Thread(
                target=self._run,
                daemon=True,
                name=f"recorder-events-{self.config['name']}",
            )
            self.thread.start()

    def stop(self):
        with self._lock:
            if not self.started:
                if self.state != "disabled":
                    self.state = "stopped"
                return
            self.started = False
            self.stop_event.set()
            response = self.response
            session = self.session
            thread = self.thread

        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        if session is not None:
            try:
                session.close()
            except Exception:
                pass

        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(2, self.config["connectTimeoutSeconds"] + 1))

        self.thread = None
        self.response = None
        self.session = None
        self.parser.reset()
        self._clear_source()
        self.state = "stopped"

    def health(self):
        safe_config = {
            key: value
            for key, value in self.config.items()
            if key != "password"
        }
        return {
            "name": self.config["name"],
            "source": self.source,
            "state": self.state,
            "started": self.started,
            "connectedAt": self.connected_at,
            "lastDataAt": self.last_data_at,
            "lastEventAt": self.last_event_at,
            "connections": self.connections,
            "eventsReceived": self.events_received,
            "eventsDispatched": self.events_dispatched,
            "droppedEvents": self.dropped_events,
            "errors": self.errors,
            "authChallenges": self.auth_challenges,
            "lastError": self.last_error,
            "unmappedChannels": sorted(self.unmapped_channels),
            "config": safe_config,
        }
