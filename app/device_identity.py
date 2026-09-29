"""Canonical, persistent Protect-facing identity for virtual ONVIF cameras.

A virtual camera is an appliance from the NVR's point of view. Stream URLs,
camera display names and source settings may change, but the device identity
must not drift unless an operator explicitly changes an identity field.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid as uuidlib
from urllib.parse import quote


DEFAULT_MANUFACTURER = "VirtualCam"
DEFAULT_FIRMWARE_VERSION = "1.0.0"
DEFAULT_LOCATION = "virtual"
_MAX_IDENTITY_TEXT = 200
_MAC_RE = re.compile(r"^[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}$")


class DeviceIdentityError(ValueError):
    pass


def normalize_uuid(value=None) -> str:
    """Return a canonical UUID string, generating one only when absent."""
    if value is None or not str(value).strip():
        return str(uuidlib.uuid4())
    try:
        return str(uuidlib.UUID(str(value).strip()))
    except (ValueError, AttributeError, TypeError) as exc:
        raise DeviceIdentityError(f"Invalid device UUID: {value!r}") from exc


def normalize_mac(value: str) -> str:
    text = str(value or "").strip().lower().replace("-", ":")
    if not _MAC_RE.fullmatch(text):
        raise DeviceIdentityError(
            "MAC address must contain six hexadecimal octets (for example 02:11:22:33:44:55)"
        )
    first_octet = int(text.split(":", 1)[0], 16)
    if first_octet & 0x01:
        raise DeviceIdentityError("MAC address must be unicast")
    return text


def generated_mac_from_uuid(device_uuid: str) -> str:
    """Stable locally-administered unicast MAC derived from the device UUID."""
    canonical = normalize_uuid(device_uuid)
    digest = hashlib.sha256(canonical.encode("utf-8")).digest()
    octets = bytearray(digest[:6])
    octets[0] = (octets[0] | 0x02) & 0xFE
    return ":".join(f"{value:02x}" for value in octets)


def _identity_text(value, field, default):
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    if len(text) > _MAX_IDENTITY_TEXT:
        raise DeviceIdentityError(
            f"{field} must be {_MAX_IDENTITY_TEXT} characters or fewer"
        )
    return text


def _serial_from_mac(mac: str) -> str:
    return normalize_mac(mac).replace(":", "").upper()


def normalize_identity(
    identity,
    *,
    camera_name: str,
    mac: str,
    existing=None,
):
    """Normalize identity fields while preserving an existing identity by default."""
    incoming = dict(identity or {})
    previous = dict(existing or {})
    serial_default = _serial_from_mac(mac)

    def pick(name, default):
        if name in incoming and incoming[name] is not None:
            return _identity_text(incoming[name], name, default)
        if name in previous and previous[name] is not None:
            return _identity_text(previous[name], name, default)
        return default

    manufacturer = pick("manufacturer", DEFAULT_MANUFACTURER)
    # Freeze the initial model instead of deriving it on every request from a
    # mutable display name.
    model = pick("model", f"ONVIF {camera_name}")
    firmware = pick("firmwareVersion", DEFAULT_FIRMWARE_VERSION)
    serial = pick("serialNumber", serial_default)
    hardware = pick("hardwareId", f"{model}-{serial}")
    location = pick("location", DEFAULT_LOCATION)
    discovery_name = pick("discoveryName", f"{manufacturer} {model}".strip())

    return {
        "manufacturer": manufacturer,
        "model": model,
        "firmwareVersion": firmware,
        "serialNumber": serial,
        "hardwareId": hardware,
        "location": location,
        "discoveryName": discovery_name,
    }


def scope_escape(value) -> str:
    return quote(str(value or ""), safe="")


def scope_uris(identity) -> list[str]:
    """Return the exact fixed scopes used by both GetScopes and WS-Discovery."""
    return [
        "onvif://www.onvif.org/type/video_encoder",
        "onvif://www.onvif.org/Profile/Streaming",
        f"onvif://www.onvif.org/name/{scope_escape(identity['discoveryName'])}",
        f"onvif://www.onvif.org/hardware/{scope_escape(identity['hardwareId'])}",
        f"onvif://www.onvif.org/location/{scope_escape(identity['location'])}",
    ]


def identity_manifest(*, device_uuid: str, mac: str, identity) -> dict:
    """Build a stable manifest and fingerprint for adoption/acceptance checks."""
    canonical = {
        "uuid": normalize_uuid(device_uuid),
        "mac": normalize_mac(mac),
        "manufacturer": identity["manufacturer"],
        "model": identity["model"],
        "firmwareVersion": identity["firmwareVersion"],
        "serialNumber": identity["serialNumber"],
        "hardwareId": identity["hardwareId"],
        "location": identity["location"],
        "discoveryName": identity["discoveryName"],
        "scopes": scope_uris(identity),
    }
    encoded = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    canonical["fingerprint"] = hashlib.sha256(encoded).hexdigest()
    return canonical
