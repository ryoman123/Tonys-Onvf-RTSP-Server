"""Normalized analytics events and multi-producer state aggregation.

Local AI, physical-camera ONVIF forwarding, Frigate MQTT and recorder-native
events all converge here before a state change is exposed through the virtual
camera's PullPoint service.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone

from .event_engine import TOPICS


TYPE_TO_TOPIC = {
    "motion": (TOPICS["motion"], "IsMotion"),
    "person": (TOPICS["person"], "State"),
    "vehicle": (TOPICS["vehicle"], "State"),
    "animal": (TOPICS["animal"], "State"),
    "package": (TOPICS["package"], "State"),
}

LABEL_MAP = {
    "person": "person",
    "car": "vehicle",
    "motorcycle": "vehicle",
    "bicycle": "vehicle",
    "bus": "vehicle",
    "truck": "vehicle",
    "dog": "animal",
    "cat": "animal",
    "bird": "animal",
    "package": "package",
}


def normalize_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {
        "true", "1", "on", "active", "yes", "start", "started"
    }


def normalize_utc(value=None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    if isinstance(value, (int, float)):
        numeric = float(value)
        # Frigate timestamps are seconds; tolerate millisecond epoch input too.
        if numeric > 10_000_000_000:
            numeric /= 1000.0
        return datetime.fromtimestamp(numeric, timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )

    text = str(value).strip()
    if not text:
        return normalize_utc()

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        return normalize_utc()


def topic_to_type(topic) -> str | None:
    normalized = str(topic or "")
    if ":" in normalized.split("/", 1)[0]:
        normalized = normalized.split(":", 1)[1]
    for event_type, (known_topic, _) in TYPE_TO_TOPIC.items():
        if normalized == known_topic:
            return event_type

    lower = normalized.lower()
    if "human" in lower or "person" in lower or "pedestrian" in lower:
        return "person"
    if any(token in lower for token in ("vehicle", "car", "truck", "motorcycle")):
        return "vehicle"
    if any(token in lower for token in ("animal", "dog", "cat", "bird", "pet")):
        return "animal"
    if any(token in lower for token in ("package", "parcel", "delivery")):
        return "package"
    if any(token in lower for token in ("motion", "cellmotiondetector")):
        return "motion"
    return None


def normalize_analytics_event(event) -> dict:
    if not isinstance(event, dict):
        raise ValueError("analytics event must be a dict")

    source = str(event.get("source") or "").strip().lower()
    camera = event.get("camera")
    event_type = str(event.get("type") or "").strip().lower()

    if not source:
        raise ValueError("analytics event source is required")
    if camera is None or str(camera).strip() == "":
        raise ValueError("analytics event camera is required")
    if event_type not in TYPE_TO_TOPIC:
        raise ValueError(f"unsupported analytics event type: {event_type!r}")

    confidence = event.get("confidence")
    if confidence is not None:
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = None

    return {
        "source": source,
        "camera": camera,
        "type": event_type,
        "active": normalize_bool(event.get("active")),
        "utcTime": normalize_utc(event.get("utcTime") or event.get("timestamp")),
        "confidence": confidence,
        "objectId": (
            str(event["objectId"]).strip()
            if event.get("objectId") is not None and str(event["objectId"]).strip()
            else None
        ),
        "zones": list(event.get("zones") or []),
        "metadata": dict(event.get("metadata") or {}),
    }


def to_onvif_event(event) -> dict:
    normalized = normalize_analytics_event(event)
    topic, data_name = TYPE_TO_TOPIC[normalized["type"]]
    return {
        "topic": topic,
        "value": normalized["active"],
        "data_name": data_name,
        "timestamp": normalized["utcTime"],
        "propertyOperation": "Changed",
        # Retain one aggregate property per topic. Producer/object provenance is
        # diagnostic metadata, not part of the ONVIF retained-property key.
        "source": {},
        "analytics": normalized,
    }


class AnalyticsStateAggregator:
    """OR-reduce independent producers into one stable ONVIF property state."""

    def __init__(self):
        self._contributors: dict[str, set[str]] = {}
        self._last_event_by_type: dict[str, dict] = {}
        self._lock = threading.RLock()
        self.transitions = 0
        self.suppressed = 0
        self.last_transition_at = None

    @staticmethod
    def _contributor(event):
        object_id = event.get("objectId")
        suffix = f"object:{object_id}" if object_id else "state"
        return f"{event['source']}|{suffix}"

    def apply(self, event):
        normalized = normalize_analytics_event(event)
        event_type = normalized["type"]
        contributor = self._contributor(normalized)

        with self._lock:
            active = self._contributors.setdefault(event_type, set())
            was_active = bool(active)

            if normalized["active"]:
                active.add(contributor)
            else:
                active.discard(contributor)

            is_active = bool(active)
            if not active:
                self._contributors.pop(event_type, None)

            self._last_event_by_type[event_type] = dict(normalized)

            if was_active == is_active:
                self.suppressed += 1
                return None

            self.transitions += 1
            self.last_transition_at = normalize_utc()
            return {
                **normalized,
                "source": "aggregate",
                "active": is_active,
                "objectId": None,
                "metadata": {
                    **normalized.get("metadata", {}),
                    "contributors": sorted(active),
                    "triggerSource": normalized["source"],
                },
            }

    def clear_source(self, source: str, *, camera=None):
        """Remove all contributors from one producer and return aggregate clears."""
        source_prefix = str(source or "").strip().lower() + "|"
        if source_prefix == "|":
            return []

        transitions = []
        with self._lock:
            for event_type, contributors in list(self._contributors.items()):
                before = bool(contributors)
                contributors.difference_update(
                    item for item in tuple(contributors)
                    if item.startswith(source_prefix)
                )
                after = bool(contributors)

                if not contributors:
                    self._contributors.pop(event_type, None)

                if before and not after:
                    self.transitions += 1
                    self.last_transition_at = normalize_utc()
                    previous = self._last_event_by_type.get(event_type, {})
                    transitions.append({
                        "source": "aggregate",
                        "camera": camera if camera is not None else previous.get("camera"),
                        "type": event_type,
                        "active": False,
                        "utcTime": normalize_utc(),
                        "confidence": None,
                        "objectId": None,
                        "zones": [],
                        "metadata": {
                            "contributors": [],
                            "triggerSource": str(source).lower(),
                            "reason": "source-cleared",
                        },
                    })
        return transitions

    def health(self):
        with self._lock:
            return {
                "active": {
                    event_type: sorted(contributors)
                    for event_type, contributors in self._contributors.items()
                },
                "transitions": self.transitions,
                "suppressed": self.suppressed,
                "lastTransitionAt": self.last_transition_at,
            }


def frigate_event_to_analytics(payload):
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise ValueError("Frigate event payload must be a JSON object")

    phase = str(payload.get("type") or "").lower()
    if phase not in {"new", "update", "end"}:
        return None

    record = payload.get("after") or payload.get("before")
    if not isinstance(record, dict):
        return None

    mapped_type = LABEL_MAP.get(str(record.get("label") or "").lower())
    if not mapped_type:
        return None

    snapshot = record.get("snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    score = record.get("top_score")
    if score is None:
        score = snapshot.get("score")

    event_time = (
        record.get("end_time") if phase == "end" else record.get("frame_time")
    )
    if event_time is None:
        event_time = record.get("start_time") or time.time()

    return normalize_analytics_event({
        "source": "frigate",
        "camera": record.get("camera"),
        "type": mapped_type,
        "active": phase != "end",
        "utcTime": event_time,
        "confidence": score,
        "objectId": record.get("id"),
        "zones": record.get("current_zones") or record.get("entered_zones") or [],
        "metadata": {
            "frigatePhase": phase,
            "box": snapshot.get("box") or record.get("box"),
        },
    })


def frigate_motion_to_analytics(camera, payload):
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    state = str(payload or "").strip().upper()
    if state not in {"ON", "OFF"}:
        return None
    return normalize_analytics_event({
        "source": "frigate",
        "camera": camera,
        "type": "motion",
        "active": state == "ON",
    })
