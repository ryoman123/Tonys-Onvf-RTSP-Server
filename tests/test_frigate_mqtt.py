import unittest
from types import SimpleNamespace

from app.frigate_mqtt import FrigateMqttRuntime, normalize_frigate_config


class FakeCamera:
    def __init__(self, name="Front Door", path_name="front_door", camera_id=1):
        self.id = camera_id
        self.name = name
        self.path_name = path_name
        self.status = "running"
        self.onvif_service = object()
        self.events = []
        self.cleared = []

    def publish_analytics_event(self, event):
        self.events.append(dict(event))
        return {"published": True}

    def clear_analytics_source(self, source):
        self.cleared.append(source)
        return []


class FakeManager:
    def __init__(self):
        self.cameras = [FakeCamera()]

    def resolve_camera_reference(self, reference):
        text = str(reference).strip().lower()
        for camera in self.cameras:
            if text in {
                str(camera.id),
                camera.name.lower(),
                camera.path_name.lower(),
            }:
                return camera
        return None


class FrigateRuntimeTests(unittest.TestCase):
    def make_runtime(self, **overrides):
        config = {
            "enabled": True,
            "host": "192.0.2.30",
            "port": 1883,
            "topicPrefix": "frigate",
            "cameraMap": {"frigate_front": "Front Door"},
            "autoMap": True,
            "username": "mqtt-user",
            "password": "mqtt-secret",
        }
        config.update(overrides)
        return FrigateMqttRuntime(FakeManager(), config)

    def test_event_topic_routes_through_explicit_camera_map(self):
        runtime = self.make_runtime()
        payload = b'''{
          "type": "new",
          "after": {
            "id": "person-1",
            "camera": "frigate_front",
            "label": "person",
            "frame_time": 1700000000,
            "top_score": 0.88
          }
        }'''

        result = runtime.route_message("frigate/events", payload)
        camera = runtime.manager.cameras[0]

        self.assertEqual(result, {"published": True})
        self.assertEqual(runtime.events_dispatched, 1)
        self.assertEqual(camera.events[0]["source"], "frigate")
        self.assertEqual(camera.events[0]["camera"], "Front Door")
        self.assertEqual(camera.events[0]["type"], "person")

    def test_motion_topic_can_auto_map_path_name(self):
        runtime = self.make_runtime(cameraMap={})
        runtime.route_message("frigate/front_door/motion", b"ON")
        camera = runtime.manager.cameras[0]

        self.assertEqual(len(camera.events), 1)
        self.assertEqual(camera.events[0]["type"], "motion")
        self.assertTrue(camera.events[0]["active"])

    def test_offline_availability_clears_frigate_contributors(self):
        runtime = self.make_runtime()
        runtime.route_message("frigate/available", b"offline")
        self.assertFalse(runtime.available)
        self.assertEqual(
            runtime.manager.cameras[0].cleared,
            ["frigate"],
        )

    def test_unmapped_camera_is_visible_in_health(self):
        runtime = self.make_runtime(cameraMap={}, autoMap=False)
        payload = b'''{
          "type": "new",
          "after": {
            "id": "person-2",
            "camera": "unknown_camera",
            "label": "person"
          }
        }'''
        self.assertIsNone(runtime.route_message("frigate/events", payload))
        health = runtime.health()
        self.assertIn("unknown_camera", health["unmappedCameras"])
        self.assertEqual(health["droppedMessages"], 1)

    def test_health_redacts_password(self):
        runtime = self.make_runtime()
        health = runtime.health()
        self.assertNotIn("password", health["config"])
        self.assertEqual(health["config"]["username"], "mqtt-user")

    def test_config_normalization_is_safe(self):
        config = normalize_frigate_config({
            "port": "99999",
            "topicPrefix": "/custom/",
            "keepaliveSeconds": 1,
            "cameraMap": None,
        })
        self.assertEqual(config["port"], 65535)
        self.assertEqual(config["topicPrefix"], "custom")
        self.assertEqual(config["keepaliveSeconds"], 5)
        self.assertEqual(config["cameraMap"], {})


if __name__ == "__main__":
    unittest.main()
