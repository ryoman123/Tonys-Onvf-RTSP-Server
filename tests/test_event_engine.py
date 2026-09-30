import unittest

from app.event_engine import (
    CONCRETE_SET_DIALECT,
    EventEngine,
    EventSubscriptionError,
    TOPICS,
    parse_message_limit,
    parse_pull_timeout_seconds,
    parse_topic_filter,
    render_notification_message,
    render_topic_set,
    resolve_termination_seconds,
)


class FakeClock:
    def __init__(self, value=1_700_000_000.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class EventEngineTests(unittest.TestCase):
    def test_publish_pull_and_synchronization_point(self):
        clock = FakeClock()
        engine = EventEngine(now=clock)
        sub = engine.create_subscription(client_ip="192.0.2.10", ttl_seconds=600)

        published = engine.publish(
            {
                "topic": TOPICS["motion"],
                "value": "true",
                "data_name": "IsMotion",
                "timestamp": "2026-09-29T12:00:00Z",
            }
        )

        self.assertEqual(published["propertyOperation"], "Changed")
        self.assertTrue(published["data"]["IsMotion"])

        _, messages = engine.pull(
            sub.sub_id,
            message_limit=10,
            timeout_seconds=0,
        )
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["topic"], TOPICS["motion"])

        queued = engine.set_synchronization_point(sub.sub_id)
        self.assertEqual(queued, 1)

        _, sync_messages = engine.pull(
            sub.sub_id,
            message_limit=10,
            timeout_seconds=0,
        )
        self.assertEqual(len(sync_messages), 1)
        self.assertEqual(sync_messages[0]["propertyOperation"], "Initialized")
        self.assertTrue(sync_messages[0]["data"]["IsMotion"])

    def test_filter_only_receives_selected_topic(self):
        engine = EventEngine()
        sub = engine.create_subscription(
            ttl_seconds=600,
            topics={TOPICS["person"]},
        )

        engine.publish({"topic": TOPICS["motion"], "value": True})
        engine.publish({"topic": TOPICS["person"], "value": True})

        _, messages = engine.pull(
            sub.sub_id,
            message_limit=10,
            timeout_seconds=0,
        )
        self.assertEqual([item["topic"] for item in messages], [TOPICS["person"]])

    def test_expired_subscription_is_pruned(self):
        clock = FakeClock()
        engine = EventEngine(now=clock)
        sub = engine.create_subscription(ttl_seconds=5)
        clock.advance(6)

        with self.assertRaises(EventSubscriptionError) as ctx:
            engine.require_subscription(sub.sub_id)

        self.assertEqual(ctx.exception.code, "resource-unknown")
        self.assertEqual(engine.health()["subscriptions"], 0)

    def test_renew_extends_subscription(self):
        clock = FakeClock()
        engine = EventEngine(now=clock)
        sub = engine.create_subscription(ttl_seconds=5)

        clock.advance(4)
        engine.renew(sub.sub_id, 20)
        clock.advance(10)

        self.assertIs(engine.require_subscription(sub.sub_id), sub)

    def test_topic_filter_supports_concrete_set_and_parent_paths(self):
        body = f"""<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
                              xmlns:tev="http://www.onvif.org/ver10/events/wsdl"
                              xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2">
            <s:Body>
              <tev:CreatePullPointSubscription>
                <tev:Filter>
                  <wsnt:TopicExpression Dialect="{CONCRETE_SET_DIALECT}">
                    tns1:UserAlarm
                  </wsnt:TopicExpression>
                </tev:Filter>
              </tev:CreatePullPointSubscription>
            </s:Body>
        </s:Envelope>"""

        selected = parse_topic_filter(body)
        self.assertEqual(
            selected,
            {TOPICS["person"]},
        )

    def test_pull_parsing_enforces_bounds(self):
        body = """<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
                              xmlns:tev="http://www.onvif.org/ver10/events/wsdl">
          <s:Body>
            <tev:PullMessages>
              <tev:Timeout>PT600S</tev:Timeout>
              <tev:MessageLimit>9999</tev:MessageLimit>
            </tev:PullMessages>
          </s:Body>
        </s:Envelope>"""

        self.assertEqual(parse_pull_timeout_seconds(body), 60)
        self.assertEqual(parse_message_limit(body), 256)

    def test_termination_supports_duration_and_absolute_time(self):
        self.assertEqual(resolve_termination_seconds("PT15M", now=0), 900)
        absolute = resolve_termination_seconds(
            "2026-09-29T12:10:00Z",
            now=1_759_147_800.0,
        )
        self.assertGreater(absolute, 0)

    def test_notification_message_has_property_operation_and_tokens(self):
        xml = render_notification_message(
            {
                "topic": TOPICS["motion"],
                "utcTime": "2026-09-29T12:00:00Z",
                "propertyOperation": "Initialized",
                "source": {},
                "data": {"IsMotion": True},
            },
            video_source_config_token="VideoSource_1",
            video_analytics_config_token="VideoAnalytics_1",
        )

        self.assertIn('PropertyOperation="Initialized"', xml)
        self.assertIn('Value="VideoSource_1"', xml)
        self.assertIn('Value="VideoAnalytics_1"', xml)
        self.assertIn('Value="true"', xml)

    def test_topic_set_advertises_all_smart_topics(self):
        xml = render_topic_set(element_name="tet:TopicSet")
        self.assertIn("<tet:TopicSet", xml)
        self.assertIn("CellMotionDetector", xml)
        self.assertIn("HumanShapeDetect", xml)
        self.assertIn("VehicleDetect", xml)
        self.assertIn("DogCatDetect", xml)
        self.assertIn("Package", xml)


if __name__ == "__main__":
    unittest.main()
