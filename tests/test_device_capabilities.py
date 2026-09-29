import unittest
import xml.etree.ElementTree as ET
from types import SimpleNamespace

from app.device_capabilities import (
    DeviceRequestError,
    EVENT_NS,
    MEDIA_NS,
    DEVICE_NS,
    capability_categories,
    include_capability,
    render_get_capabilities,
    render_get_device_service_capabilities,
    render_get_services,
)


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def make_camera(**overrides):
    values = {
        "onvif_port": 8001,
        "disable_substream": False,
        "enable_audio": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class DeviceCapabilitiesTests(unittest.TestCase):
    def test_category_filter_does_not_fabricate_unimplemented_services(self):
        body = """<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
                              xmlns:tds="http://www.onvif.org/ver10/device/wsdl">
          <s:Body>
            <tds:GetCapabilities>
              <tds:Category>Media</tds:Category>
            </tds:GetCapabilities>
          </s:Body>
        </s:Envelope>"""

        categories = capability_categories(body)
        xml = render_get_capabilities(make_camera(), "192.0.2.20", categories)
        root = ET.fromstring(xml)
        caps = next(node for node in root.iter() if local_name(node.tag) == "Capabilities")
        children = [local_name(node.tag) for node in caps]

        self.assertEqual(children, ["Media"])
        self.assertNotIn("Analytics", xml)
        self.assertNotIn("Imaging", xml)
        self.assertNotIn("DeviceIO", xml)

    def test_all_capabilities_match_live_service_set(self):
        body = """<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
                              xmlns:tds="http://www.onvif.org/ver10/device/wsdl">
          <s:Body><tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities></s:Body>
        </s:Envelope>"""
        xml = render_get_capabilities(
            make_camera(),
            "192.0.2.20",
            capability_categories(body),
        )
        root = ET.fromstring(xml)
        caps = next(node for node in root.iter() if local_name(node.tag) == "Capabilities")
        self.assertEqual(
            [local_name(node.tag) for node in caps],
            ["Device", "Events", "Media"],
        )
        self.assertIn("<tt:WSSubscriptionPolicySupport>false</tt:WSSubscriptionPolicySupport>", xml)

    def test_maximum_profiles_tracks_substream_state(self):
        enabled = render_get_capabilities(
            make_camera(disable_substream=False),
            "192.0.2.20",
            {"Media"},
        )
        disabled = render_get_capabilities(
            make_camera(disable_substream=True),
            "192.0.2.20",
            {"Media"},
        )
        self.assertIn("<tt:MaximumNumberOfProfiles>2</tt:MaximumNumberOfProfiles>", enabled)
        self.assertIn("<tt:MaximumNumberOfProfiles>1</tt:MaximumNumberOfProfiles>", disabled)

    def test_get_services_lists_only_device_media_and_events(self):
        xml = render_get_services(
            make_camera(),
            "192.0.2.20",
            include_capabilities=False,
        )
        root = ET.fromstring(xml)
        namespaces = [
            (node.text or "").strip()
            for node in root.iter()
            if local_name(node.tag) == "Namespace"
        ]
        self.assertEqual(namespaces, [DEVICE_NS, MEDIA_NS, EVENT_NS])

    def test_get_services_honors_include_capability(self):
        request_body = """<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
                                      xmlns:tds="http://www.onvif.org/ver10/device/wsdl">
          <s:Body>
            <tds:GetServices><tds:IncludeCapability>true</tds:IncludeCapability></tds:GetServices>
          </s:Body>
        </s:Envelope>"""
        self.assertTrue(include_capability(request_body))

        xml = render_get_services(
            make_camera(),
            "192.0.2.20",
            include_capabilities=True,
            max_pullpoints=32,
        )
        root = ET.fromstring(xml)
        service_nodes = [node for node in root.iter() if local_name(node.tag) == "Service"]
        self.assertEqual(len(service_nodes), 3)

        for service in service_nodes:
            direct = [local_name(child.tag) for child in service]
            self.assertIn("Capabilities", direct)

        self.assertIn('WSPullPointSupport="true"', xml)
        self.assertIn('MaxPullPoints="32"', xml)

    def test_device_service_capabilities_are_parseable(self):
        xml = render_get_device_service_capabilities()
        root = ET.fromstring(xml)
        response = next(
            node for node in root.iter()
            if local_name(node.tag) == "GetServiceCapabilitiesResponse"
        )
        capabilities = next(
            child for child in response
            if local_name(child.tag) == "Capabilities"
        )
        self.assertEqual(
            [local_name(child.tag) for child in capabilities],
            ["Network", "Security", "System"],
        )

    def test_malformed_device_request_is_rejected(self):
        with self.assertRaises(DeviceRequestError):
            capability_categories("<not-closed")


if __name__ == "__main__":
    unittest.main()
