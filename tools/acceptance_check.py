#!/usr/bin/env python3
"""Field acceptance and soak gate for the unified Tony-based runtime."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Running "python tools/acceptance_check.py" sets sys.path[0] to tools/.
# Add the repository root so the application package resolves without install.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.runtime_health import build_identity_manifest, evaluate_acceptance


def fetch_json(url, timeout=10):
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
        payload = response.read()
    except urllib.error.HTTPError as error:
        payload = error.read()
        if not payload:
            raise
    return json.loads(payload.decode("utf-8"))


def load_manifest(path):
    if not path:
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def continuity_failures(previous, current):
    failures = []
    if previous.get("bootId") != current.get("bootId"):
        failures.append(
            f"process bootId changed during soak "
            f"({previous.get('bootId')} -> {current.get('bootId')})"
        )

    previous_cameras = {
        item.get("name"): item
        for item in (previous.get("cameras") or {}).get("items", [])
    }
    current_cameras = {
        item.get("name"): item
        for item in (current.get("cameras") or {}).get("items", [])
    }

    if set(previous_cameras) != set(current_cameras):
        failures.append("camera inventory changed during soak")

    for name, current_camera in current_cameras.items():
        old = previous_cameras.get(name)
        if not old:
            continue

        old_identity = (old.get("identity") or {}).get("fingerprint")
        new_identity = (current_camera.get("identity") or {}).get("fingerprint")
        if old_identity != new_identity:
            failures.append(f"{name}: identity fingerprint changed during soak")

        old_delivered = (old.get("events") or {}).get("messagesDelivered", 0)
        new_delivered = (current_camera.get("events") or {}).get("messagesDelivered", 0)
        if (
            isinstance(old_delivered, int)
            and isinstance(new_delivered, int)
            and new_delivered < old_delivered
        ):
            failures.append(
                f"{name}: PullPoint delivery counter regressed "
                f"({old_delivered} -> {new_delivered})"
            )

    return failures


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:5552/api/readiness",
        help="Unified runtime readiness endpoint",
    )
    parser.add_argument("--expected-cameras", type=int)
    parser.add_argument("--manifest", help="Previously captured identity manifest JSON")
    parser.add_argument(
        "--write-manifest",
        help="Write the observed identity manifest after a successful check",
    )
    parser.add_argument(
        "--require-pullpoint-subscribers",
        action="store_true",
        help="Require at least one active PullPoint consumer on every camera",
    )
    parser.add_argument(
        "--require-analytics",
        action="store_true",
        help="Require every enabled external analytics producer to be ready",
    )
    parser.add_argument(
        "--soak-seconds",
        type=int,
        default=0,
        help="Continue checking readiness/continuity for this many seconds",
    )
    parser.add_argument('--require-smart-pipeline', action='store_true',
                        help='Require healthy per-camera detection, PullPoints and a recently checked Protect listener')
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=30,
        help="Interval between soak samples",
    )
    args = parser.parse_args(argv)

    manifest = load_manifest(args.manifest)
    status = fetch_json(args.url)
    result = evaluate_acceptance(
        status,
        expected_cameras=args.expected_cameras,
        require_pullpoint_subscribers=args.require_pullpoint_subscribers,
        require_analytics=args.require_analytics,
        require_smart_pipeline=args.require_smart_pipeline,
        expected_manifest=manifest,
    )

    failures = list(result["failures"])
    baseline = status
    deadline = time.monotonic() + max(0, args.soak_seconds)

    while not failures and time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        time.sleep(min(max(1, args.interval_seconds), remaining))
        current = fetch_json(args.url)
        sample = evaluate_acceptance(
            current,
            expected_cameras=args.expected_cameras,
            require_pullpoint_subscribers=args.require_pullpoint_subscribers,
            require_analytics=args.require_analytics,
            require_smart_pipeline=args.require_smart_pipeline,
            expected_manifest=manifest,
        )
        failures.extend(sample["failures"])
        failures.extend(continuity_failures(baseline, current))
        baseline = current

    passed = not failures
    output = {
        "passed": passed,
        "failures": failures,
        "cameras": result["cameras"],
        "bootId": baseline.get("bootId"),
        "timestamp": baseline.get("timestamp"),
        "soakSeconds": max(0, args.soak_seconds),
        "smartPipeline": baseline.get('fullStack'),
    }
    print(json.dumps(output, indent=2))

    if passed and args.write_manifest:
        path = Path(args.write_manifest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(build_identity_manifest(baseline), indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Identity manifest written to {path}", file=sys.stderr)

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
