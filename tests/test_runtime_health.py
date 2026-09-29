import unittest
from types import SimpleNamespace

from app.runtime_health import (
    build_identity_manifest,
    build_readiness,
    camera_readiness,
    evaluate_acceptance,
)


class FakeThread:
    def __init__(self, alive=True):
        self.alive = alive

    def is_alive(self):
        return self.alive


class FakeProcess:
    def __init__(self, returncode=None):
        self.returncode = returncode

    def poll(self):
        return self.returncode


class FakeOnvif:
    def __init__(self, subscriptions=1):
        self._discovery_thread = FakeThread()
        self.subscriptions = subscriptions

    def event_health(self):
        return {
            "subscriptions": self.subscriptions,
            "messagesDelivered": 7,
            "lastPullRequestAt": "2026-09-29T20:00:00Z",
        }


class FakeAnalytics:
    def health(self):
        return {
            "active": {},
            "transitions": 2,
            "suppressed": 3,
        }


class FakeCamera:
    def __init__(self, name="Cam 1", camera_id=1, fingerprint="fp1"):
        self.id = camera_id
        self.name = name
        self.path_name = name.lower().replace(" ", "_")
        self.auto_start = True
        self.status = "running"
        self.server = object()
        self.flask_thread = FakeThread()
        self.onvif_service = FakeOnvif()
        self.analytics_state = FakeAnalytics()
        self.onvif_port = 8000 + camera_id
        self.rtsp_port = 8554
        self.main_width = 3840
        self.main_height = 2160
        self.main_framerate = 7
        self.sub_width = 960
        self.sub_height = 480
        self.sub_framerate = 7
        self.disable_substream = False
        self.stream_probe = {
            "mismatch": False,
            "main": {"width": 3840, "height": 2160, "codec": "h264"},
        }
        self._fingerprint = fingerprint

    def get_effective_ip(self):
        return f"192.0.2.{10 + self.id}"

    def get_identity_manifest(self):
        return {
            "uuid": f"00000000-0000-0000-0000-{self.id:012d}",
            "mac": f"02:00:00:00:00:{self.id:02x}",
            "manufacturer": "VirtualCam",
            "model": "Virtual",
            "firmwareVersion": "1.0",
            "serialNumber": f"SER{self.id}",
            "hardwareId": f"HW{self.id}",
            "location": "virtual",
            "discoveryName": self.name,
            "scopes": [],
            "fingerprint": self._fingerprint,
        }


class FakeManager:
    def __init__(self, cameras=None):
        self.cameras = cameras or [FakeCamera()]
        self.mediamtx = SimpleNamespace(process=FakeProcess())
        self.external_analytics_config = {
            "frigate": {"enabled": False},
            "recorders": [],
        }

    def external_analytics_health(self):
        return {
            "frigate": {"state": "disabled", "started": False},
            "recorders": [],
            "cameras": {},
        }


class RuntimeHealthTests(unittest.TestCase):
    def test_healthy_inventory_is_ready(self):
        status = build_readiness(FakeManager(), boot_id="boot-1")
        self.assertTrue(status["ready"])
        self.assertEqual(status["status"], "healthy")
        self.assertTrue(status["identity"]["valid"])
        self.assertEqual(status["cameras"]["ready"], 1)

    def test_stream_mismatch_degrades_camera_and_runtime(self):
        camera = FakeCamera()
        camera.stream_probe["mismatch"] = True
        status = build_readiness(FakeManager([camera]))

        self.assertFalse(status["ready"])
        self.assertIn(
            "stream-metadata-mismatch",
            status["cameras"]["items"][0]["issues"],
        )

    def test_dead_onvif_thread_is_not_ready(self):
        camera = FakeCamera()
        camera.flask_thread = FakeThread(False)
        item = camera_readiness(camera)
        self.assertFalse(item["ready"])
        self.assertIn("onvif-http-not-ready", item["issues"])

    def test_duplicate_identity_is_detected(self):
        first = FakeCamera("One", 1, fingerprint="same")
        second = FakeCamera("Two", 2, fingerprint="same")
        status = build_readiness(FakeManager([first, second]))
        self.assertFalse(status["identity"]["valid"])
        self.assertTrue(
            any(
                conflict["field"] == "fingerprint"
                and conflict["reason"] == "duplicate"
                for conflict in status["identity"]["conflicts"]
            )
        )

    def test_acceptance_requires_exact_camera_count(self):
        status = build_readiness(FakeManager())
        result = evaluate_acceptance(status, expected_cameras=29)
        self.assertFalse(result["passed"])
        self.assertIn("expected 29 cameras, found 1", result["failures"])

    def test_acceptance_can_require_pullpoint_consumer(self):
        camera = FakeCamera()
        camera.onvif_service = FakeOnvif(subscriptions=0)
        status = build_readiness(FakeManager([camera]))
        result = evaluate_acceptance(
            status,
            require_pullpoint_subscribers=True,
        )
        self.assertFalse(result["passed"])
        self.assertIn("Cam 1: no active PullPoint subscriber", result["failures"])

    def test_identity_manifest_round_trips_into_acceptance(self):
        status = build_readiness(FakeManager())
        manifest = build_identity_manifest(status)
        result = evaluate_acceptance(
            status,
            expected_cameras=1,
            expected_manifest=manifest,
        )
        self.assertTrue(result["passed"])

        manifest["cameras"][0]["serialNumber"] = "WRONG"
        result = evaluate_acceptance(status, expected_manifest=manifest)
        self.assertFalse(result["passed"])
        self.assertTrue(
            any("serialNumber expected 'WRONG'" in item for item in result["failures"])
        )

    def test_enabled_external_analytics_exposed_separately_from_core(self):
        manager = FakeManager()
        manager.external_analytics_config["frigate"]["enabled"] = True
        manager.external_analytics_health = lambda: {
            "frigate": {
                "state": "disconnected",
                "available": False,
                "started": True,
            },
            "recorders": [],
            "cameras": {},
        }
        status = build_readiness(manager)
        self.assertTrue(status["ready"])
        self.assertFalse(status["analytics"]["ready"])

        result = evaluate_acceptance(status, require_analytics=True)
        self.assertFalse(result["passed"])
        self.assertIn(
            "required external analytics producer is not ready",
            result["failures"],
        )


if __name__ == "__main__":
    unittest.main()
