"""Device-service capability rendering for the unified ONVIF runtime.

Only services that actually have live endpoints are advertised. Keeping this in
one module prevents GetCapabilities, GetServices and GetServiceCapabilities from
contradicting one another.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET


DEVICE_NS = "http://www.onvif.org/ver10/device/wsdl"
MEDIA_NS = "http://www.onvif.org/ver10/media/wsdl"
EVENT_NS = "http://www.onvif.org/ver10/events/wsdl"


class DeviceRequestError(Exception):
    pass


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _parse_xml(body: str):
    try:
        return ET.fromstring(body)
    except ET.ParseError as exc:
        raise DeviceRequestError(f"invalid SOAP XML: {exc}") from exc


def capability_categories(soap_body: str):
    """Return requested ONVIF capability categories.

    Empty Category and All both mean all categories. Unsupported categories are
    preserved in the set but simply do not produce fabricated service blocks.
    """
    root = _parse_xml(soap_body)
    values = []
    for node in root.iter():
        if _local_name(node.tag) == "Category":
            value = (node.text or "").strip()
            if value:
                values.append(value)

    if not values or "All" in values:
        return {"Device", "Media", "Events"}
    return set(values)


def include_capability(soap_body: str) -> bool:
    root = _parse_xml(soap_body)
    for node in root.iter():
        if _local_name(node.tag) == "IncludeCapability":
            value = (node.text or "").strip().lower()
            return value in {"true", "1"}
    return False


def device_service_capabilities_xml(*, element="tds:Capabilities"):
    return (
        f"<{element}>"
        '<tds:Network IPFilter="false" ZeroConfiguration="false" '
        'IPVersion6="false" DynDNS="false" Dot11Configuration="false" '
        'Dot1XConfigurations="0" HostnameFromDHCP="false" NTP="0" DHCPv6="false"/>'
        '<tds:Security TLS1.0="false" TLS1.1="false" TLS1.2="false" '
        'OnboardKeyGeneration="false" AccessPolicyConfig="false" '
        'DefaultAccessPolicy="false" Dot1X="false" RemoteUserHandling="false" '
        'X.509Token="false" SAMLToken="false" KerberosToken="false" '
        'UsernameToken="true" HttpDigest="false" RELToken="false"/>'
        '<tds:System DiscoveryResolve="false" DiscoveryBye="false" '
        'RemoteDiscovery="false" SystemBackup="false" SystemLogging="false" '
        'FirmwareUpgrade="false" HttpFirmwareUpgrade="false" '
        'HttpSystemBackup="false" HttpSystemLogging="false" '
        'HttpSupportInformation="false"/>'
        f"</{element}>"
    )


def media_service_capabilities_xml(camera, *, element="trt:Capabilities"):
    max_profiles = 1 if getattr(camera, "disable_substream", False) else 2
    return (
        f'<{element} SnapshotUri="true" Rotation="false" '
        'VideoSourceMode="false" OSD="false">'
        f'<trt:ProfileCapabilities MaximumNumberOfProfiles="{max_profiles}"/>'
        '<trt:StreamingCapabilities RTPMulticast="false" RTP_TCP="true" '
        'RTP_RTSP_TCP="true" NonAggregateControl="false" NoRTSPStreaming="false"/>'
        f"</{element}>"
    )


def event_service_capabilities_xml(
    *,
    max_pullpoints=32,
    element="tev:Capabilities",
):
    return (
        f'<{element} WSSubscriptionPolicySupport="false" '
        'WSPullPointSupport="true" '
        'WSPausableSubscriptionManagerInterfaceSupport="false" '
        f'MaxNotificationProducers="1" MaxPullPoints="{int(max_pullpoints)}" '
        'PersistentNotificationStorage="false"/>'
    )


def render_get_device_service_capabilities():
    caps = device_service_capabilities_xml()
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:tds="{DEVICE_NS}">
    <SOAP-ENV:Body>
        <tds:GetServiceCapabilitiesResponse>
            {caps}
        </tds:GetServiceCapabilitiesResponse>
    </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""


def render_get_capabilities(
    camera,
    local_ip: str,
    categories,
):
    port = camera.onvif_port
    blocks = []

    if "Device" in categories:
        blocks.append(f"""
                <tt:Device>
                    <tt:XAddr>http://{local_ip}:{port}/onvif/device_service</tt:XAddr>
                    <tt:Network>
                        <tt:IPFilter>false</tt:IPFilter>
                        <tt:ZeroConfiguration>false</tt:ZeroConfiguration>
                        <tt:IPVersion6>false</tt:IPVersion6>
                        <tt:DynDNS>false</tt:DynDNS>
                    </tt:Network>
                    <tt:System>
                        <tt:DiscoveryResolve>false</tt:DiscoveryResolve>
                        <tt:DiscoveryBye>false</tt:DiscoveryBye>
                        <tt:RemoteDiscovery>false</tt:RemoteDiscovery>
                        <tt:SystemBackup>false</tt:SystemBackup>
                        <tt:SystemLogging>false</tt:SystemLogging>
                        <tt:FirmwareUpgrade>false</tt:FirmwareUpgrade>
                        <tt:SupportedVersions>
                            <tt:Major>2</tt:Major>
                            <tt:Minor>5</tt:Minor>
                        </tt:SupportedVersions>
                    </tt:System>
                    <tt:IO>
                        <tt:InputConnectors>0</tt:InputConnectors>
                        <tt:RelayOutputs>0</tt:RelayOutputs>
                    </tt:IO>
                    <tt:Security>
                        <tt:TLS1.1>false</tt:TLS1.1>
                        <tt:TLS1.2>false</tt:TLS1.2>
                        <tt:OnboardKeyGeneration>false</tt:OnboardKeyGeneration>
                        <tt:AccessPolicyConfig>false</tt:AccessPolicyConfig>
                        <tt:X.509Token>false</tt:X.509Token>
                        <tt:SAMLToken>false</tt:SAMLToken>
                        <tt:KerberosToken>false</tt:KerberosToken>
                        <tt:RELToken>false</tt:RELToken>
                    </tt:Security>
                </tt:Device>""")

    if "Events" in categories:
        blocks.append(f"""
                <tt:Events>
                    <tt:XAddr>http://{local_ip}:{port}/onvif/events_service</tt:XAddr>
                    <tt:WSSubscriptionPolicySupport>false</tt:WSSubscriptionPolicySupport>
                    <tt:WSPullPointSupport>true</tt:WSPullPointSupport>
                    <tt:WSPausableSubscriptionManagerInterfaceSupport>false</tt:WSPausableSubscriptionManagerInterfaceSupport>
                </tt:Events>""")

    if "Media" in categories:
        max_profiles = 1 if getattr(camera, "disable_substream", False) else 2
        blocks.append(f"""
                <tt:Media>
                    <tt:XAddr>http://{local_ip}:{port}/onvif/media_service</tt:XAddr>
                    <tt:StreamingCapabilities>
                        <tt:RTPMulticast>false</tt:RTPMulticast>
                        <tt:RTP_TCP>true</tt:RTP_TCP>
                        <tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP>
                        <tt:NonAggregateControl>false</tt:NonAggregateControl>
                        <tt:NoRTSPStreaming>false</tt:NoRTSPStreaming>
                    </tt:StreamingCapabilities>
                    <tt:Extension>
                        <tt:ProfileCapabilities>
                            <tt:MaximumNumberOfProfiles>{max_profiles}</tt:MaximumNumberOfProfiles>
                        </tt:ProfileCapabilities>
                    </tt:Extension>
                </tt:Media>""")

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:tds="{DEVICE_NS}"
                   xmlns:tt="http://www.onvif.org/ver10/schema">
    <SOAP-ENV:Body>
        <tds:GetCapabilitiesResponse>
            <tds:Capabilities>{''.join(blocks)}
            </tds:Capabilities>
        </tds:GetCapabilitiesResponse>
    </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""


def _service_xml(
    *,
    namespace,
    xaddr,
    capability_xml=None,
):
    capabilities = (
        f"<tds:Capabilities>{capability_xml}</tds:Capabilities>"
        if capability_xml
        else ""
    )
    return f"""
            <tds:Service>
                <tds:Namespace>{namespace}</tds:Namespace>
                <tds:XAddr>{xaddr}</tds:XAddr>
                {capabilities}
                <tds:Version>
                    <tt:Major>2</tt:Major>
                    <tt:Minor>5</tt:Minor>
                </tds:Version>
            </tds:Service>"""


def render_get_services(
    camera,
    local_ip: str,
    *,
    include_capabilities=False,
    max_pullpoints=32,
):
    port = camera.onvif_port

    device_caps = None
    media_caps = None
    event_caps = None
    if include_capabilities:
        device_caps = device_service_capabilities_xml()
        media_caps = media_service_capabilities_xml(camera)
        event_caps = event_service_capabilities_xml(max_pullpoints=max_pullpoints)

    services = [
        _service_xml(
            namespace=DEVICE_NS,
            xaddr=f"http://{local_ip}:{port}/onvif/device_service",
            capability_xml=device_caps,
        ),
        _service_xml(
            namespace=MEDIA_NS,
            xaddr=f"http://{local_ip}:{port}/onvif/media_service",
            capability_xml=media_caps,
        ),
        _service_xml(
            namespace=EVENT_NS,
            xaddr=f"http://{local_ip}:{port}/onvif/events_service",
            capability_xml=event_caps,
        ),
    ]

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:tds="{DEVICE_NS}"
                   xmlns:trt="{MEDIA_NS}"
                   xmlns:tev="{EVENT_NS}"
                   xmlns:tt="http://www.onvif.org/ver10/schema">
    <SOAP-ENV:Body>
        <tds:GetServicesResponse>
            {''.join(services)}
        </tds:GetServicesResponse>
    </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
