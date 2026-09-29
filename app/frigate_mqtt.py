"""Frigate MQTT producer for the unified analytics state engine."""

from __future__ import annotations

import threading
from copy import deepcopy

from .analytics_events import (
    frigate_event_to_analytics,
    frigate_motion_to_analytics,
)


DEFAULT_FRIGATE_CONFIG = {
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
}


def normalize_frigate_config(config=None):
    merged = deepcopy(DEFAULT_FRIGATE_CONFIG)
    if isinstance(config, dict):
        merged.update(config)

    merged["enabled"] = bool(merged.get("enabled", False))
    merged["host"] = str(merged.get("host") or "").strip()
    try:
        merged["port"] = int(merged.get("port", 1883))
    except (TypeError, ValueError):
        merged["port"] = 1883
    merged["port"] = min(max(1, merged["port"]), 65535)
    merged["username"] = str(merged.get("username") or "")
    merged["password"] = str(merged.get("password") or "")
    merged["topicPrefix"] = str(
        merged.get("topicPrefix") or "frigate"
    ).strip().strip("/") or "frigate"
    merged["clientId"] = str(
        merged.get("clientId") or "tonys-onvif-unified"
    ).strip() or "tonys-onvif-unified"
    try:
        merged["keepaliveSeconds"] = int(merged.get("keepaliveSeconds", 30))
    except (TypeError, ValueError):
        merged["keepaliveSeconds"] = 30
    merged["keepaliveSeconds"] = min(max(5, merged["keepaliveSeconds"]), 3600)
    merged["cameraMap"] = (
        dict(merged.get("cameraMap") or {})
        if isinstance(merged.get("cameraMap"), dict)
        else {}
    )
    merged["autoMap"] = bool(merged.get("autoMap", True))
    return merged


class FrigateMqttRuntime:
    def __init__(self, manager, config=None, mqtt_module=None):
        self.manager = manager
        self.config = normalize_frigate_config(config)
        self._mqtt_module = mqtt_module
        self.client = None
        self._lock = threading.RLock()
        self.started = False
        self.state = "stopped"
        self.available = None
        self.connected_at = None
        self.last_message_at = None
        self.messages_received = 0
        self.events_dispatched = 0
        self.dropped_messages = 0
        self.errors = 0
        self.last_error = None
        self.unmapped_cameras = set()

    @property
    def topic_prefix(self):
        return self.config["topicPrefix"]

    def topics(self):
        prefix = self.topic_prefix
        return [
            f"{prefix}/available",
            f"{prefix}/events",
            f"{prefix}/+/motion",
        ]

    def _load_mqtt(self):
        if self._mqtt_module is not None:
            return self._mqtt_module
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:
            raise RuntimeError(
                "Frigate MQTT is enabled but paho-mqtt is not installed"
            ) from exc
        self._mqtt_module = mqtt
        return mqtt

    def _record_error(self, error):
        self.errors += 1
        self.last_error = str(error)
        print(f"  [Frigate MQTT] {error}")

    def _resolve_camera(self, frigate_name):
        mapping = self.config.get("cameraMap") or {}
        target = mapping.get(str(frigate_name))
        if target is not None:
            return self.manager.resolve_camera_reference(target)

        if self.config.get("autoMap", True):
            return self.manager.resolve_camera_reference(str(frigate_name))
        return None

    def _clear_source(self):
        for camera in self.manager.cameras:
            try:
                camera.clear_analytics_source("frigate")
            except Exception as exc:
                self._record_error(
                    f"failed clearing Frigate state for {camera.name}: {exc}"
                )

    def _dispatch(self, analytics):
        if not analytics:
            return None

        camera = self._resolve_camera(analytics["camera"])
        if not camera:
            self.unmapped_cameras.add(str(analytics["camera"]))
            self.dropped_messages += 1
            return None

        if camera.status != "running" or not camera.onvif_service:
            self.dropped_messages += 1
            return None

        published = camera.publish_analytics_event({
            **analytics,
            "camera": camera.name,
        })
        if published:
            self.events_dispatched += 1
        return published

    def route_message(self, topic, payload):
        prefix = self.topic_prefix
        available_topic = f"{prefix}/available"
        events_topic = f"{prefix}/events"

        if isinstance(payload, bytes):
            text = payload.decode("utf-8", errors="replace")
        else:
            text = str(payload)

        if topic == available_topic:
            state = text.strip().lower()
            self.available = True if state == "online" else False if state == "offline" else None
            if self.available is False:
                self._clear_source()
            return None

        if topic == events_topic:
            return self._dispatch(frigate_event_to_analytics(text))

        topic_prefix = f"{prefix}/"
        suffix = "/motion"
        if topic.startswith(topic_prefix) and topic.endswith(suffix):
            camera_name = topic[len(topic_prefix):-len(suffix)]
            if not camera_name or "/" in camera_name:
                self.dropped_messages += 1
                return None
            return self._dispatch(
                frigate_motion_to_analytics(camera_name, text)
            )

        return None

    # Paho callback API v1 signatures intentionally accept trailing optional
    # args so this remains compatible with paho-mqtt 1.x and 2.x.
    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if int(rc) != 0:
            self.state = "degraded"
            self._record_error(f"connect returned rc={rc}")
            return

        self.state = "connected"
        from datetime import datetime, timezone
        self.connected_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        for topic in self.topics():
            client.subscribe(topic, qos=0)
        print(
            "  [Frigate MQTT] Connected and subscribed: "
            + ", ".join(self.topics())
        )

    def _on_disconnect(self, client, userdata, rc, properties=None):
        if self.started:
            self.state = "disconnected"
            self._clear_source()

    def _on_message(self, client, userdata, message):
        from datetime import datetime, timezone
        self.messages_received += 1
        self.last_message_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            result = self.route_message(message.topic, message.payload)
            if result is None and (
                message.topic.endswith("/events")
                or message.topic.endswith("/motion")
            ):
                # A valid producer event can be suppressed by aggregation, so
                # only count it as dropped when parsing/routing has already done so.
                pass
        except Exception as exc:
            self.dropped_messages += 1
            self._record_error(exc)

    def start(self):
        with self._lock:
            if self.started:
                return
            if not self.config.get("enabled"):
                self.state = "disabled"
                return
            if not self.config.get("host"):
                raise RuntimeError("Frigate MQTT host is required when enabled")

            mqtt = self._load_mqtt()
            client = mqtt.Client(
                client_id=self.config["clientId"],
                protocol=mqtt.MQTTv311,
            )
            if self.config.get("username"):
                client.username_pw_set(
                    self.config["username"],
                    self.config.get("password") or None,
                )

            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect
            client.on_message = self._on_message

            self.client = client
            self.started = True
            self.state = "connecting"
            try:
                client.connect_async(
                    self.config["host"],
                    self.config["port"],
                    keepalive=self.config["keepaliveSeconds"],
                )
                client.loop_start()
            except Exception:
                self.started = False
                self.client = None
                self.state = "stopped"
                raise

    def stop(self):
        with self._lock:
            if not self.started:
                if self.state != "disabled":
                    self.state = "stopped"
                return

            client = self.client
            self.client = None
            self.started = False

        self._clear_source()

        if client is not None:
            try:
                client.disconnect()
            except Exception as exc:
                self._record_error(exc)
            try:
                client.loop_stop()
            except Exception as exc:
                self._record_error(exc)

        self.state = "stopped"

    def health(self):
        safe_config = {
            key: value
            for key, value in self.config.items()
            if key != "password"
        }
        return {
            "state": self.state,
            "available": self.available,
            "started": self.started,
            "connectedAt": self.connected_at,
            "lastMessageAt": self.last_message_at,
            "messagesReceived": self.messages_received,
            "eventsDispatched": self.events_dispatched,
            "droppedMessages": self.dropped_messages,
            "errors": self.errors,
            "lastError": self.last_error,
            "unmappedCameras": sorted(self.unmapped_cameras),
            "config": safe_config,
        }
