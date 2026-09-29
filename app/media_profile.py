"""Schema-ordered ONVIF Media profile helpers for the unified runtime.

Tony's original raw XML was intentionally minimal for Protect, but strict ONVIF
clients deserialize against the Profile sequence in onvif.xsd. In particular,
AudioSourceConfiguration must appear before VideoEncoderConfiguration and
AudioEncoderConfiguration must follow it. These helpers centralize profile
tokens and render the exact schema order so GetProfiles/GetProfile cannot drift.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from xml.sax.saxutils import escape


class MediaProfileError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def extract_request_text(soap_body: str, local_name: str) -> str | None:
    try:
        root = ET.fromstring(soap_body)
    except ET.ParseError as exc:
        raise MediaProfileError("invalid-xml", f"invalid SOAP XML: {exc}") from exc

    for node in root.iter():
        if _local_name(node.tag) == local_name:
            text = (node.text or "").strip()
            return text or None
    return None


def _parse_positive_int(value, default: int) -> int:
    if value is None:
        return default
    text = str(value).strip().lower()

    # Accept common UI forms such as 128k, 128kbps, 44100Hz and 44.1kHz.
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(khz|hz|kbps|k|bps)?", text)
    if not match:
        return default

    number = float(match.group(1))
    unit = match.group(2) or ""

    if unit == "khz":
        number *= 1000
    elif unit == "bps":
        # ONVIF AudioEncoderConfiguration Bitrate is kbit/s; callers use this
        # parser for both sample rate and bitrate, so only scale obviously large
        # bps values down to kbit/s.
        if number >= 1000:
            number /= 1000
    elif unit in {"k", "kbps"}:
        # For a sample-rate string like 44.1k this should be Hz; for bitrate it
        # should remain kbit/s. The caller selects the interpretation.
        pass

    return max(1, int(round(number)))


def parse_sample_rate(value, default=8000) -> int:
    if value is None:
        return default
    text = str(value).strip().lower()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(khz|hz|k)?", text)
    if not match:
        return default
    number = float(match.group(1))
    unit = match.group(2) or ""
    if unit in {"khz", "k"}:
        number *= 1000
    return max(1, int(round(number)))


def parse_bitrate(value, default=64) -> int:
    if value is None:
        return default
    text = str(value).strip().lower()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(kbps|k|bps)?", text)
    if not match:
        return default
    number = float(match.group(1))
    unit = match.group(2) or ""
    if unit == "bps":
        number /= 1000
    return max(1, int(round(number)))


@dataclass(frozen=True)
class ProfileDefinition:
    kind: str
    profile_token: str
    profile_name: str
    video_source_token: str
    video_source_config_token: str
    video_source_config_name: str
    video_encoder_token: str
    video_encoder_name: str
    audio_source_token: str
    audio_source_config_token: str
    audio_source_config_name: str
    audio_encoder_token: str
    audio_encoder_name: str
    width: int
    height: int
    framerate: int
    quality: int
    bitrate: int
    h264_profile: str


def profile_definition(camera, kind: str) -> ProfileDefinition:
    cam_id = camera.id
    if kind not in {"main", "sub"}:
        raise MediaProfileError("invalid-args", f"unknown profile kind: {kind}")

    if kind == "sub" and getattr(camera, "disable_substream", False):
        raise MediaProfileError("no-profile", "The requested profile token does not exist")

    if kind == "main":
        return ProfileDefinition(
            kind="main",
            profile_token=f"mainStream_{cam_id}",
            profile_name="mainStream",
            video_source_token=f"VideoSource_{cam_id}",
            video_source_config_token=f"VideoSource_{cam_id}",
            video_source_config_name="Video Source",
            video_encoder_token=f"VideoEncoderMain_{cam_id}",
            video_encoder_name="Main Video Encoder",
            audio_source_token=f"AudioSource_{cam_id}",
            audio_source_config_token=f"AudioSourceConfig_Main_{cam_id}",
            audio_source_config_name="Main Audio Source",
            audio_encoder_token=f"AudioEncoder_Main_{cam_id}",
            audio_encoder_name="Main Audio Encoder",
            width=int(camera.main_width),
            height=int(camera.main_height),
            framerate=int(camera.main_framerate),
            quality=5,
            bitrate=4096,
            h264_profile="Main",
        )

    return ProfileDefinition(
        kind="sub",
        profile_token=f"subStream_{cam_id}",
        profile_name="subStream",
        video_source_token=f"VideoSource_{cam_id}",
        video_source_config_token=f"VideoSource_{cam_id}",
        video_source_config_name="Video Source",
        video_encoder_token=f"VideoEncoderSub_{cam_id}",
        video_encoder_name="Sub Video Encoder",
        audio_source_token=f"AudioSource_{cam_id}",
        audio_source_config_token=f"AudioSourceConfig_Sub_{cam_id}",
        audio_source_config_name="Sub Audio Source",
        audio_encoder_token=f"AudioEncoder_Sub_{cam_id}",
        audio_encoder_name="Sub Audio Encoder",
        width=int(camera.sub_width),
        height=int(camera.sub_height),
        framerate=int(camera.sub_framerate),
        quality=3,
        bitrate=1024,
        h264_profile="Baseline",
    )


def profile_kind_from_token(camera, token: str | None) -> str:
    if not token:
        raise MediaProfileError("invalid-args", "ProfileToken is required")

    main = profile_definition(camera, "main")
    if token == main.profile_token:
        return "main"

    if not getattr(camera, "disable_substream", False):
        sub = profile_definition(camera, "sub")
        if token == sub.profile_token:
            return "sub"

    raise MediaProfileError("no-profile", "The requested profile token does not exist")


def encoder_kind_from_token(camera, token: str | None) -> str:
    if not token:
        raise MediaProfileError("invalid-args", "ConfigurationToken is required")

    main = profile_definition(camera, "main")
    if token == main.video_encoder_token:
        return "main"

    if not getattr(camera, "disable_substream", False):
        sub = profile_definition(camera, "sub")
        if token == sub.video_encoder_token:
            return "sub"

    raise MediaProfileError("no-config", "The requested configuration token does not exist")


def audio_parameters(camera, kind: str):
    if kind == "main":
        transcode = bool(getattr(camera, "transcode_main_audio", False))
        encoding = getattr(camera, "audio_encoding_main", "aac")
        sample_rate = getattr(camera, "audio_sample_rate_main", "8000")
        bitrate = getattr(camera, "audio_bitrate_main", "64k")
    else:
        transcode = bool(getattr(camera, "transcode_sub_audio", False))
        encoding = getattr(camera, "audio_encoding_sub", "aac")
        sample_rate = getattr(camera, "audio_sample_rate_sub", "8000")
        bitrate = getattr(camera, "audio_bitrate_sub", "64k")

    if not transcode:
        return {
            "encoding": "PCMU",
            "sample_rate": 8000,
            "bitrate": 64,
        }

    codec = str(encoding or "aac").strip().upper()
    return {
        "encoding": "AAC" if codec == "AAC" else codec,
        "sample_rate": parse_sample_rate(sample_rate, 8000),
        "bitrate": parse_bitrate(bitrate, 64),
    }


def render_video_source_configuration(camera, definition: ProfileDefinition) -> str:
    use_count = 1 if getattr(camera, "disable_substream", False) else 2
    # Both profiles intentionally reference one physical video-source
    # configuration. It must therefore be byte-for-byte consistent for both.
    return f"""<tt:VideoSourceConfiguration token="{escape(definition.video_source_config_token)}">
                    <tt:Name>{escape(definition.video_source_config_name)}</tt:Name>
                    <tt:UseCount>{use_count}</tt:UseCount>
                    <tt:SourceToken>{escape(definition.video_source_token)}</tt:SourceToken>
                    <tt:Bounds x="0" y="0" width="{int(camera.main_width)}" height="{int(camera.main_height)}"/>
                </tt:VideoSourceConfiguration>"""


def render_video_encoder_configuration(definition: ProfileDefinition) -> str:
    return f"""<tt:VideoEncoderConfiguration token="{escape(definition.video_encoder_token)}">
                    <tt:Name>{escape(definition.video_encoder_name)}</tt:Name>
                    <tt:UseCount>1</tt:UseCount>
                    <tt:Encoding>H264</tt:Encoding>
                    <tt:Resolution>
                        <tt:Width>{definition.width}</tt:Width>
                        <tt:Height>{definition.height}</tt:Height>
                    </tt:Resolution>
                    <tt:Quality>{definition.quality}</tt:Quality>
                    <tt:RateControl>
                        <tt:FrameRateLimit>{definition.framerate}</tt:FrameRateLimit>
                        <tt:EncodingInterval>1</tt:EncodingInterval>
                        <tt:BitrateLimit>{definition.bitrate}</tt:BitrateLimit>
                    </tt:RateControl>
                    <tt:H264>
                        <tt:GovLength>{definition.framerate}</tt:GovLength>
                        <tt:H264Profile>{escape(definition.h264_profile)}</tt:H264Profile>
                    </tt:H264>
                </tt:VideoEncoderConfiguration>"""


def render_audio_source_configuration(definition: ProfileDefinition) -> str:
    return f"""<tt:AudioSourceConfiguration token="{escape(definition.audio_source_config_token)}">
                    <tt:Name>{escape(definition.audio_source_config_name)}</tt:Name>
                    <tt:UseCount>1</tt:UseCount>
                    <tt:SourceToken>{escape(definition.audio_source_token)}</tt:SourceToken>
                </tt:AudioSourceConfiguration>"""


def render_audio_encoder_configuration(camera, definition: ProfileDefinition) -> str:
    audio = audio_parameters(camera, definition.kind)
    return f"""<tt:AudioEncoderConfiguration token="{escape(definition.audio_encoder_token)}">
                    <tt:Name>{escape(definition.audio_encoder_name)}</tt:Name>
                    <tt:UseCount>1</tt:UseCount>
                    <tt:Encoding>{escape(audio["encoding"])}</tt:Encoding>
                    <tt:Bitrate>{audio["bitrate"]}</tt:Bitrate>
                    <tt:SampleRate>{audio["sample_rate"]}</tt:SampleRate>
                </tt:AudioEncoderConfiguration>"""


def render_profile(camera, kind: str, *, response_element="trt:Profiles") -> str:
    definition = profile_definition(camera, kind)
    audio_enabled = bool(getattr(camera, "enable_audio", False))

    # Profile is an xs:sequence in onvif.xsd. Do not reorder these fragments.
    fragments = [
        f"<tt:Name>{escape(definition.profile_name)}</tt:Name>",
        render_video_source_configuration(camera, definition),
    ]
    if audio_enabled:
        fragments.append(render_audio_source_configuration(definition))

    fragments.append(render_video_encoder_configuration(definition))

    if audio_enabled:
        fragments.append(render_audio_encoder_configuration(camera, definition))

    return (
        f'<{response_element} token="{escape(definition.profile_token)}" fixed="true">'
        + "".join(fragments)
        + f"</{response_element}>"
    )


def render_get_profiles_response(camera) -> str:
    profiles = [render_profile(camera, "main")]
    if not getattr(camera, "disable_substream", False):
        profiles.append(render_profile(camera, "sub"))

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
                   xmlns:tt="http://www.onvif.org/ver10/schema">
    <SOAP-ENV:Body>
        <trt:GetProfilesResponse>
            {''.join(profiles)}
        </trt:GetProfilesResponse>
    </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""


def render_get_profile_response(camera, kind: str) -> str:
    profile = render_profile(camera, kind, response_element="trt:Profile")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
                   xmlns:tt="http://www.onvif.org/ver10/schema">
    <SOAP-ENV:Body>
        <trt:GetProfileResponse>
            {profile}
        </trt:GetProfileResponse>
    </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
