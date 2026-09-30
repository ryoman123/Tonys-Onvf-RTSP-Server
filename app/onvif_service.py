
import json
import time
import socket
import struct
import threading
from pathlib import Path
from flask import Flask, request, Response
from flask_cors import CORS
from datetime import datetime, timezone, timedelta
import sys
import os
import tempfile
from xml.sax.saxutils import escape
from urllib.parse import quote
from .ffmpeg_manager import FFmpegManager
from .device_capabilities import (
    DeviceRequestError,
    capability_categories,
    include_capability,
    render_get_capabilities,
    render_get_device_service_capabilities,
    render_get_services,
)
from .media_profile import (
    MediaProfileError,
    extract_request_text as extract_media_request_text,
    encoder_kind_from_token,
    profile_definition,
    profile_kind_from_token,
    render_get_profile_response,
    render_get_profiles_response,
    render_video_encoder_configuration,
    validate_stream_setup,
)
from .event_engine import (
    CONCRETE_SET_DIALECT,
    CONCRETE_TOPIC_DIALECT,
    DEFAULT_TOPICS,
    EventEngine,
    EventSubscriptionError,
    extract_xml_text,
    parse_message_limit,
    parse_pull_timeout_seconds,
    parse_topic_filter,
    render_notification_message,
    render_topic_set,
    resolve_termination_seconds,
)

# Try to import zoneinfo
if sys.version_info >= (3, 9):
    try:
        import zoneinfo
    except ImportError:
        # Fallback will be handled in _get_system_date_time
        pass
else:
    try:
        from backports import zoneinfo
    except ImportError:
        pass

from .config import CONFIG_FILE
from .utils import get_local_ip

class ONVIFService:
    def __init__(self, camera):
        self.camera = camera
        self.app = None
        # Cache for authenticated IPs: {ip: timestamp}
        # Prevents repetitive 401 challenges for recently authenticated clients (30 min TTL)
        self.auth_cache = {}
        self.event_engine = EventEngine()
        # Compatibility alias used by Tony's existing diagnostics/UI.
        self.subscriptions = self.event_engine.subscriptions
        self._discovery_thread = None
        self._discovery_stop_event = threading.Event()
        
    def create_app(self):
        """Create the Flask app for ONVIF service"""
        app = Flask(f"onvif_camera_{self.camera.id}")
        CORS(app)
        self.app = app
        
        # Disable Flask logging if not in debug mode
        import logging
        log = logging.getLogger('werkzeug')
        if getattr(self.camera, 'debug_mode', False):
            log.setLevel(logging.INFO)
        else:
            log.setLevel(logging.ERROR)
        
        # Disable Flask development server warnings
        import os
        os.environ['FLASK_ENV'] = 'production'
        
        # Get correct local IP for ONVIF URLs (Use camera's effective IP)
        local_ip = self.camera.get_effective_ip()
        
        # Authentication decorator for ONVIF endpoints
        def require_auth(f):
            from functools import wraps
            @wraps(f)
            def decorated(*args, **kwargs):
                client_ip = request.remote_addr

                # Check for IP whitelist bypass
                if self.camera.manager and self.camera.manager.is_ip_whitelisted(request.remote_addr):
                    if getattr(self.camera, 'debug_mode', False):
                        print(f"  [ONVIF] Auth bypass for whitelisted IP: {client_ip}")
                    return f(*args, **kwargs)
                
                current_time = time.time()
                
                # Check if IP is in auth cache (30 minute TTL)
                if client_ip in self.auth_cache:
                    if current_time - self.auth_cache[client_ip] < 1800:  # 30 minutes
                        return f(*args, **kwargs)
                    else:
                        # Expired, remove from cache
                        del self.auth_cache[client_ip]
                
                # Check for Basic Auth
                auth = request.authorization
                if auth and auth.username == self.camera.onvif_username and auth.password == self.camera.onvif_password:
                    # Cache successful authentication
                    self.auth_cache[client_ip] = current_time
                    return f(*args, **kwargs)
                
                # Check for SOAP WS-UsernameToken (in request body)
                data = request.get_data(as_text=True)
                
                if 'UsernameToken' in data:
                    import re, hashlib, base64

                    # Extract username (supports wsse: namespace prefix or none)
                    user_match = re.search(r'(?:<|:)Username>(.*?)</', data)
                    token_user = user_match.group(1).strip() if user_match else ''
                    has_user = (token_user == self.camera.onvif_username)

                    auth_ok = False
                    if has_user:
                        # Check if this is a PasswordDigest request
                        if 'PasswordDigest' in data:
                            try:
                                digest_match  = re.search(r'(?:<|:)Password[^>]*>(.*?)</', data)
                                nonce_match   = re.search(r'(?:<|:)Nonce[^>]*>(.*?)</', data)
                                created_match = re.search(r'(?:<|:)Created[^>]*>(.*?)</', data)
                                if digest_match and nonce_match and created_match:
                                    client_digest = digest_match.group(1).strip()
                                    nonce_b64     = nonce_match.group(1).strip()
                                    created       = created_match.group(1).strip()
                                    nonce_bytes   = base64.b64decode(nonce_b64)
                                    raw           = nonce_bytes + created.encode('utf-8') + self.camera.onvif_password.encode('utf-8')
                                    expected      = base64.b64encode(hashlib.sha1(raw).digest()).decode('utf-8')
                                    auth_ok       = (client_digest == expected)
                            except Exception:
                                auth_ok = False
                        else:
                            # PasswordText (cleartext) fallback
                            has_pass = f'>{self.camera.onvif_password}</' in data and \
                                       ('<Password' in data or ':Password' in data)
                            auth_ok = has_pass

                    if auth_ok:
                        # Cache successful authentication
                        self.auth_cache[client_ip] = current_time
                        return f(*args, **kwargs)
                
                # Authentication failed - return 401
                return Response(
                    'Authentication required', 401,
                    {'WWW-Authenticate': 'Basic realm="ONVIF"'}
                )
            return decorated
        
        # ONVIF Device Management
        @app.route('/onvif/device_service', methods=['GET', 'POST'], endpoint=f'device_service_{self.camera.id}')
        @require_auth
        def device_service():
            try:
                if request.method == 'GET':
                    return self._get_device_wsdl()
                
                # Parse SOAP request
                soap_body = request.data.decode('utf-8')
                
                # GetDeviceInformation
                if 'GetDeviceInformation' in soap_body:
                    return self._handle_get_device_info()

                # Service-specific capabilities must be checked before the
                # generic GetCapabilities action.
                elif 'GetServiceCapabilities' in soap_body:
                    return self._handle_get_device_service_capabilities()

                # GetCapabilities
                elif 'GetCapabilities' in soap_body:
                    return self._handle_get_capabilities(local_ip)
                
                # GetServices
                elif 'GetServices' in soap_body:
                    return self._handle_get_services(local_ip)
                
                # GetSystemDateAndTime
                elif 'GetSystemDateAndTime' in soap_body:
                    return self._handle_get_system_date_time()
                
                # GetScopes
                elif 'GetScopes' in soap_body:
                    return self._handle_get_scopes()
                
                # GetNetworkInterfaces
                elif 'GetNetworkInterfaces' in soap_body:
                    return self._handle_get_network_interfaces()

                return self._soap_fault(
                    'ter:ActionNotSupported',
                    'Unsupported Device service action'
                )
                
            except Exception as e:
                print(f"  Error handling request: {e}")
                import traceback
                traceback.print_exc()
                return Response("Internal Server Error", status=500)
            
        # Root Route: Handle ONVIF Device Service at the root for convenience
        @app.route('/', methods=['GET', 'POST'], endpoint=f'root_service_{self.camera.id}')
        @require_auth
        def root_service():
            return device_service()

        # ONVIF Snapshot Endpoint (Used by GetSnapshotUri)
        @app.route('/onvif/snapshot', methods=['GET'], endpoint=f'snapshot_{self.camera.id}')
        @require_auth
        def snapshot():
            """Capture and return a real-time snapshot for ONVIF"""
            # Capture from the MAIN stream so the snapshot dimensions match the
            # advertised main resolution. Serving sub-resolution snapshots made
            # NVRs (UniFi Protect) flap the camera's resolution class (issue #42).
            stream_path = f"{self.camera.path_name}_main"
            
            # Construct local MediaMTX URL
            rtsp_port = self.camera.rtsp_port
            if getattr(self.camera.manager, 'rtsp_auth_enabled', False):
                user = quote(getattr(self.camera.manager, 'global_username', 'admin'))
                pw = quote(getattr(self.camera.manager, 'global_password', 'admin'))
                stream_url = f"rtsp://{user}:{pw}@localhost:{rtsp_port}/{stream_path}"
            else:
                stream_url = f"rtsp://localhost:{rtsp_port}/{stream_path}"
            
            ffmpeg_mgr = FFmpegManager()
            
            # Create a temp file
            fd, path = tempfile.mkstemp(suffix='.jpg')
            os.close(fd)
            
            try:
                success, error = ffmpeg_mgr.capture_snapshot(stream_url, path)
                if not success:
                    # Fallback to direct camera main URL if MediaMTX failed
                    success, error = ffmpeg_mgr.capture_snapshot(self.camera.main_stream_url, path)
                if not success and self.camera.sub_stream_url and not getattr(self.camera, 'disable_substream', False):
                    # Last resort: sub stream (wrong resolution beats no snapshot)
                    success, error = ffmpeg_mgr.capture_snapshot(self.camera.sub_stream_url, path)
                
                if success:
                    with open(path, 'rb') as f:
                        content = f.read()
                    
                    from flask import make_response
                    response = make_response(content)
                    response.headers['Content-Type'] = 'image/jpeg'
                    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
                    return response
                else:
                    return Response(f"Snapshot capture failed: {error}", status=500)
                    
            except Exception as e:
                return Response(f"Error: {str(e)}", status=500)
            finally:
                if os.path.exists(path):
                    try: os.remove(path)
                    except: pass

        # ONVIF Media Service
        @app.route('/onvif/media_service', methods=['GET', 'POST'], endpoint=f'media_service_{self.camera.id}')
        @require_auth
        def media_service():
            try:
                if request.method == 'GET':
                    return self._get_media_wsdl()
                
                # Parse SOAP request
                soap_body = request.data.decode('utf-8')

                # Order matters: longer action names must be checked before their
                # prefixes (GetProfiles before GetProfile, ...ConfigurationOptions
                # before ...Configurations before ...Configuration).

                # GetProfiles
                if 'GetProfiles' in soap_body:
                    return self._handle_get_profiles()

                # GetProfile (singular, by token)
                elif 'GetProfile' in soap_body:
                    return self._handle_get_profile()

                # GetStreamUri
                elif 'GetStreamUri' in soap_body:
                    return self._handle_get_stream_uri(local_ip)

                # GetSnapshotUri
                elif 'GetSnapshotUri' in soap_body:
                    return self._handle_get_snapshot_uri(local_ip)

                # GetVideoSources
                elif 'GetVideoSources' in soap_body:
                    return self._handle_get_video_sources()

                # GetAudioSources (empty list when audio is disabled)
                elif 'GetAudioSources' in soap_body:
                    if getattr(self.camera, 'enable_audio', False):
                        return self._handle_get_audio_sources()
                    return self._handle_empty_media_response('GetAudioSources')

                # GetAudioEncoderConfigurations
                elif 'GetAudioEncoderConfigurations' in soap_body:
                    if getattr(self.camera, 'enable_audio', False):
                        return self._handle_get_audio_encoder_configs()
                    return self._handle_empty_media_response('GetAudioEncoderConfigurations')

                # GetAudioSourceConfigurations
                elif 'GetAudioSourceConfigurations' in soap_body:
                    if getattr(self.camera, 'enable_audio', False):
                        return self._handle_get_audio_source_configs()
                    return self._handle_empty_media_response('GetAudioSourceConfigurations')

                # GetVideoEncoderConfigurationOptions
                elif 'GetVideoEncoderConfigurationOptions' in soap_body:
                    return self._handle_get_video_encoder_config_options()

                # GetVideoEncoderConfigurations
                elif 'GetVideoEncoderConfigurations' in soap_body:
                    return self._handle_get_video_encoder_configs()

                # GetVideoEncoderConfiguration (singular, by token)
                elif 'GetVideoEncoderConfiguration' in soap_body:
                    return self._handle_get_video_encoder_config()

                # GetVideoSourceConfigurationOptions
                elif 'GetVideoSourceConfigurationOptions' in soap_body:
                    return self._handle_get_video_source_config_options()

                # GetVideoSourceConfigurations
                elif 'GetVideoSourceConfigurations' in soap_body:
                    return self._handle_get_video_source_configs()

                # GetVideoSourceConfiguration (singular, by token)
                elif 'GetVideoSourceConfiguration' in soap_body:
                    return self._handle_get_video_source_config()

                # GetServiceCapabilities
                elif 'GetServiceCapabilities' in soap_body:
                    return self._handle_get_media_service_capabilities()

                # Unknown action: return a proper SOAP fault. Answering with
                # GetProfiles data here made NVRs parse the wrong profile's
                # resolution (UniFi Protect HD/4K flapping, issue #42).
                return self._soap_fault()
                
            except Exception as e:
                print(f"  Error in media service: {e}")
                import traceback
                traceback.print_exc()
                return Response("Internal Server Error", status=500)

        # ONVIF Events Service
        @app.route('/onvif/events_service', methods=['GET', 'POST'], endpoint=f'events_service_{self.camera.id}')
        @require_auth
        def events_service():
            try:
                if request.method == 'GET':
                    return self._get_events_wsdl()
                
                soap_body = request.data.decode('utf-8')
                
                if 'CreatePullPointSubscription' in soap_body:
                    return self._handle_create_pull_point_subscription(local_ip)
                elif 'GetEventProperties' in soap_body:
                    return self._handle_get_event_properties()
                elif 'GetServiceCapabilities' in soap_body:
                    return self._handle_get_event_service_capabilities()

                return self._soap_fault(
                    'ter:ActionNotSupported',
                    'Unsupported Events service action'
                )
            except Exception as e:
                print(f"  Error in events service: {e}")
                import traceback
                traceback.print_exc()
                return Response("Internal Server Error", status=500)

        # ONVIF Subscription Service
        @app.route('/onvif/subscription/<sub_id>', methods=['POST'], endpoint=f'subscription_service_{self.camera.id}')
        @require_auth
        def subscription_service(sub_id):
            try:
                soap_body = request.data.decode('utf-8')
                
                if 'PullMessages' in soap_body:
                    return self._handle_pull_messages(sub_id)
                elif 'SetSynchronizationPoint' in soap_body:
                    return self._handle_set_synchronization_point(sub_id)
                elif 'Unsubscribe' in soap_body:
                    return self._handle_unsubscribe(sub_id)
                elif 'Renew' in soap_body:
                    return self._handle_renew_subscription(sub_id)

                return self._soap_fault(
                    'ter:ActionNotSupported',
                    'Unsupported PullPoint subscription action'
                )
            except Exception as e:
                print(f"  Error in subscription service: {e}")
                import traceback
                traceback.print_exc()
                return Response("Internal Server Error", status=500)
                
        return app

    def start_discovery_service(self, local_ip):
        """Start WS-Discovery multicast service for ONVIF discovery."""
        if self._discovery_thread and self._discovery_thread.is_alive():
            return

        self._discovery_stop_event.clear()

        def discovery_responder():
            MCAST_GRP = '239.255.255.250'
            MCAST_PORT = 3702
            
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(2.0)
            
            try:
                sock.bind(('', MCAST_PORT))
                try:
                    mreq = struct.pack('4s4s', socket.inet_aton(MCAST_GRP), socket.inet_aton(local_ip))
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                except Exception:
                    mreq = struct.pack('4sl', socket.inet_aton(MCAST_GRP), socket.INADDR_ANY)
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                print(f"  WS-Discovery listener started for {self.camera.name} on {local_ip}:{self.camera.onvif_port}")
            except Exception as e:
                print(f"  Discovery service error: {e}")
                print(f"  You can still add camera manually in ODM: {local_ip}:{self.camera.onvif_port}")
                return
            
            while self.camera.status == "running" and not self._discovery_stop_event.is_set():
                try:
                    data, addr = sock.recvfrom(10240)
                    message = data.decode('utf-8', errors='ignore')
                    
                    # Respond to any Probe request
                    if 'Probe' in message or 'probe' in message.lower():
                        # Extract MessageID if present
                        msg_id = "uuid:probe-request"
                        if 'MessageID' in message:
                            try:
                                start = message.find('<a:MessageID>') + 13
                                end = message.find('</a:MessageID>')
                                if start > 12 and end > start:
                                    msg_id = message[start:end]
                            except:
                                pass
                        
                        identity = self.camera.get_identity_manifest()
                        discovery_scopes = " ".join(identity["scopes"])
                        response = f'''<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:SOAP-ENC="http://www.w3.org/2003/05/soap-encoding"
                   xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"
                   xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
                   xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
    <SOAP-ENV:Header>
        <wsa:MessageID>uuid:{self.camera.uuid}-{time.time_ns()}</wsa:MessageID>
        <wsa:RelatesTo>{escape(msg_id)}</wsa:RelatesTo>
        <wsa:To SOAP-ENV:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</wsa:To>
        <wsa:Action SOAP-ENV:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatches</wsa:Action>
    </SOAP-ENV:Header>
    <SOAP-ENV:Body>
        <d:ProbeMatches>
            <d:ProbeMatch>
                <wsa:EndpointReference>
                    <wsa:Address>urn:uuid:{identity["uuid"]}</wsa:Address>
                </wsa:EndpointReference>
                <d:Types>dn:NetworkVideoTransmitter</d:Types>
                <d:Scopes>{escape(discovery_scopes)}</d:Scopes>
                <d:XAddrs>http://{local_ip}:{self.camera.onvif_port}/onvif/device_service</d:XAddrs>
                <d:MetadataVersion>1</d:MetadataVersion>
            </d:ProbeMatch>
        </d:ProbeMatches>
    </SOAP-ENV:Body>
</SOAP-ENV:Envelope>'''
                        
                        try:
                            # Create a temporary socket bound to the virtual IP to send the response.
                            # This ensures the UDP packet has the correct virtual IP as the source.
                            reply_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                            try:
                                reply_sock.bind((local_ip, 0))
                                reply_sock.sendto(response.encode('utf-8'), addr)
                            finally:
                                reply_sock.close()
                        except Exception as e:
                            print(f"  Failed to send discovery response from {local_ip}: {e}")
                        
                except socket.timeout:
                    continue
                except Exception as e:
                    if self.camera.status == "running":
                        print(f"  Discovery error: {e}")
                    break
            
            try:
                sock.close()
            except:
                pass
        
        # Start discovery thread and store reference
        self._discovery_thread = threading.Thread(
            target=discovery_responder,
            daemon=True,
            name=f"ws-discovery-{self.camera.path_name}"
        )
        self._discovery_thread.start()

    def stop_discovery_service(self):
        """Stop WS-Discovery and wait for its bounded socket timeout."""
        self._discovery_stop_event.set()
        thread = self._discovery_thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2.5)
            if thread.is_alive():
                raise RuntimeError(
                    f"WS-Discovery thread for {self.camera.name} did not stop within 2.5 seconds"
                )
        self._discovery_thread = None

    def _handle_get_device_info(self):
        """Return the same persisted identity used by discovery and scopes."""
        identity = self.camera.get_identity_manifest()
        soap_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:tds="http://www.onvif.org/ver10/device/wsdl">
    <SOAP-ENV:Body>
        <tds:GetDeviceInformationResponse>
            <tds:Manufacturer>{escape(identity["manufacturer"])}</tds:Manufacturer>
            <tds:Model>{escape(identity["model"])}</tds:Model>
            <tds:FirmwareVersion>{escape(identity["firmwareVersion"])}</tds:FirmwareVersion>
            <tds:SerialNumber>{escape(identity["serialNumber"])}</tds:SerialNumber>
            <tds:HardwareId>{escape(identity["hardwareId"])}</tds:HardwareId>
        </tds:GetDeviceInformationResponse>
    </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        return Response(soap_response, mimetype='application/soap+xml')

    def _handle_get_device_service_capabilities(self):
        """Return Device-service capabilities using the same truth source as GetServices."""
        soap_response = render_get_device_service_capabilities()
        return Response(soap_response, mimetype='application/soap+xml')

    def _handle_get_capabilities(self, local_ip):
        """Return only capability categories backed by live service endpoints."""
        soap_body = request.data.decode('utf-8')
        try:
            categories = capability_categories(soap_body)
        except DeviceRequestError as error:
            return self._soap_fault('ter:InvalidArgs', str(error))

        soap_response = render_get_capabilities(
            self.camera,
            local_ip,
            categories,
        )
        return Response(soap_response, mimetype='application/soap+xml')
    def _handle_get_services(self, local_ip):
        """Describe the three services this runtime actually implements."""
        soap_body = request.data.decode('utf-8')
        try:
            with_capabilities = include_capability(soap_body)
        except DeviceRequestError as error:
            return self._soap_fault('ter:InvalidArgs', str(error))

        soap_response = render_get_services(
            self.camera,
            local_ip,
            include_capabilities=with_capabilities,
            max_pullpoints=self.event_engine.max_pullpoints,
        )
        return Response(soap_response, mimetype='application/soap+xml')
    def _handle_get_system_date_time(self):
        """Handle GetSystemDateAndTime request - Always uses UTC"""
        now = datetime.now(timezone.utc)
        utc_now = now
        tz_string = "UTC"
        
        soap_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:tds="http://www.onvif.org/ver10/device/wsdl"
                   xmlns:tt="http://www.onvif.org/ver10/schema">
    <SOAP-ENV:Body>
        <tds:GetSystemDateAndTimeResponse>
            <tds:SystemDateAndTime>
                <tt:DateTimeType>NTP</tt:DateTimeType>
                <tt:DaylightSavings>false</tt:DaylightSavings>
                <tt:TimeZone>
                    <tt:TZ>{tz_string}</tt:TZ>
                </tt:TimeZone>
                <tt:UTCDateTime>
                    <tt:Time>
                        <tt:Hour>{utc_now.hour}</tt:Hour>
                        <tt:Minute>{utc_now.minute}</tt:Minute>
                        <tt:Second>{utc_now.second}</tt:Second>
                    </tt:Time>
                    <tt:Date>
                        <tt:Year>{utc_now.year}</tt:Year>
                        <tt:Month>{utc_now.month}</tt:Month>
                        <tt:Day>{utc_now.day}</tt:Day>
                    </tt:Date>
                </tt:UTCDateTime>
                <tt:LocalDateTime>
                    <tt:Time>
                        <tt:Hour>{now.hour}</tt:Hour>
                        <tt:Minute>{now.minute}</tt:Minute>
                        <tt:Second>{now.second}</tt:Second>
                    </tt:Time>
                    <tt:Date>
                        <tt:Year>{now.year}</tt:Year>
                        <tt:Month>{now.month}</tt:Month>
                        <tt:Day>{now.day}</tt:Day>
                    </tt:Date>
                </tt:LocalDateTime>
            </tds:SystemDateAndTime>
        </tds:GetSystemDateAndTimeResponse>
    </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        
        return Response(soap_response, mimetype='application/soap+xml')

    def _handle_get_network_interfaces(self):
        """Handle GetNetworkInterfaces request"""
        mac = self.camera.mac_address
        
        soap_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:tds="http://www.onvif.org/ver10/device/wsdl"
                   xmlns:tt="http://www.onvif.org/ver10/schema">
    <SOAP-ENV:Body>
        <tds:GetNetworkInterfacesResponse>
            <tds:NetworkInterfaces token="eth0">
                <tt:Enabled>true</tt:Enabled>
                <tt:Info>
                    <tt:Name>Ethernet0</tt:Name>
                    <tt:HwAddress>{mac}</tt:HwAddress>
                    <tt:MTU>1500</tt:MTU>
                </tt:Info>
                <tt:IPv4>
                    <tt:Enabled>true</tt:Enabled>
                    <tt:Config>
                        <tt:Manual>
                            <tt:Address>{self.camera.assigned_ip if self.camera.assigned_ip else '0.0.0.0'}</tt:Address>
                            <tt:PrefixLength>24</tt:PrefixLength>
                        </tt:Manual>
                        <tt:DHCP>{'true' if self.camera.ip_mode == 'dhcp' else 'false'}</tt:DHCP>
                    </tt:Config>
                </tt:IPv4>
            </tds:NetworkInterfaces>
        </tds:GetNetworkInterfacesResponse>
    </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        
        return Response(soap_response, mimetype='application/soap+xml')

    def _handle_get_profiles(self):
        """Return schema-ordered media profiles.

        The Profile type is an xs:sequence. Keeping construction in
        media_profile.py prevents audio-enabled responses from placing
        VideoEncoderConfiguration before AudioSourceConfiguration, which strict
        ONVIF clients reject during deserialization.
        """
        soap_response = render_get_profiles_response(self.camera)
        return Response(soap_response, mimetype='application/soap+xml')
    def _handle_get_stream_uri(self, local_ip):
        """Return the RTSP URI for one exact, validated ProfileToken."""
        soap_body = request.data.decode('utf-8')
        try:
            profile_token = extract_media_request_text(soap_body, 'ProfileToken')
            kind = profile_kind_from_token(self.camera, profile_token)
            validate_stream_setup(soap_body)
        except MediaProfileError as error:
            return self._media_profile_fault(error)

        stream_path = f"{self.camera.path_name}_{'sub' if kind == 'sub' else 'main'}"

        soap_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
                   xmlns:tt="http://www.onvif.org/ver10/schema">
    <SOAP-ENV:Body>
        <trt:GetStreamUriResponse>
            <trt:MediaUri>
                <tt:Uri>rtsp://{local_ip}:{self.camera.rtsp_port}/{stream_path}</tt:Uri>
                <tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>
                <tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>
                <tt:Timeout>PT0S</tt:Timeout>
            </trt:MediaUri>
        </trt:GetStreamUriResponse>
    </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        return Response(soap_response, mimetype='application/soap+xml')
    def _handle_get_snapshot_uri(self, local_ip):
        """Return the snapshot URI only for an existing media profile."""
        soap_body = request.data.decode('utf-8')
        try:
            profile_token = extract_media_request_text(soap_body, 'ProfileToken')
            profile_kind_from_token(self.camera, profile_token)
        except MediaProfileError as error:
            return self._media_profile_fault(error)

        soap_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
                   xmlns:tt="http://www.onvif.org/ver10/schema">
    <SOAP-ENV:Body>
        <trt:GetSnapshotUriResponse>
            <trt:MediaUri>
                <tt:Uri>http://{local_ip}:{self.camera.onvif_port}/onvif/snapshot</tt:Uri>
                <tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>
                <tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>
                <tt:Timeout>PT0S</tt:Timeout>
            </trt:MediaUri>
        </trt:GetSnapshotUriResponse>
    </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        return Response(soap_response, mimetype='application/soap+xml')
    def _get_device_wsdl(self):
        """Return device service WSDL"""
        local_ip = self.camera.get_effective_ip()
        
        wsdl = f"""<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="http://schemas.xmlsoap.org/wsdl/"
             xmlns:tds="http://www.onvif.org/ver10/device/wsdl"
             xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap12/"
             targetNamespace="http://www.onvif.org/ver10/device/wsdl">
    <service name="DeviceService">
        <port name="DevicePort" binding="tds:DeviceBinding">
            <soap:address location="http://{local_ip}:{self.camera.onvif_port}/"/>
        </port>
    </service>
</definitions>"""
        return Response(wsdl, mimetype='text/xml')

    def _get_media_wsdl(self):
        """Return media service WSDL"""
        local_ip = self.camera.get_effective_ip()
        
        wsdl = f"""<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="http://schemas.xmlsoap.org/wsdl/"
             xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
             xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap12/"
             targetNamespace="http://www.onvif.org/ver10/media/wsdl">
    <service name="MediaService">
        <port name="MediaPort" binding="trt:MediaBinding">
            <soap:address location="http://{local_ip}:{self.camera.onvif_port}/onvif/media_service"/>
        </port>
    </service>
</definitions>"""
        return Response(wsdl, mimetype='text/xml')

    def _handle_get_video_sources(self):
        """Handle GetVideoSources request with unique tokens"""
        cam_id = self.camera.id
        soap_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
                   xmlns:tt="http://www.onvif.org/ver10/schema">
    <SOAP-ENV:Body>
        <trt:GetVideoSourcesResponse>
            <trt:VideoSources token="VideoSource_{cam_id}">
                <tt:Framerate>{self.camera.main_framerate}</tt:Framerate>
                <tt:Resolution>
                    <tt:Width>{self.camera.main_width}</tt:Width>
                    <tt:Height>{self.camera.main_height}</tt:Height>
                </tt:Resolution>
            </trt:VideoSources>
        </trt:GetVideoSourcesResponse>
    </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        
        return Response(soap_response, mimetype='application/soap+xml')

    def _handle_get_audio_sources(self):
        """Handle GetAudioSources request with unique tokens"""
        cam_id = self.camera.id
        soap_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
                   xmlns:tt="http://www.onvif.org/ver10/schema">
    <SOAP-ENV:Body>
        <trt:GetAudioSourcesResponse>
            <trt:AudioSources token="AudioSource_{cam_id}">
                <tt:Channels>1</tt:Channels>
            </trt:AudioSources>
        </trt:GetAudioSourcesResponse>
    </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        
        return Response(soap_response, mimetype='application/soap+xml')

    def _handle_get_audio_encoder_configs(self):
        """Handle GetAudioEncoderConfigurations request"""
        cam_id = self.camera.id
        
        # Determine actual audio encoding for ONVIF reporting
        audio_enc = "PCMU"
        audio_rate = 8000
        audio_bitrate = 64
        if getattr(self.camera, 'transcode_main_audio', False):
            codec = getattr(self.camera, 'audio_encoding_main', 'aac').upper()
            audio_enc = "AAC" if codec == "AAC" else codec
            audio_rate = int(str(getattr(self.camera, 'audio_sample_rate_main', '8000')).replace('khz', '000').replace('Hz', ''))
            audio_bitrate = int(str(getattr(self.camera, 'audio_bitrate_main', '64k')).replace('k', '').replace('kbps', ''))

        audio_enc_sub = "PCMU"
        audio_rate_sub = 8000
        audio_bitrate_sub = 64
        if getattr(self.camera, 'transcode_sub_audio', False):
            codec = getattr(self.camera, 'audio_encoding_sub', 'aac').upper()
            audio_enc_sub = "AAC" if codec == "AAC" else codec
            audio_rate_sub = int(str(getattr(self.camera, 'audio_sample_rate_sub', '8000')).replace('khz', '000').replace('Hz', ''))
            audio_bitrate_sub = int(str(getattr(self.camera, 'audio_bitrate_sub', '64k')).replace('k', '').replace('kbps', ''))

        soap_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
                   xmlns:tt="http://www.onvif.org/ver10/schema">
    <SOAP-ENV:Body>
        <trt:GetAudioEncoderConfigurationsResponse>
            <trt:Configurations token="AudioEncoder_Main_{cam_id}">
                <tt:Name>Main Audio Encoder</tt:Name>
                <tt:UseCount>1</tt:UseCount>
                <tt:Encoding>{audio_enc}</tt:Encoding>
                <tt:Bitrate>{audio_bitrate}</tt:Bitrate>
                <tt:SampleRate>{audio_rate}</tt:SampleRate>
            </trt:Configurations>
            <trt:Configurations token="AudioEncoder_Sub_{cam_id}">
                <tt:Name>Sub Audio Encoder</tt:Name>
                <tt:UseCount>1</tt:UseCount>
                <tt:Encoding>{audio_enc_sub}</tt:Encoding>
                <tt:Bitrate>{audio_bitrate_sub}</tt:Bitrate>
                <tt:SampleRate>{audio_rate_sub}</tt:SampleRate>
            </trt:Configurations>
        </trt:GetAudioEncoderConfigurationsResponse>
    </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        return Response(soap_response, mimetype='application/soap+xml')

    def _handle_get_audio_source_configs(self):
        """Handle GetAudioSourceConfigurations request"""
        cam_id = self.camera.id
        soap_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
                   xmlns:tt="http://www.onvif.org/ver10/schema">
    <SOAP-ENV:Body>
        <trt:GetAudioSourceConfigurationsResponse>
            <trt:Configurations token="AudioSourceConfig_Main_{cam_id}">
                <tt:Name>Main Audio Source</tt:Name>
                <tt:UseCount>1</tt:UseCount>
                <tt:SourceToken>AudioSource_{cam_id}</tt:SourceToken>
            </trt:Configurations>
            <trt:Configurations token="AudioSourceConfig_Sub_{cam_id}">
                <tt:Name>Sub Audio Source</tt:Name>
                <tt:UseCount>1</tt:UseCount>
                <tt:SourceToken>AudioSource_{cam_id}</tt:SourceToken>
            </trt:Configurations>
        </trt:GetAudioSourceConfigurationsResponse>
    </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        return Response(soap_response, mimetype='application/soap+xml')

    def _handle_get_video_encoder_configs(self):
        """Return all live VideoEncoderConfigurations with truthful codecs."""
        definitions = [profile_definition(self.camera, 'main')]
        if not getattr(self.camera, 'disable_substream', False):
            definitions.append(profile_definition(self.camera, 'sub'))

        configurations = "".join(
            render_video_encoder_configuration(
                definition,
                response_element="trt:Configurations",
            )
            for definition in definitions
        )

        soap_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
                   xmlns:tt="http://www.onvif.org/ver10/schema">
    <SOAP-ENV:Body>
        <trt:GetVideoEncoderConfigurationsResponse>
            {configurations}
        </trt:GetVideoEncoderConfigurationsResponse>
    </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        return Response(soap_response, mimetype='application/soap+xml')

    def _handle_get_video_source_configs(self):
        """Handle GetVideoSourceConfigurations request"""
        cam_id = self.camera.id
        use_count = 2 if not getattr(self.camera, 'disable_substream', False) else 1
        soap_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
                   xmlns:tt="http://www.onvif.org/ver10/schema">
    <SOAP-ENV:Body>
        <trt:GetVideoSourceConfigurationsResponse>
            <trt:Configurations token="VideoSource_{cam_id}">
                <tt:Name>Video Source</tt:Name>
                <tt:UseCount>{use_count}</tt:UseCount>
                <tt:SourceToken>VideoSource_{cam_id}</tt:SourceToken>
                <tt:Bounds x="0" y="0" width="{self.camera.main_width}" height="{self.camera.main_height}"/>
            </trt:Configurations>
        </trt:GetVideoSourceConfigurationsResponse>
    </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        return Response(soap_response, mimetype='application/soap+xml')

    def _soap_fault(self, subcode='ter:ActionNotSupported', reason='Action not supported'):
        """Return a standard ONVIF SOAP fault for unsupported/invalid requests."""
        soap_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:ter="http://www.onvif.org/ver10/error">
    <SOAP-ENV:Body>
        <SOAP-ENV:Fault>
            <SOAP-ENV:Code>
                <SOAP-ENV:Value>SOAP-ENV:Sender</SOAP-ENV:Value>
                <SOAP-ENV:Subcode>
                    <SOAP-ENV:Value>{subcode}</SOAP-ENV:Value>
                </SOAP-ENV:Subcode>
            </SOAP-ENV:Code>
            <SOAP-ENV:Reason>
                <SOAP-ENV:Text xml:lang="en">{reason}</SOAP-ENV:Text>
            </SOAP-ENV:Reason>
        </SOAP-ENV:Fault>
    </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        return Response(soap_response, mimetype='application/soap+xml', status=400)

    def _handle_empty_media_response(self, action):
        """Return an empty-but-valid response for list-type requests
        (e.g. audio queries when audio is disabled)."""
        soap_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
                   xmlns:tt="http://www.onvif.org/ver10/schema">
    <SOAP-ENV:Body>
        <trt:{action}Response></trt:{action}Response>
    </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        return Response(soap_response, mimetype='application/soap+xml')

    def _media_profile_fault(self, error):
        """Map strict media-token/setup failures to ONVIF sender faults."""
        if not isinstance(error, MediaProfileError):
            return self._soap_fault('ter:InvalidArgs', str(error))

        if error.code == 'no-profile':
            return self._soap_fault(
                'ter:InvalidArgVal',
                'NoProfile: the requested profile token does not exist'
            )
        if error.code == 'no-config':
            return self._soap_fault(
                'ter:InvalidArgVal',
                'NoConfig: the requested configuration token does not exist'
            )
        if error.code == 'invalid-stream-setup':
            return self._soap_fault(
                'ter:InvalidArgVal',
                'InvalidStreamSetup: unsupported or incomplete StreamSetup'
            )
        return self._soap_fault('ter:InvalidArgs', str(error))
    def _handle_get_profile(self):
        """Return exactly the requested profile and reject unknown tokens."""
        soap_body = request.data.decode('utf-8')
        try:
            profile_token = extract_media_request_text(soap_body, 'ProfileToken')
            kind = profile_kind_from_token(self.camera, profile_token)
        except MediaProfileError as error:
            return self._media_profile_fault(error)

        soap_response = render_get_profile_response(self.camera, kind)
        return Response(soap_response, mimetype='application/soap+xml')
    def _handle_get_video_encoder_config(self):
        """Return exactly the requested VideoEncoderConfiguration."""
        soap_body = request.data.decode('utf-8')
        try:
            token = extract_media_request_text(soap_body, 'ConfigurationToken')
            kind = encoder_kind_from_token(self.camera, token)
            definition = profile_definition(self.camera, kind)
        except MediaProfileError as error:
            return self._media_profile_fault(error)

        configuration = render_video_encoder_configuration(
            definition,
            response_element="trt:Configuration",
        )
        soap_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
                   xmlns:tt="http://www.onvif.org/ver10/schema">
    <SOAP-ENV:Body>
        <trt:GetVideoEncoderConfigurationResponse>
            {configuration}
        </trt:GetVideoEncoderConfigurationResponse>
    </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        return Response(soap_response, mimetype='application/soap+xml')

    def _handle_get_video_encoder_config_options(self):
        """Return encoder options scoped by exact profile/configuration tokens."""
        soap_body = request.data.decode('utf-8')

        try:
            configuration_token = extract_media_request_text(
                soap_body, 'ConfigurationToken'
            )
            profile_token = extract_media_request_text(soap_body, 'ProfileToken')

            selected_kind = None
            if configuration_token:
                selected_kind = encoder_kind_from_token(
                    self.camera, configuration_token
                )

            if profile_token:
                profile_kind = profile_kind_from_token(
                    self.camera, profile_token
                )
                if selected_kind and selected_kind != profile_kind:
                    raise MediaProfileError(
                        'invalid-args',
                        'ProfileToken and ConfigurationToken refer to different profiles'
                    )
                selected_kind = profile_kind
        except MediaProfileError as error:
            return self._media_profile_fault(error)

        if selected_kind:
            definitions = [profile_definition(self.camera, selected_kind)]
        else:
            definitions = [profile_definition(self.camera, 'main')]
            if not getattr(self.camera, 'disable_substream', False):
                definitions.append(profile_definition(self.camera, 'sub'))

        h264_definitions = [
            item for item in definitions
            if item.encoding == 'H264'
        ]
        h264_xml = ""
        if h264_definitions:
            resolutions = [
                (item.width, item.height)
                for item in h264_definitions
            ]
            max_fps = max(item.framerate for item in h264_definitions)
            res_xml = "".join(
                f"""
                    <tt:ResolutionsAvailable>
                        <tt:Width>{w}</tt:Width>
                        <tt:Height>{h}</tt:Height>
                    </tt:ResolutionsAvailable>""" for w, h in resolutions
            )
            h264_xml = f"""
                <tt:H264>{res_xml}
                    <tt:GovLengthRange>
                        <tt:Min>1</tt:Min>
                        <tt:Max>{max_fps * 4}</tt:Max>
                    </tt:GovLengthRange>
                    <tt:FrameRateRange>
                        <tt:Min>1</tt:Min>
                        <tt:Max>{max_fps}</tt:Max>
                    </tt:FrameRateRange>
                    <tt:EncodingIntervalRange>
                        <tt:Min>1</tt:Min>
                        <tt:Max>1</tt:Max>
                    </tt:EncodingIntervalRange>
                    <tt:H264ProfilesSupported>Baseline</tt:H264ProfilesSupported>
                    <tt:H264ProfilesSupported>Main</tt:H264ProfilesSupported>
                </tt:H264>"""

        soap_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
                   xmlns:tt="http://www.onvif.org/ver10/schema">
    <SOAP-ENV:Body>
        <trt:GetVideoEncoderConfigurationOptionsResponse>
            <trt:Options>
                <tt:QualityRange>
                    <tt:Min>1</tt:Min>
                    <tt:Max>5</tt:Max>
                </tt:QualityRange>{h264_xml}
            </trt:Options>
        </trt:GetVideoEncoderConfigurationOptionsResponse>
    </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        return Response(soap_response, mimetype='application/soap+xml')

    def _handle_get_video_source_config(self):
        """Handle GetVideoSourceConfiguration (singular) — there is only one source."""
        cam_id = self.camera.id
        use_count = 2 if not getattr(self.camera, 'disable_substream', False) else 1
        soap_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
                   xmlns:tt="http://www.onvif.org/ver10/schema">
    <SOAP-ENV:Body>
        <trt:GetVideoSourceConfigurationResponse>
            <trt:Configuration token="VideoSource_{cam_id}">
                <tt:Name>Video Source</tt:Name>
                <tt:UseCount>{use_count}</tt:UseCount>
                <tt:SourceToken>VideoSource_{cam_id}</tt:SourceToken>
                <tt:Bounds x="0" y="0" width="{self.camera.main_width}" height="{self.camera.main_height}"/>
            </trt:Configuration>
        </trt:GetVideoSourceConfigurationResponse>
    </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        return Response(soap_response, mimetype='application/soap+xml')

    def _handle_get_video_source_config_options(self):
        """Handle GetVideoSourceConfigurationOptions — bounds are fixed to the source size."""
        cam_id = self.camera.id
        w, h = self.camera.main_width, self.camera.main_height
        soap_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
                   xmlns:tt="http://www.onvif.org/ver10/schema">
    <SOAP-ENV:Body>
        <trt:GetVideoSourceConfigurationOptionsResponse>
            <trt:Options>
                <tt:BoundsRange>
                    <tt:XRange><tt:Min>0</tt:Min><tt:Max>0</tt:Max></tt:XRange>
                    <tt:YRange><tt:Min>0</tt:Min><tt:Max>0</tt:Max></tt:YRange>
                    <tt:WidthRange><tt:Min>{w}</tt:Min><tt:Max>{w}</tt:Max></tt:WidthRange>
                    <tt:HeightRange><tt:Min>{h}</tt:Min><tt:Max>{h}</tt:Max></tt:HeightRange>
                </tt:BoundsRange>
                <tt:VideoSourceTokensAvailable>VideoSource_{cam_id}</tt:VideoSourceTokensAvailable>
            </trt:Options>
        </trt:GetVideoSourceConfigurationOptionsResponse>
    </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        return Response(soap_response, mimetype='application/soap+xml')

    def _handle_get_media_service_capabilities(self):
        """Handle GetServiceCapabilities on the media service"""
        max_profiles = 1 if getattr(self.camera, 'disable_substream', False) else 2
        soap_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:trt="http://www.onvif.org/ver10/media/wsdl">
    <SOAP-ENV:Body>
        <trt:GetServiceCapabilitiesResponse>
            <trt:Capabilities SnapshotUri="true" Rotation="false">
                <trt:ProfileCapabilities MaximumNumberOfProfiles="{max_profiles}"/>
                <trt:StreamingCapabilities RTPMulticast="false" RTP_TCP="true" RTP_RTSP_TCP="true"/>
            </trt:Capabilities>
        </trt:GetServiceCapabilitiesResponse>
    </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        return Response(soap_response, mimetype='application/soap+xml')

    def _handle_get_scopes(self):
        """Return exactly the fixed scopes advertised through WS-Discovery."""
        scopes = self.camera.get_identity_manifest()["scopes"]
        scope_xml = "".join(
            f"""
            <tds:Scopes>
                <tt:ScopeDef>Fixed</tt:ScopeDef>
                <tt:ScopeItem>{escape(scope)}</tt:ScopeItem>
            </tds:Scopes>"""
            for scope in scopes
        )

        soap_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:tds="http://www.onvif.org/ver10/device/wsdl"
                   xmlns:tt="http://www.onvif.org/ver10/schema">
    <SOAP-ENV:Body>
        <tds:GetScopesResponse>{scope_xml}
        </tds:GetScopesResponse>
    </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        return Response(soap_response, mimetype='application/soap+xml')

    def _get_events_wsdl(self):
        """Return events service WSDL"""
        local_ip = self.camera.get_effective_ip()
        wsdl = f"""<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="http://schemas.xmlsoap.org/wsdl/"
             xmlns:tev="http://www.onvif.org/ver10/events/wsdl"
             xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap12/"
             targetNamespace="http://www.onvif.org/ver10/events/wsdl">
    <service name="EventsService">
        <port name="EventsPort" binding="tev:EventBinding">
            <soap:address location="http://{local_ip}:{self.camera.onvif_port}/onvif/events_service"/>
        </port>
    </service>
</definitions>"""
        return Response(wsdl, mimetype='text/xml')

    def publish_event(self, event):
        """Publish one normalized event into the retained PullPoint state engine."""
        return self.event_engine.publish(event)

    def event_health(self):
        """Return non-secret PullPoint/event counters for diagnostics."""
        return self.event_engine.health()

    def _event_now_iso(self):
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _event_fault(self, error):
        """Map event-engine failures to SOAP faults instead of HTTP/plain-text errors."""
        if isinstance(error, EventSubscriptionError):
            if error.code == "resource-unknown":
                return self._soap_fault(
                    "ter:ResourceUnknown",
                    "Unknown or expired PullPoint subscription",
                )
            if error.code == "capacity":
                return self._soap_fault(
                    "ter:InvalidArgVal",
                    "Maximum PullPoint subscription count reached",
                )
            return self._soap_fault("ter:InvalidArgVal", str(error))
        return self._soap_fault("ter:InvalidArgVal", str(error))

    def _handle_create_pull_point_subscription(self, local_ip):
        try:
            soap_body = request.data.decode("utf-8")
            ttl_seconds = resolve_termination_seconds(
                extract_xml_text(soap_body, "InitialTerminationTime")
            )
            topics = parse_topic_filter(soap_body, DEFAULT_TOPICS)
            sub = self.event_engine.create_subscription(
                client_ip=request.remote_addr,
                ttl_seconds=ttl_seconds,
                topics=topics,
            )
        except Exception as error:
            return self._event_fault(error)

        sub_ref = (
            f"http://{local_ip}:{self.camera.onvif_port}"
            f"/onvif/subscription/{sub.sub_id}"
        )
        current_time = self._event_now_iso()

        soap_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:wsa="http://www.w3.org/2005/08/addressing"
                   xmlns:tet="http://www.onvif.org/ver10/events/wsdl">
    <SOAP-ENV:Header>
        <wsa:Action>http://www.onvif.org/ver10/events/wsdl/EventPortType/CreatePullPointSubscriptionResponse</wsa:Action>
    </SOAP-ENV:Header>
    <SOAP-ENV:Body>
        <tet:CreatePullPointSubscriptionResponse>
            <tet:SubscriptionReference>
                <wsa:Address>{sub_ref}</wsa:Address>
            </tet:SubscriptionReference>
            <tet:CurrentTime>{current_time}</tet:CurrentTime>
            <tet:TerminationTime>{sub.termination_time}</tet:TerminationTime>
        </tet:CreatePullPointSubscriptionResponse>
    </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        return Response(soap_response, mimetype="application/soap+xml")

    def _handle_pull_messages(self, sub_id):
        try:
            soap_body = request.data.decode("utf-8")
            timeout_seconds = parse_pull_timeout_seconds(soap_body)
            message_limit = parse_message_limit(soap_body)
            sub, events = self.event_engine.pull(
                sub_id,
                message_limit=message_limit,
                timeout_seconds=timeout_seconds,
            )
        except Exception as error:
            return self._event_fault(error)

        source_token = f"VideoSource_{self.camera.id}"
        analytics_token = f"VideoAnalytics_{self.camera.id}"
        messages_xml = "".join(
            render_notification_message(
                event,
                video_source_config_token=source_token,
                video_analytics_config_token=analytics_token,
            )
            for event in events
        )

        soap_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:wsa="http://www.w3.org/2005/08/addressing"
                   xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"
                   xmlns:tt="http://www.onvif.org/ver10/schema"
                   xmlns:tns1="http://www.onvif.org/ver10/topics"
                   xmlns:xs="http://www.w3.org/2001/XMLSchema">
    <SOAP-ENV:Header>
        <wsa:Action>http://www.onvif.org/ver10/events/wsdl/PullPointSubscription/PullMessagesResponse</wsa:Action>
    </SOAP-ENV:Header>
    <SOAP-ENV:Body>
        <tet:PullMessagesResponse xmlns:tet="http://www.onvif.org/ver10/events/wsdl">
            <tet:CurrentTime>{self._event_now_iso()}</tet:CurrentTime>
            <tet:TerminationTime>{sub.termination_time}</tet:TerminationTime>
            {messages_xml}
        </tet:PullMessagesResponse>
    </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        return Response(soap_response, mimetype="application/soap+xml")

    def _handle_set_synchronization_point(self, sub_id):
        try:
            self.event_engine.set_synchronization_point(sub_id)
        except Exception as error:
            return self._event_fault(error)

        soap_response = """<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:wsa="http://www.w3.org/2005/08/addressing"
                   xmlns:tet="http://www.onvif.org/ver10/events/wsdl">
    <SOAP-ENV:Header>
        <wsa:Action>http://www.onvif.org/ver10/events/wsdl/PullPointSubscription/SetSynchronizationPointResponse</wsa:Action>
    </SOAP-ENV:Header>
    <SOAP-ENV:Body>
        <tet:SetSynchronizationPointResponse/>
    </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        return Response(soap_response, mimetype="application/soap+xml")

    def _handle_unsubscribe(self, sub_id):
        try:
            self.event_engine.unsubscribe(sub_id)
        except Exception as error:
            return self._event_fault(error)

        soap_response = """<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:wsa="http://www.w3.org/2005/08/addressing"
                   xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2">
    <SOAP-ENV:Header>
        <wsa:Action>http://docs.oasis-open.org/wsn/b-2/SubscriptionManager/UnsubscribeResponse</wsa:Action>
    </SOAP-ENV:Header>
    <SOAP-ENV:Body>
        <wsnt:UnsubscribeResponse/>
    </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        return Response(soap_response, mimetype="application/soap+xml")

    def _handle_renew_subscription(self, sub_id):
        try:
            soap_body = request.data.decode("utf-8")
            ttl_seconds = resolve_termination_seconds(
                extract_xml_text(soap_body, "TerminationTime")
            )
            sub = self.event_engine.renew(sub_id, ttl_seconds)
        except Exception as error:
            return self._event_fault(error)

        soap_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:wsa="http://www.w3.org/2005/08/addressing"
                   xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2">
    <SOAP-ENV:Header>
        <wsa:Action>http://docs.oasis-open.org/wsn/b-2/SubscriptionManager/RenewResponse</wsa:Action>
    </SOAP-ENV:Header>
    <SOAP-ENV:Body>
        <wsnt:RenewResponse>
            <wsnt:TerminationTime>{sub.termination_time}</wsnt:TerminationTime>
            <wsnt:CurrentTime>{self._event_now_iso()}</wsnt:CurrentTime>
        </wsnt:RenewResponse>
    </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        return Response(soap_response, mimetype="application/soap+xml")

    def _handle_get_event_properties(self):
        topic_set = render_topic_set(
            DEFAULT_TOPICS,
            element_name="tet:TopicSet",
        )
        soap_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:tet="http://www.onvif.org/ver10/events/wsdl"
                   xmlns:wstop="http://docs.oasis-open.org/wsn/t-1"
                   xmlns:wsa="http://www.w3.org/2005/08/addressing"
                   xmlns:tns1="http://www.onvif.org/ver10/topics"
                   xmlns:tt="http://www.onvif.org/ver10/schema"
                   xmlns:xs="http://www.w3.org/2001/XMLSchema">
    <SOAP-ENV:Header>
        <wsa:Action>http://www.onvif.org/ver10/events/wsdl/EventPortType/GetEventPropertiesResponse</wsa:Action>
    </SOAP-ENV:Header>
    <SOAP-ENV:Body>
        <tet:GetEventPropertiesResponse>
            <tet:TopicNamespaceLocation>http://www.onvif.org/onvif/ver10/topics/topicns.xml</tet:TopicNamespaceLocation>
            <tet:FixedTopicSet>true</tet:FixedTopicSet>
            {topic_set}
            <tet:TopicExpressionDialect>{CONCRETE_TOPIC_DIALECT}</tet:TopicExpressionDialect>
            <tet:TopicExpressionDialect>{CONCRETE_SET_DIALECT}</tet:TopicExpressionDialect>
            <tet:MessageContentSchemaLocation>http://www.onvif.org/ver10/schema/onvif.xsd</tet:MessageContentSchemaLocation>
        </tet:GetEventPropertiesResponse>
    </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        return Response(soap_response, mimetype="application/soap+xml")

    def _handle_get_event_service_capabilities(self):
        max_pullpoints = self.event_engine.max_pullpoints
        soap_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:tet="http://www.onvif.org/ver10/events/wsdl"
                   xmlns:wsa="http://www.w3.org/2005/08/addressing">
    <SOAP-ENV:Header>
        <wsa:Action>http://www.onvif.org/ver10/events/wsdl/EventPortType/GetServiceCapabilitiesResponse</wsa:Action>
    </SOAP-ENV:Header>
    <SOAP-ENV:Body>
        <tet:GetServiceCapabilitiesResponse>
            <tet:Capabilities WSSubscriptionPolicySupport="false"
                              WSPullPointSupport="true"
                              WSPausableSubscriptionManagerInterfaceSupport="false"
                              MaxNotificationProducers="1"
                              MaxPullPoints="{max_pullpoints}"
                              PersistentNotificationStorage="false"/>
        </tet:GetServiceCapabilitiesResponse>
    </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        return Response(soap_response, mimetype="application/soap+xml")
