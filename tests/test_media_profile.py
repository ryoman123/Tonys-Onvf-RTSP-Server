import unittest
import xml.etree.ElementTree as ET
from types import SimpleNamespace

from app.media_profile import (
    MediaProfileError,
    audio_parameters,
    profile_kind_from_token,
    render_get_profile_response,
    render_get_profiles_response,
    validate_stream_setup,
)


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def make_camera(**overrides):
    values = {
        "id": 2,
        "main_width": 3840,
        "main_height": 2160,
        "main_framerate": 7,
        "sub_width": 960,
        "sub_height": 480,
        "sub_framerate": 7,
        "main_encoding": "H264",
        "sub_encoding": "H264",
        "disable_substream": False,
        "enable_audio": True,
        "transcode_main_audio": False,
        "audio_encoding_main": "aac",
        "audio_sample_rate_main": "44100",
        "audio_bitrate_main": "128k",
        "transcode_sub_audio": False,
        "audio_encoding_sub": "aac",
        "audio_sample_rate_sub": "44100",
        "audio_bitrate_sub": "128k",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class MediaProfileTests(unittest.TestCase):
    def test_audio_enabled_profile_follows_onvif_xsd_sequence(self):
        """Regression for upstream issue #66 / strict ODM deserialization."""
        xml = render_get_profiles_response(make_camera(enable_audio=True))
        root = ET.fromstring(xml)
        profiles = [
            node for node in root.iter()
            if local_name(node.tag) == "Profiles"
        ]

        self.assertEqual(len(profiles), 2)
        expected = [
            "Name",
            "VideoSourceConfiguration",
            "AudioSourceConfiguration",
            "VideoEncoderConfiguration",
            "AudioEncoderConfiguration",
        ]
        for profile in profiles:
            self.assertEqual([local_name(child.tag) for child in profile], expected)

    def test_audio_disabled_profile_has_valid_remaining_sequence(self):
        xml = render_get_profiles_response(make_camera(enable_audio=False))
        root = ET.fromstring(xml)
        profiles = [
            node for node in root.iter()
            if local_name(node.tag) == "Profiles"
        ]
        expected = [
            "Name",
            "VideoSourceConfiguration",
            "VideoEncoderConfiguration",
        ]
        for profile in profiles:
            self.assertEqual([local_name(child.tag) for child in profile], expected)

    def test_shared_video_source_configuration_is_consistent(self):
        xml = render_get_profiles_response(make_camera())
        root = ET.fromstring(xml)
        configs = [
            node for node in root.iter()
            if local_name(node.tag) == "VideoSourceConfiguration"
        ]

        self.assertEqual(len(configs), 2)
        self.assertEqual(configs[0].attrib, configs[1].attrib)

        def normalized(node):
            return [
                (local_name(child.tag), child.text, child.attrib)
                for child in node
            ]

        self.assertEqual(normalized(configs[0]), normalized(configs[1]))
        self.assertEqual(configs[0].attrib["token"], "VideoSource_2")

    def test_profile_tokens_are_exact_not_fuzzy(self):
        camera = make_camera()
        self.assertEqual(profile_kind_from_token(camera, "mainStream_2"), "main")
        self.assertEqual(profile_kind_from_token(camera, "subStream_2"), "sub")

        for bad in (None, "", "mainStream", "subStream", "mainStream_20", "junk"):
            with self.assertRaises(MediaProfileError):
                profile_kind_from_token(camera, bad)

    def test_disabled_substream_rejects_sub_profile(self):
        camera = make_camera(disable_substream=True)
        with self.assertRaises(MediaProfileError) as ctx:
            profile_kind_from_token(camera, "subStream_2")
        self.assertEqual(ctx.exception.code, "no-profile")

        xml = render_get_profiles_response(camera)
        root = ET.fromstring(xml)
        profiles = [
            node for node in root.iter()
            if local_name(node.tag) == "Profiles"
        ]
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0].attrib["token"], "mainStream_2")

    def test_get_profile_contains_only_requested_profile(self):
        xml = render_get_profile_response(make_camera(), "sub")
        root = ET.fromstring(xml)
        profiles = [
            node for node in root.iter()
            if local_name(node.tag) == "Profile"
        ]
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0].attrib["token"], "subStream_2")

    def test_h265_profile_advertises_hevc_without_h264_extension(self):
        xml = render_get_profile_response(
            make_camera(main_encoding="H265"),
            "main",
        )
        root = ET.fromstring(xml)
        encoder = next(
            node for node in root.iter()
            if local_name(node.tag) == "VideoEncoderConfiguration"
        )
        children = {
            local_name(child.tag): child
            for child in encoder
        }
        self.assertEqual(children["Encoding"].text, "H265")
        self.assertNotIn("H264", children)

    def test_mixed_h265_main_h264_sub_profiles_keep_each_codec(self):
        xml = render_get_profiles_response(
            make_camera(main_encoding="H265", sub_encoding="H264")
        )
        root = ET.fromstring(xml)
        encoders = [
            node for node in root.iter()
            if local_name(node.tag) == "VideoEncoderConfiguration"
        ]
        encodings = [
            next(
                child.text for child in encoder
                if local_name(child.tag) == "Encoding"
            )
            for encoder in encoders
        ]
        self.assertEqual(encodings, ["H265", "H264"])

    def test_transcoded_audio_values_are_normalized(self):
        camera = make_camera(
            transcode_main_audio=True,
            audio_encoding_main="aac",
            audio_sample_rate_main="44.1kHz",
            audio_bitrate_main="128k",
        )
        audio = audio_parameters(camera, "main")
        self.assertEqual(audio["encoding"], "AAC")
        self.assertEqual(audio["sample_rate"], 44100)
        self.assertEqual(audio["bitrate"], 128)

    def test_stream_setup_requires_supported_unicast_transport(self):
        valid = """<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
                              xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
                              xmlns:tt="http://www.onvif.org/ver10/schema">
          <s:Body>
            <trt:GetStreamUri>
              <trt:StreamSetup>
                <tt:Stream>RTP-Unicast</tt:Stream>
                <tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>
              </trt:StreamSetup>
              <trt:ProfileToken>mainStream_2</trt:ProfileToken>
            </trt:GetStreamUri>
          </s:Body>
        </s:Envelope>"""
        self.assertEqual(
            validate_stream_setup(valid),
            {"stream": "RTP-Unicast", "protocol": "RTSP"},
        )

        invalid = valid.replace("RTP-Unicast", "RTP-Multicast")
        with self.assertRaises(MediaProfileError) as ctx:
            validate_stream_setup(invalid)
        self.assertEqual(ctx.exception.code, "invalid-stream-setup")


if __name__ == "__main__":
    unittest.main()
