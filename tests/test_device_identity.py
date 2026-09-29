import unittest

from app.camera import VirtualONVIFCamera
from app.device_identity import (
    DeviceIdentityError,
    generated_mac_from_uuid,
    identity_manifest,
    normalize_identity,
    normalize_mac,
    normalize_uuid,
    scope_uris,
)


class DeviceIdentityTests(unittest.TestCase):
    def test_uuid_and_generated_mac_are_canonical_and_stable(self):
        value = normalize_uuid("550E8400-E29B-41D4-A716-446655440000")
        self.assertEqual(value, "550e8400-e29b-41d4-a716-446655440000")
        self.assertEqual(
            generated_mac_from_uuid(value),
            generated_mac_from_uuid(value),
        )
        first_octet = int(generated_mac_from_uuid(value).split(":")[0], 16)
        self.assertEqual(first_octet & 0x01, 0)
        self.assertEqual(first_octet & 0x02, 0x02)

    def test_invalid_multicast_mac_is_rejected(self):
        with self.assertRaises(DeviceIdentityError):
            normalize_mac("01:00:5e:00:00:01")

    def test_identity_defaults_freeze_mutable_display_name(self):
        identity = normalize_identity(
            None,
            camera_name="Front Door",
            mac="02:11:22:33:44:55",
        )
        renamed = normalize_identity(
            None,
            camera_name="Renamed Camera",
            mac="02:11:22:33:44:55",
            existing=identity,
        )
        self.assertEqual(renamed, identity)
        self.assertEqual(identity["model"], "ONVIF Front Door")

    def test_manifest_fingerprint_changes_only_when_identity_changes(self):
        identity = normalize_identity(
            None,
            camera_name="Front Door",
            mac="02:11:22:33:44:55",
        )
        first = identity_manifest(
            device_uuid="550e8400-e29b-41d4-a716-446655440000",
            mac="02:11:22:33:44:55",
            identity=identity,
        )
        second = identity_manifest(
            device_uuid="550e8400-e29b-41d4-a716-446655440000",
            mac="02:11:22:33:44:55",
            identity=dict(identity),
        )
        self.assertEqual(first["fingerprint"], second["fingerprint"])

        changed = dict(identity)
        changed["model"] = "Different Model"
        third = identity_manifest(
            device_uuid="550e8400-e29b-41d4-a716-446655440000",
            mac="02:11:22:33:44:55",
            identity=changed,
        )
        self.assertNotEqual(first["fingerprint"], third["fingerprint"])

    def test_scope_set_is_stable_and_uri_escaped(self):
        identity = {
            "manufacturer": "VirtualCam",
            "model": "Model X",
            "firmwareVersion": "1.0",
            "serialNumber": "ABC",
            "hardwareId": "Model X/ABC",
            "location": "West Hill / Cameras",
            "discoveryName": "Front Door Camera",
        }
        scopes = scope_uris(identity)
        self.assertIn("onvif://www.onvif.org/type/video_encoder", scopes)
        self.assertIn("onvif://www.onvif.org/Profile/Streaming", scopes)
        self.assertIn("onvif://www.onvif.org/name/Front%20Door%20Camera", scopes)
        self.assertIn("onvif://www.onvif.org/hardware/Model%20X%2FABC", scopes)
        self.assertIn("onvif://www.onvif.org/location/West%20Hill%20%2F%20Cameras", scopes)


class CameraIdentityPersistenceTests(unittest.TestCase):
    def make_camera(self, **extra):
        config = {
            "id": 7,
            "uuid": "550e8400-e29b-41d4-a716-446655440000",
            "name": "Front Door",
            "mainStreamUrl": "rtsp://192.0.2.10/main",
            "subStreamUrl": "rtsp://192.0.2.10/sub",
            "nicMac": "02:11:22:33:44:55",
        }
        config.update(extra)
        return VirtualONVIFCamera(config)

    def test_camera_persists_identity_in_config_and_api(self):
        camera = self.make_camera(
            identity={
                "manufacturer": "Acme",
                "model": "VCam 4K",
                "firmwareVersion": "2.1",
                "serialNumber": "SER123",
                "hardwareId": "HW123",
                "location": "Exterior",
                "discoveryName": "Front Door",
            }
        )

        config = camera.to_config_dict()
        api = camera.to_dict()

        self.assertEqual(config["identity"]["serialNumber"], "SER123")
        self.assertEqual(api["identity"]["hardwareId"], "HW123")
        self.assertEqual(api["serialNumber"], "SER123")
        self.assertEqual(
            api["identityManifest"]["uuid"],
            "550e8400-e29b-41d4-a716-446655440000",
        )
        self.assertEqual(api["identityManifest"]["mac"], "02:11:22:33:44:55")

    def test_stream_and_name_edits_do_not_drift_identity(self):
        camera = self.make_camera()
        before = camera.get_identity_manifest()

        camera.name = "Completely Different Display Name"
        camera.main_stream_url = "rtsp://198.51.100.20/replaced"
        camera.sub_stream_url = "rtsp://198.51.100.20/replaced-sub"
        camera.set_identity(None)

        after = camera.get_identity_manifest()
        self.assertEqual(before["uuid"], after["uuid"])
        self.assertEqual(before["mac"], after["mac"])
        self.assertEqual(before["model"], after["model"])
        self.assertEqual(before["serialNumber"], after["serialNumber"])
        self.assertEqual(before["hardwareId"], after["hardwareId"])
        self.assertEqual(before["fingerprint"], after["fingerprint"])

    def test_partial_identity_edit_preserves_unspecified_fields(self):
        camera = self.make_camera(
            identity={
                "manufacturer": "Acme",
                "model": "Original",
                "firmwareVersion": "1",
                "serialNumber": "SER",
                "hardwareId": "HW",
                "location": "A",
                "discoveryName": "Cam A",
            }
        )
        camera.set_identity({"firmwareVersion": "2"})

        self.assertEqual(camera.identity["firmwareVersion"], "2")
        self.assertEqual(camera.identity["manufacturer"], "Acme")
        self.assertEqual(camera.identity["model"], "Original")
        self.assertEqual(camera.identity["serialNumber"], "SER")


if __name__ == "__main__":
    unittest.main()
