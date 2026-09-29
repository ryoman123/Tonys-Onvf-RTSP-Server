import unittest

from app.recorder_events import (
    DahuaEventStreamParser,
    RecorderEventRuntime,
    normalize_recorder_config,
    parse_dahua_event_line,
)


class FakeCamera:
    def __init__(self, name="Lorex 1", path_name="lorex_1", camera_id=1):
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


class DahuaParserTests(unittest.TestCase):
    def test_motion_and_smart_motion_lines(self):
        motion = parse_dahua_event_line(
            "Code=VideoMotion;action=Start;index=0;"
        )
        person = parse_dahua_event_line(
            'Code=SmartMotionHuman;action=Start;index=1;data={"RegionName":"Porch"};'
        )
        vehicle = parse_dahua_event_line(
            "Code=SmartMotionVehicle;action=Stop;index=2;"
        )

        self.assertEqual(motion["type"], "motion")
        self.assertTrue(motion["active"])
        self.assertEqual(person["type"], "person")
        self.assertEqual(person["data"]["RegionName"], "Porch")
        self.assertEqual(vehicle["type"], "vehicle")
        self.assertFalse(vehicle["active"])

    def test_parser_handles_chunk_boundaries(self):
        events = []
        parser = DahuaEventStreamParser(events.append)
        parser.push(b"Code=VideoMotion;action=Start;")
        parser.push(b"index=0;\r\nCode=VideoMotion;action=Stop;index=0;\r\n")

        self.assertEqual(len(events), 2)
        self.assertTrue(events[0]["active"])
        self.assertFalse(events[1]["active"])

    def test_invalid_lines_are_ignored(self):
        self.assertIsNone(parse_dahua_event_line("heartbeat"))
        self.assertIsNone(
            parse_dahua_event_line("Code=VideoMotion;action=wat;index=0;")
        )
        self.assertIsNone(
            parse_dahua_event_line("Code=VideoMotion;action=Start;index=-1;")
        )


class RecorderRuntimeTests(unittest.TestCase):
    def make_runtime(self, **overrides):
        config = {
            "name": "lorex",
            "enabled": True,
            "host": "192.0.2.50",
            "source": "lorex",
            "channelMap": {"0": "Lorex 1"},
            "username": "admin",
            "password": "secret",
        }
        config.update(overrides)
        return RecorderEventRuntime(FakeManager(), config)

    def test_channel_event_routes_to_camera(self):
        runtime = self.make_runtime()
        runtime._handle_event({
            "channel": 0,
            "type": "person",
            "active": True,
            "action": "start",
            "data": None,
        })

        camera = runtime.manager.cameras[0]
        self.assertEqual(len(camera.events), 1)
        self.assertEqual(camera.events[0]["source"], "lorex")
        self.assertEqual(camera.events[0]["camera"], "Lorex 1")
        self.assertEqual(camera.events[0]["type"], "person")
        self.assertEqual(runtime.events_received, 1)
        self.assertEqual(runtime.events_dispatched, 1)

    def test_pulse_emits_active_then_clear_with_same_object(self):
        runtime = self.make_runtime()
        runtime._handle_event({
            "channel": 0,
            "type": "motion",
            "active": True,
            "action": "pulse",
            "data": None,
        })

        camera = runtime.manager.cameras[0]
        self.assertEqual(len(camera.events), 2)
        self.assertTrue(camera.events[0]["active"])
        self.assertFalse(camera.events[1]["active"])
        self.assertEqual(
            camera.events[0]["objectId"],
            camera.events[1]["objectId"],
        )

    def test_unmapped_channel_is_reported(self):
        runtime = self.make_runtime()
        runtime._handle_event({
            "channel": 9,
            "type": "motion",
            "active": True,
            "action": "start",
            "data": None,
        })
        self.assertEqual(runtime.dropped_events, 1)
        self.assertIn(9, runtime.health()["unmappedChannels"])

    def test_source_cleanup_reaches_all_cameras(self):
        runtime = self.make_runtime()
        runtime._clear_source()
        self.assertEqual(
            runtime.manager.cameras[0].cleared,
            ["lorex"],
        )

    def test_health_redacts_password(self):
        runtime = self.make_runtime()
        health = runtime.health()
        self.assertNotIn("password", health["config"])
        self.assertEqual(health["config"]["username"], "admin")

    def test_config_normalization(self):
        config = normalize_recorder_config({
            "protocol": "HTTPS://",
            "port": "70000",
            "path": "cgi-bin/events",
            "reconnectSeconds": 0,
            "channelMap": None,
        })
        self.assertEqual(config["protocol"], "https")
        self.assertEqual(config["port"], 65535)
        self.assertEqual(config["path"], "/cgi-bin/events")
        self.assertEqual(config["reconnectSeconds"], 1)
        self.assertEqual(config["channelMap"], {})


if __name__ == "__main__":
    unittest.main()
