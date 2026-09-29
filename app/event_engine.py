"""Hardened ONVIF PullPoint event state for the unified Tony-based runtime.

This module ports the event-state semantics proven in ryoman123/onvif-virtual-camera
into Tony's Python application without changing the surrounding UI/MediaMTX model.

The engine deliberately keeps a subscriptions mapping whose values expose a
queue and client_ip attribute. That preserves compatibility with Tony's
existing diagnostics while event producers migrate to publish().
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from xml.sax.saxutils import escape


TOPICS = {
    "motion": "RuleEngine/CellMotionDetector/Motion",
    "person": "UserAlarm/IVA/HumanShapeDetect",
    "vehicle": "VehicleAlarm/IVB/VehicleDetect",
    "animal": "UserAlarm/IVA/AnimalDetect",
    "package": "UserAlarm/IVA/PackageDetect",
}

DEFAULT_TOPICS = tuple(TOPICS.values())
SMART_TOPICS = tuple(
    TOPICS[name] for name in ("person", "vehicle", "animal", "package")
)

CONCRETE_TOPIC_DIALECT = (
    "http://docs.oasis-open.org/wsn/t-1/TopicExpression/Concrete"
)
CONCRETE_SET_DIALECT = (
    "http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet"
)
ITEM_FILTER_DIALECT = (
    "http://www.onvif.org/ver10/tev/messageContentFilter/ItemFilter"
)

DEFAULT_SUBSCRIPTION_TTL_SECONDS = 600
MAX_SUBSCRIPTION_TTL_SECONDS = 24 * 60 * 60
MAX_PULL_TIMEOUT_SECONDS = 60
MAX_MESSAGE_LIMIT = 256
DEFAULT_MAX_QUEUE = 512
DEFAULT_MAX_PULLPOINTS = 32

_TOPIC_DEFINITIONS = {
    TOPICS["motion"]: {
        "source": (
            ("VideoSourceConfigurationToken", "tt:ReferenceToken"),
            ("VideoAnalyticsConfigurationToken", "tt:ReferenceToken"),
            ("Rule", "xs:string"),
        ),
        "data": (("IsMotion", "xs:boolean"),),
    },
    TOPICS["person"]: {
        "source": (("VideoSourceConfigurationToken", "tt:ReferenceToken"),),
        "data": (("State", "xs:boolean"),),
    },
    TOPICS["vehicle"]: {
        "source": (("VideoSourceConfigurationToken", "tt:ReferenceToken"),),
        "data": (("State", "xs:boolean"),),
    },
    TOPICS["animal"]: {
        "source": (("VideoSourceConfigurationToken", "tt:ReferenceToken"),),
        "data": (("State", "xs:boolean"),),
    },
    TOPICS["package"]: {
        "source": (("VideoSourceConfigurationToken", "tt:ReferenceToken"),),
        "data": (("State", "xs:boolean"),),
    },
}


class EventSubscriptionError(Exception):
    """Structured event-service error that maps cleanly to an ONVIF SOAP fault."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _parse_xml(body: str) -> ET.Element | None:
    if not body or not body.strip():
        return None
    try:
        return ET.fromstring(body)
    except ET.ParseError as exc:
        raise EventSubscriptionError("invalid-xml", f"invalid SOAP XML: {exc}") from exc


def extract_xml_text(body: str, local_name: str) -> str | None:
    root = _parse_xml(body)
    if root is None:
        return None
    for node in root.iter():
        if _local_name(node.tag) == local_name:
            text = (node.text or "").strip()
            return text or None
    return None


def parse_duration_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    match = re.fullmatch(
        r"P(?:(\d+(?:\.\d+)?)D)?"
        r"(?:T(?:(\d+(?:\.\d+)?)H)?"
        r"(?:(\d+(?:\.\d+)?)M)?"
        r"(?:(\d+(?:\.\d+)?)S)?)?",
        text,
    )
    if not match:
        return None

    days = float(match.group(1) or 0)
    hours = float(match.group(2) or 0)
    minutes = float(match.group(3) or 0)
    seconds = float(match.group(4) or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def resolve_termination_seconds(
    value: str | None,
    *,
    now: float | None = None,
    default_seconds: int = DEFAULT_SUBSCRIPTION_TTL_SECONDS,
) -> int:
    if value is None or not str(value).strip():
        return default_seconds

    now = time.time() if now is None else now
    duration = parse_duration_seconds(value)
    if duration is not None:
        if duration <= 0:
            raise EventSubscriptionError(
                "invalid-termination", "termination duration must be greater than zero"
            )
        return min(int(duration), MAX_SUBSCRIPTION_TTL_SECONDS)

    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EventSubscriptionError(
            "invalid-termination",
            "termination time must be an xs:duration or xs:dateTime",
        ) from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    ttl = int(parsed.timestamp() - now)
    if ttl <= 0:
        raise EventSubscriptionError(
            "invalid-termination", "termination time must be in the future"
        )
    return min(ttl, MAX_SUBSCRIPTION_TTL_SECONDS)


def parse_pull_timeout_seconds(body: str) -> float:
    raw = extract_xml_text(body, "Timeout")
    if raw is None:
        return 0.0
    duration = parse_duration_seconds(raw)
    if duration is None or duration < 0:
        raise EventSubscriptionError(
            "invalid-pull", "PullMessages Timeout must be an xs:duration"
        )
    return min(duration, MAX_PULL_TIMEOUT_SECONDS)


def parse_message_limit(body: str) -> int:
    raw = extract_xml_text(body, "MessageLimit")
    if raw is None:
        return 10
    try:
        limit = int(raw)
    except (TypeError, ValueError) as exc:
        raise EventSubscriptionError(
            "invalid-pull", "PullMessages MessageLimit must be a positive integer"
        ) from exc
    if limit <= 0:
        raise EventSubscriptionError(
            "invalid-pull", "PullMessages MessageLimit must be a positive integer"
        )
    return min(limit, MAX_MESSAGE_LIMIT)


def _normalize_topic_expression(value: str) -> str:
    return re.sub(r"^(?:tns1|tns):", "", value.strip())


def parse_topic_filter(body: str, known_topics=DEFAULT_TOPICS):
    """Return None for no filter, otherwise a set of selected leaf topics."""
    root = _parse_xml(body)
    if root is None:
        return None

    expressions: list[str] = []
    for node in root.iter():
        if _local_name(node.tag) != "TopicExpression":
            continue

        dialect = None
        for key, value in node.attrib.items():
            if _local_name(key).lower() == "dialect":
                dialect = value
                break

        if dialect and dialect not in {
            CONCRETE_TOPIC_DIALECT,
            CONCRETE_SET_DIALECT,
        }:
            raise EventSubscriptionError(
                "unsupported-filter",
                f"unsupported topic expression dialect: {dialect}",
            )

        value = (node.text or "").strip()
        if value:
            expressions.extend(
                item
                for item in re.split(r"\s*\|\s*|\s+", value)
                if item
            )

    if not expressions:
        return None

    known = set(known_topics)
    selected = set()
    for expression in expressions:
        normalized = _normalize_topic_expression(expression)
        if normalized in known:
            selected.add(normalized)
            continue

        descendants = {
            topic for topic in known if topic.startswith(normalized + "/")
        }
        if not descendants:
            raise EventSubscriptionError(
                "unsupported-filter",
                f"unsupported topic expression: {normalized}",
            )
        selected.update(descendants)

    return selected


def _iso_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "on", "active", "yes"}


def _stable_object_key(value) -> str:
    if not isinstance(value, dict):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def retained_event_key(event: dict) -> str:
    explicit = event.get("key")
    if explicit:
        return str(explicit)
    return f"{event.get('topic', '')}|{_stable_object_key(event.get('source', {}))}"


class VirtualSubscription:
    def __init__(
        self,
        sub_id: str,
        *,
        client_ip: str | None = None,
        ttl_seconds: int = DEFAULT_SUBSCRIPTION_TTL_SECONDS,
        topics=None,
        max_queue: int = DEFAULT_MAX_QUEUE,
        now: float | None = None,
    ):
        now = time.time() if now is None else now
        self.sub_id = sub_id
        self.client_ip = client_ip
        self.created_at = now
        self.expires_at = now + ttl_seconds
        self.topics = None if topics is None else set(topics)
        self.queue = queue.Queue(maxsize=max_queue)
        self.last_active = now
        self.pull_requests = 0
        self.messages_delivered = 0
        self.pending_pull = False
        self._lock = threading.Lock()

    @property
    def termination_time(self) -> str:
        return _iso_from_epoch(self.expires_at)

    def matches(self, event: dict) -> bool:
        return self.topics is None or event.get("topic") in self.topics

    def enqueue(self, event: dict) -> None:
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            self.queue.put_nowait(event)


class EventEngine:
    """Thread-safe retained-property and PullPoint subscription engine."""

    def __init__(
        self,
        *,
        max_queue: int = DEFAULT_MAX_QUEUE,
        max_pullpoints: int = DEFAULT_MAX_PULLPOINTS,
        now=None,
    ):
        self.max_queue = max_queue
        self.max_pullpoints = max_pullpoints
        self.now = now or time.time
        self.subscriptions: dict[str, VirtualSubscription] = {}
        self.retained: dict[str, dict] = {}
        self.topic_registry = set(DEFAULT_TOPICS)
        self._lock = threading.RLock()

        self.sequence = 0
        self.subscriptions_created = 0
        self.pull_requests = 0
        self.messages_delivered = 0
        self.synchronization_points = 0
        self.last_pull_request_at = None

    def prune_expired(self) -> int:
        now = self.now()
        removed = 0
        with self._lock:
            for sub_id, sub in list(self.subscriptions.items()):
                if sub.expires_at <= now:
                    self.subscriptions.pop(sub_id, None)
                    removed += 1
        return removed

    def require_subscription(self, sub_id: str) -> VirtualSubscription:
        self.prune_expired()
        with self._lock:
            sub = self.subscriptions.get(sub_id)
        if sub is None:
            raise EventSubscriptionError(
                "resource-unknown", f"unknown or expired subscription: {sub_id}"
            )
        return sub

    def create_subscription(
        self,
        *,
        client_ip: str | None = None,
        ttl_seconds: int = DEFAULT_SUBSCRIPTION_TTL_SECONDS,
        topics=None,
    ) -> VirtualSubscription:
        self.prune_expired()
        with self._lock:
            if len(self.subscriptions) >= self.max_pullpoints:
                raise EventSubscriptionError(
                    "capacity", "maximum PullPoint subscriptions reached"
                )
            sub_id = str(uuid.uuid4())
            sub = VirtualSubscription(
                sub_id,
                client_ip=client_ip,
                ttl_seconds=ttl_seconds,
                topics=topics,
                max_queue=self.max_queue,
                now=self.now(),
            )
            self.subscriptions[sub_id] = sub
            self.subscriptions_created += 1
            return sub

    def renew(self, sub_id: str, ttl_seconds: int) -> VirtualSubscription:
        sub = self.require_subscription(sub_id)
        now = self.now()
        sub.expires_at = now + ttl_seconds
        sub.last_active = now
        return sub

    def unsubscribe(self, sub_id: str) -> VirtualSubscription:
        sub = self.require_subscription(sub_id)
        with self._lock:
            self.subscriptions.pop(sub_id, None)
        return sub

    def publish(self, input_event: dict) -> dict:
        if not isinstance(input_event, dict):
            raise ValueError("event must be a dict")

        raw_topic = str(input_event.get("topic") or "").strip()
        topic = _normalize_topic_expression(raw_topic)
        if not topic:
            raise ValueError("event.topic must be non-empty")

        now = self.now()
        timestamp = (
            input_event.get("utcTime")
            or input_event.get("timestamp")
            or _iso_from_epoch(now)
        )
        try:
            parsed_timestamp = datetime.fromisoformat(
                str(timestamp).replace("Z", "+00:00")
            )
            if parsed_timestamp.tzinfo is None:
                parsed_timestamp = parsed_timestamp.replace(tzinfo=timezone.utc)
            timestamp = parsed_timestamp.astimezone(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )
        except ValueError:
            timestamp = _iso_from_epoch(now)

        data = input_event.get("data")
        if not isinstance(data, dict):
            data_name = input_event.get("data_name")
            if not data_name:
                data_name = "IsMotion" if topic == TOPICS["motion"] else "State"
            data = {str(data_name): _normalize_bool(input_event.get("value", False))}
        else:
            data = dict(data)

        source = input_event.get("source")
        source = dict(source) if isinstance(source, dict) else {}

        with self._lock:
            self.sequence += 1
            event = {
                "id": str(input_event.get("id") or uuid.uuid4()),
                "sequence": self.sequence,
                "topic": topic,
                "utcTime": timestamp,
                "propertyOperation": str(
                    input_event.get("propertyOperation")
                    or input_event.get("property_operation")
                    or "Changed"
                ),
                "source": source,
                "data": data,
            }

            self.topic_registry.add(topic)
            if input_event.get("retain", True) is not False:
                self.retained[retained_event_key({**input_event, **event})] = dict(event)

            subscribers = list(self.subscriptions.values())

        self.prune_expired()
        for sub in subscribers:
            if sub.expires_at <= self.now() or not sub.matches(event):
                continue
            sub.enqueue(dict(event))

        return event

    def pull(
        self,
        sub_id: str,
        *,
        message_limit: int,
        timeout_seconds: float = 0.0,
    ):
        sub = self.require_subscription(sub_id)
        now = self.now()
        remaining = max(0.0, sub.expires_at - now)
        wait_seconds = min(max(0.0, timeout_seconds), remaining, MAX_PULL_TIMEOUT_SECONDS)

        with sub._lock:
            if sub.pending_pull:
                raise EventSubscriptionError(
                    "concurrent-pull",
                    f"subscription already has a pending pull: {sub_id}",
                )
            sub.pending_pull = True

        messages = []
        try:
            if not sub.queue.empty():
                try:
                    messages.append(sub.queue.get_nowait())
                except queue.Empty:
                    pass
            elif wait_seconds > 0:
                try:
                    messages.append(sub.queue.get(timeout=wait_seconds))
                except queue.Empty:
                    pass

            while len(messages) < message_limit:
                try:
                    messages.append(sub.queue.get_nowait())
                except queue.Empty:
                    break
        finally:
            with sub._lock:
                sub.pending_pull = False

        now = self.now()
        sub.last_active = now
        sub.pull_requests += 1
        sub.messages_delivered += len(messages)

        with self._lock:
            self.pull_requests += 1
            self.messages_delivered += len(messages)
            self.last_pull_request_at = _iso_from_epoch(now)

        return sub, messages

    def set_synchronization_point(self, sub_id: str) -> int:
        sub = self.require_subscription(sub_id)
        now_iso = _iso_from_epoch(self.now())
        with self._lock:
            retained = list(self.retained.values())
            self.synchronization_points += 1

        queued = 0
        for event in retained:
            if not sub.matches(event):
                continue
            sync_event = {
                **event,
                "id": str(uuid.uuid4()),
                "utcTime": now_iso,
                "propertyOperation": "Initialized",
            }
            sub.enqueue(sync_event)
            queued += 1
        return queued

    def health(self) -> dict:
        self.prune_expired()
        with self._lock:
            subscriptions = list(self.subscriptions.values())
            return {
                "topics": len(self.topic_registry),
                "subscriptions": len(subscriptions),
                "retained": len(self.retained),
                "queued": sum(sub.queue.qsize() for sub in subscriptions),
                "pendingPulls": sum(1 for sub in subscriptions if sub.pending_pull),
                "sequence": self.sequence,
                "subscriptionsCreated": self.subscriptions_created,
                "pullRequests": self.pull_requests,
                "messagesDelivered": self.messages_delivered,
                "synchronizationPoints": self.synchronization_points,
                "lastPullRequestAt": self.last_pull_request_at,
            }


def _render_simple_items(items: dict) -> str:
    def simple_value(value):
        if isinstance(value, bool):
            return "true" if value else "false"
        return "" if value is None else str(value)

    return "".join(
        f'<tt:SimpleItem Name="{escape(str(name))}" Value="{escape(simple_value(value))}"/>'
        for name, value in (items or {}).items()
    )


def render_notification_message(
    event: dict,
    *,
    video_source_config_token: str,
    video_analytics_config_token: str = "video_analytics_config",
) -> str:
    topic = _normalize_topic_expression(str(event.get("topic") or ""))
    if not topic:
        raise ValueError("event.topic is required")

    source = {"VideoSourceConfigurationToken": video_source_config_token}
    if topic == TOPICS["motion"]:
        source.update(
            {
                "VideoAnalyticsConfigurationToken": video_analytics_config_token,
                "Rule": "MotionDetector",
            }
        )
    source.update(event.get("source") or {})

    return "".join(
        (
            "<wsnt:NotificationMessage>",
            f'<wsnt:Topic Dialect="{CONCRETE_SET_DIALECT}">tns1:{escape(topic)}</wsnt:Topic>',
            "<wsnt:Message>",
            f'<tt:Message UtcTime="{escape(str(event.get("utcTime") or ""))}" '
            f'PropertyOperation="{escape(str(event.get("propertyOperation") or "Changed"))}">',
            f"<tt:Source>{_render_simple_items(source)}</tt:Source>",
            f"<tt:Data>{_render_simple_items(event.get('data') or {})}</tt:Data>",
            "</tt:Message>",
            "</wsnt:Message>",
            "</wsnt:NotificationMessage>",
        )
    )


def render_topic_set(topics=DEFAULT_TOPICS, *, element_name="wstop:TopicSet") -> str:
    tree = {}
    for topic in topics:
        if topic not in _TOPIC_DEFINITIONS:
            continue
        cursor = tree
        for segment in topic.split("/"):
            cursor = cursor.setdefault(segment, {})
        cursor["__definition__"] = _TOPIC_DEFINITIONS[topic]

    def render_node(name, node):
        definition = node.get("__definition__")
        attrs = ' wstop:topic="true"' if definition else ""
        body = []

        if definition:
            source = "".join(
                f'<tt:SimpleItemDescription Name="{escape(item_name)}" Type="{escape(item_type)}"/>'
                for item_name, item_type in definition["source"]
            )
            data = "".join(
                f'<tt:SimpleItemDescription Name="{escape(item_name)}" Type="{escape(item_type)}"/>'
                for item_name, item_type in definition["data"]
            )
            body.append(
                '<tt:MessageDescription IsProperty="true">'
                f"<tt:Source>{source}</tt:Source>"
                f"<tt:Data>{data}</tt:Data>"
                "</tt:MessageDescription>"
            )

        for child_name in sorted(k for k in node if k != "__definition__"):
            body.append(render_node(child_name, node[child_name]))

        return f"<tns1:{name}{attrs}>{''.join(body)}</tns1:{name}>"

    nodes = "".join(render_node(name, tree[name]) for name in sorted(tree))
    return (
        f'<{element_name} xmlns:wstop="http://docs.oasis-open.org/wsn/t-1" '
        'xmlns:tns1="http://www.onvif.org/ver10/topics" '
        'xmlns:tt="http://www.onvif.org/ver10/schema" '
        'xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        f"{nodes}</{element_name}>"
    )
