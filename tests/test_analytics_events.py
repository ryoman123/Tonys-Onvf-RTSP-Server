import json
import unittest

from app.analytics_events import (
    AnalyticsStateAggregator,
    TOPICS,
    frigate_event_to_analytics,
    frigate_motion_to_analytics,
    to_onvif_event,
)


class AnalyticsStateAggregatorTests(unittest.TestCase):
    def test_one_source_cannot_clear_another_active_source(self):
        state = AnalyticsStateAggregator()

        first = state.apply({
            "source": "local_ai",
            "camera": "Front",
            "type": "person",
            "active": True,
        })
        self.assertIsNotNone(first)
        self.assertTrue(first["active"])

        # A second source becoming active does not generate a duplicate ONVIF
        # transition because the aggregate property is already true.
        second = state.apply({
            "source": "frigate",
            "camera": "Front",
            "type": "person",
            "active": True,
            "objectId": "p1",
        })
        self.assertIsNone(second)

        # Local AI clearing while Frigate remains active must be suppressed.
        local_clear = state.apply({
            "source": "local_ai",
            "camera": "Front",
            "type": "person",
            "active": False,
        })
        self.assertIsNone(local_clear)

        # Only the last contributor clearing exposes the false transition.
        final = state.apply({
            "source": "frigate",
            "camera": "Front",
            "type": "person",
            "active": False,
            "objectId": "p1",
        })
        self.assertIsNotNone(final)
        self.assertFalse(final["active"])

    def test_multiple_objects_from_same_source_are_reference_counted(self):
        state = AnalyticsStateAggregator()
        self.assertTrue(state.apply({
            "source": "frigate",
            "camera": "Front",
            "type": "vehicle",
            "active": True,
            "objectId": "car-1",
        })["active"])

        self.assertIsNone(state.apply({
            "source": "frigate",
            "camera": "Front",
            "type": "vehicle",
            "active": True,
            "objectId": "car-2",
        }))
        self.assertIsNone(state.apply({
            "source": "frigate",
            "camera": "Front",
            "type": "vehicle",
            "active": False,
            "objectId": "car-1",
        }))

        cleared = state.apply({
            "source": "frigate",
            "camera": "Front",
            "type": "vehicle",
            "active": False,
            "objectId": "car-2",
        })
        self.assertFalse(cleared["active"])

    def test_clear_source_preserves_other_producers(self):
        state = AnalyticsStateAggregator()
        state.apply({
            "source": "frigate",
            "camera": "Front",
            "type": "motion",
            "active": True,
        })
        state.apply({
            "source": "physical_onvif",
            "camera": "Front",
            "type": "motion",
            "active": True,
        })

        self.assertEqual(state.clear_source("frigate", camera="Front"), [])
        health = state.health()
        self.assertEqual(
            health["active"]["motion"],
            ["physical_onvif|state"],
        )

        transitions = state.clear_source("physical_onvif", camera="Front")
        self.assertEqual(len(transitions), 1)
        self.assertFalse(transitions[0]["active"])

    def test_onvif_projection_uses_expected_topic(self):
        event = to_onvif_event({
            "source": "aggregate",
            "camera": "Front",
            "type": "package",
            "active": True,
        })
        self.assertEqual(event["topic"], TOPICS["package"])
        self.assertEqual(event["data_name"], "State")
        self.assertTrue(event["value"])


class FrigateAdapterTests(unittest.TestCase):
    def test_new_and_end_object_events_map_to_same_contributor(self):
        new_payload = {
            "type": "new",
            "after": {
                "id": "1700.abc",
                "camera": "front_door",
                "label": "person",
                "frame_time": 1700000000.5,
                "top_score": 0.91,
                "current_zones": ["porch"],
            },
        }
        ended_payload = {
            "type": "end",
            "after": {
                "id": "1700.abc",
                "camera": "front_door",
                "label": "person",
                "end_time": 1700000012.0,
            },
        }

        started = frigate_event_to_analytics(json.dumps(new_payload))
        ended = frigate_event_to_analytics(json.dumps(ended_payload))

        self.assertEqual(started["source"], "frigate")
        self.assertEqual(started["camera"], "front_door")
        self.assertEqual(started["type"], "person")
        self.assertTrue(started["active"])
        self.assertEqual(started["objectId"], "1700.abc")
        self.assertEqual(started["zones"], ["porch"])
        self.assertAlmostEqual(started["confidence"], 0.91)

        self.assertFalse(ended["active"])
        self.assertEqual(ended["objectId"], started["objectId"])

    def test_vehicle_labels_collapse_to_vehicle_property(self):
        payload = {
            "type": "update",
            "after": {
                "id": "truck-1",
                "camera": "driveway",
                "label": "truck",
                "frame_time": 1700000000,
            },
        }
        event = frigate_event_to_analytics(payload)
        self.assertEqual(event["type"], "vehicle")

    def test_unsupported_frigate_labels_are_ignored(self):
        payload = {
            "type": "new",
            "after": {
                "id": "chair-1",
                "camera": "patio",
                "label": "chair",
            },
        }
        self.assertIsNone(frigate_event_to_analytics(payload))

    def test_motion_topics_map_on_and_off(self):
        on = frigate_motion_to_analytics("front", "ON")
        off = frigate_motion_to_analytics("front", b"OFF")
        self.assertTrue(on["active"])
        self.assertFalse(off["active"])
        self.assertEqual(on["type"], "motion")
        self.assertIsNone(frigate_motion_to_analytics("front", "MAYBE"))


if __name__ == "__main__":
    unittest.main()
