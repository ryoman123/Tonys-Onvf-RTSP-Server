"""Behavioral checks at the inherited detector/recorder/browser boundaries."""
import json
from pathlib import Path
import shlex
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

import yaml

from app.camera import VirtualONVIFCamera
from app.manager import CameraManager
from app.mediamtx_manager import MediaMTXManager
from app.onvif_service import ONVIFService
from app.runtime_health import build_readiness, evaluate_acceptance
from app.stream_paths import internal_rtsp_url, stream_path
from app.media_profile import profile_definition
from test_field_deployment import migrated_config
from test_onvif_events_soap import envelope, WSA, WSNT, TT
from test_runtime_health import FakeManager, FakeThread


class TonyFeatureParityTests(unittest.TestCase):
    def camera(self):
        camera = VirtualONVIFCamera(migrated_config()['cameras'][0])
        camera.assigned_ip = camera.static_ip
        return camera

    def test_ai_main_fallback_uses_real_path_and_encoded_relay_credentials(self):
        camera = self.camera()
        camera.disable_substream = True
        camera.manager = SimpleNamespace(rtsp_port=18554, rtsp_auth_enabled=True,
                                         global_username='reader@', global_password='p:/?#')
        url = internal_rtsp_url(camera)
        self.assertEqual(url, f'rtsp://reader%40:p%3A%2F%3F%23@127.0.0.1:18554/{camera.path_name}_main')
        self.assertEqual(stream_path(camera, browser=True), camera.path_name + '_main_browser')

    def test_explicit_video_transcoding_advertises_its_real_output_codec(self):
        camera = self.camera()
        camera.transcode_main = True
        self.assertEqual(profile_definition(camera, 'main').encoding, 'H264')
        self.assertEqual(stream_path(camera, 'main', browser=True), camera.path_name + '_main')
        self.assertEqual(profile_definition(camera, 'sub').encoding, 'H265')

    def test_h265_recorder_paths_stay_native_and_browser_encoder_is_on_demand(self):
        camera = self.camera()
        camera.status = 'running'
        relay = MediaMTXManager()
        with tempfile.TemporaryDirectory() as directory, patch('app.ffmpeg_manager.FFmpegManager') as ffmpeg:
            ffmpeg.return_value.get_ffmpeg_path.return_value = '/usr/bin/ffmpeg'
            relay.config_file = str(Path(directory) / 'mediamtx.yml')
            relay.create_config([camera], rtsp_port=8554, rtsp_username='reader@', rtsp_password='p:/?#')
            paths = yaml.safe_load(Path(relay.config_file).read_text())['paths']
        for kind in ('main', 'sub'):
            path = paths[camera.path_name + '_' + kind]
            self.assertEqual(path['source'], getattr(camera, kind + '_stream_url'))
            self.assertNotIn('runOnInit', path)
            preview = paths[camera.path_name + '_' + kind + '_browser']
            self.assertNotIn('runOnInit', preview)
            args = shlex.split(preview['runOnDemand'])
            self.assertEqual(args[args.index('-i') + 1], internal_rtsp_url(camera, kind, username='reader@', password='p:/?#'))
            self.assertEqual(args[args.index('-c:v') + 1], 'libx264')
            self.assertEqual(camera.to_dict()['browserPaths'][kind], camera.path_name + '_' + kind + '_browser')

    def test_camera_edit_keeps_adoption_auth_paths_tokens_and_identity(self):
        camera = self.camera()
        manager = CameraManager.__new__(CameraManager)
        manager.cameras = [camera]
        manager.global_username, manager.global_password = 'admin', 'admin'
        manager.save_config = Mock()
        manager.is_port_available = Mock(return_value=True)
        before = (camera.path_name, camera.onvif_username, camera.onvif_password,
                  camera.get_identity_manifest(), dict(camera.media_tokens))
        manager.update_camera(camera.id, 'Renamed Camera', '192.0.2.57', 554,
            'lorex-user', 'lorex-pw', '/main', '/sub',
            use_virtual_nic=True, nic_mac=camera.nic_mac, static_ip=camera.static_ip,
            parent_interface='ens19', ip_mode='static', event_source='ai', enable_event_forwarding=True)
        self.assertEqual(before, (camera.path_name, camera.onvif_username, camera.onvif_password,
                                 camera.get_identity_manifest(), dict(camera.media_tokens)))

    def test_local_ai_restart_emits_new_smart_events_after_clearing(self):
        camera = self.camera()
        camera.onvif_service = ONVIFService(camera)
        client = camera.onvif_service.create_app().test_client()
        def post(path, body):
            return client.post(path, data=envelope(body), auth=(camera.onvif_username, camera.onvif_password))
        response = post('/onvif/events_service', '<tev:CreatePullPointSubscription/>')
        path = urlparse(ET.fromstring(response.data).find('.//' + WSA + 'Address').text).path
        pull = '<tev:PullMessages><tev:Timeout>PT0S</tev:Timeout><tev:MessageLimit>256</tev:MessageLimit></tev:PullMessages>'
        post(path, pull)  # Drain initialization properties.
        for _ in range(2):
            camera._trigger_ai_motion(True, ['person', 'vehicle'], {'person': .9, 'vehicle': .8})
            camera.stop_ai_detection()
            messages = ET.fromstring(post(path, pull).data).findall('.//' + WSNT + 'NotificationMessage')
            values = [(message.find(WSNT + 'Topic').text, message.find('.//' + TT + 'Data/' + TT + 'SimpleItem').attrib['Value']) for message in messages]
            for topic in ('UserAlarm/IVA/HumanShapeDetect', 'VehicleAlarm/IVB/VehicleDetect'):
                self.assertIn(('tns1:' + topic, 'true'), values)
                self.assertIn(('tns1:' + topic, 'false'), values)

    def test_full_pipeline_rejects_video_only_and_stale_local_ai_or_listener(self):
        manager = FakeManager()
        status = build_readiness(manager)
        self.assertTrue(status['ready'])
        self.assertFalse(evaluate_acceptance(status, require_smart_pipeline=True)['passed'])
        camera = manager.cameras[0]
        camera.enable_event_forwarding, camera.event_source = True, 'ai'
        camera.ai_targets, camera.send_smart_onvif_topics = ['person', 'vehicle'], True
        camera._ai_running, camera._ai_model_loaded = True, True
        camera._ai_thread = FakeThread()
        camera._ai_last_frame_at = time.time()
        target = {'id': 'nvr', 'name': 'Protect', 'status': 'active', 'checkedAt': time.time()}
        manager.protect_listener = SimpleNamespace(get_public_state=lambda: {'monitorEnabled': True, 'nvrs': [target]})
        status = build_readiness(manager)
        self.assertTrue(evaluate_acceptance(status, require_smart_pipeline=True)['passed'])
        self.assertFalse(status['fullStack']['timelineVerified'])
        camera._ai_last_frame_at -= 20
        status = build_readiness(manager)
        self.assertFalse(status['analytics']['ready'])
        self.assertFalse(evaluate_acceptance(status, require_smart_pipeline=True)['passed'])
        camera._ai_last_frame_at = time.time()
        target['checkedAt'] -= 400
        self.assertFalse(evaluate_acceptance(build_readiness(manager), require_smart_pipeline=True)['passed'])


if __name__ == '__main__':
    unittest.main()
