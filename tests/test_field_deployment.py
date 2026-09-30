import copy
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

from tools.import_vcam_config import migrate, legacy_uuid_from_mac
from tools.field_preflight import validate_config, probe_video, write_private
from tools.field_deploy import capture_interfaces, deploy, rollback, image_tool
from app.camera import VirtualONVIFCamera
from app.media_profile import profile_kind_from_token, encoder_kind_from_token, render_get_profiles_response
from app.event_engine import TOPICS, render_notification_message


def source_config():
    return {
        'host_sources': [
            {'name': 'lorex', 'hostname': '192.0.2.57', 'auth': {'username': 'lorex-user', 'password': 'lorex-pw'}},
            {'name': 'nvr69', 'hostname': '192.0.2.69', 'auth': {'username': 'nvr-user', 'password': 'nvr-pw'}},
        ],
        'virtual_cameras': [
            {'name': f'Camera {index}', 'model': 'Legacy', 'mac': f'02:42:ac:11:00:{index:02x}',
             'ip': f'192.0.2.{100+index}/24', 'host_source': 'lorex' if index <= 16 else 'nvr69',
             'rtsp_path_hq': f'/cam/realmonitor?channel={index}&subtype=0',
             'rtsp_path_lq': f'/cam/realmonitor?channel={index}&subtype=1',
             'stream_hq': {'encoding': 'H265', 'width': 3840, 'height': 2160, 'framerate': 7},
             'stream_lq': {'encoding': 'H265', 'width': 960, 'height': 480, 'framerate': 7}}
            for index in range(1, 30)]}


def migrated_config():
    return migrate(source_config(), parent_interface='ens19', onvif_username=None,
                   onvif_password=None, first_onvif_port=8001, preserve_port_80=True)


class FieldMigrationTests(unittest.TestCase):
    def test_29_camera_migration_preserves_recorder_credentials_and_h265(self):
        config = migrated_config()
        manifest = validate_config(config)
        self.assertEqual(len(manifest['cameras']), 29)
        self.assertEqual(config['cameras'][0]['onvifUsername'], 'lorex-user')
        self.assertEqual(config['cameras'][16]['onvifPassword'], 'nvr-pw')
        self.assertEqual(config['cameras'][0]['mainEncoding'], 'H265')
        self.assertEqual(config['cameras'][0]['subEncoding'], 'H265')
        self.assertEqual(config['cameras'][0]['uuid'], '0242ac11-0001-0000-0000-000000000000')

    def test_cached_legacy_profile_and_encoder_tokens_are_preserved(self):
        camera = VirtualONVIFCamera(migrated_config()['cameras'][0])
        self.assertEqual(profile_kind_from_token(camera, 'profile_hq_0242ac110001'), 'main')
        self.assertEqual(profile_kind_from_token(camera, 'profile_lq_0242ac110001'), 'sub')
        self.assertEqual(encoder_kind_from_token(camera, 'video_encoder_lq_0242ac110001'), 'sub')
        self.assertIn('profile_hq_0242ac110001', render_get_profiles_response(camera))
        self.assertEqual(camera.to_config_dict()['mediaTokens'], camera.media_tokens)

    def test_validation_rejects_duplicate_mac_ip_and_paths(self):
        for field in ('nicMac', 'staticIp', 'pathName', 'uuid'):
            config = migrated_config()
            config['cameras'][1][field] = config['cameras'][0][field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_config(config)

    def test_dhcp_migration_cannot_pass_static_field_gate(self):
        config = migrated_config()
        config['cameras'][0]['ipMode'] = 'dhcp'
        with self.assertRaisesRegex(ValueError, 'static virtual NIC'):
            validate_config(config)

    def test_secret_reference_is_resolved_per_recorder(self):
        source = source_config()
        source['host_sources'][1]['auth']['password'] = {'env': 'TEST_NVR_PASSWORD'}
        with patch.dict('os.environ', {'TEST_NVR_PASSWORD': 'second-secret'}):
            output = migrate(source, parent_interface='ens19', onvif_username=None,
                onvif_password=None, first_onvif_port=8001, preserve_port_80=True)
        self.assertEqual(output['cameras'][16]['onvifPassword'], 'second-secret')
        self.assertEqual(output['cameras'][0]['onvifPassword'], 'lorex-pw')

    def test_failed_ffprobe_does_not_include_url_or_password(self):
        with patch('tools.field_preflight.subprocess.run', return_value=SimpleNamespace(stdout='{}')):
            with self.assertRaises(ValueError) as error:
                probe_video('rtsp://user:secret@192.0.2.57/main')
        self.assertNotIn('secret', str(error.exception))
        self.assertNotIn('rtsp://', str(error.exception))

    def test_probe_recognizes_hevc_and_fractional_framerate(self):
        payload = {'streams': [{'codec_name': 'hevc', 'width': 3840, 'height': 2160,
                                'avg_frame_rate': '0/0', 'r_frame_rate': '7/1'}],
                   'packets': [{'pts_time': str(index / 7)} for index in range(8)]}
        with patch('tools.field_preflight.subprocess.run', return_value=SimpleNamespace(stdout=json.dumps(payload))):
            observed = probe_video('rtsp://192.0.2.57/main')
        self.assertEqual(observed['Encoding'], 'H265')
        self.assertEqual(observed['Framerate'], 7)

    def test_probe_measures_packets_instead_of_false_100_fps_header(self):
        payload = {'streams': [{'codec_name': 'hevc', 'width': 3840, 'height': 2160,
                               'avg_frame_rate': '100/1', 'r_frame_rate': '100/1'}],
                   'packets': [{'pts_time': str(index / 7)} for index in (0, 2, 1, 3, 5, 4, 6, 7)]}
        with patch('tools.field_preflight.subprocess.run', return_value=SimpleNamespace(stdout=json.dumps(payload))):
            self.assertEqual(probe_video('rtsp://192.0.2.57/main')['Framerate'], 7)

    def test_metadata_without_live_packets_cannot_pass_video_gate(self):
        payload = {'streams': [{'codec_name': 'hevc', 'width': 3840, 'height': 2160,
                               'avg_frame_rate': '100/1', 'r_frame_rate': '100/1'}]}
        with patch('tools.field_preflight.subprocess.run', return_value=SimpleNamespace(stdout=json.dumps(payload))):
            with self.assertRaisesRegex(ValueError, 'valid video packets not observed'):
                probe_video('rtsp://user:secret@192.0.2.57/main')

    def test_timeout_and_authentication_reasons_never_echo_credentials(self):
        url = 'rtsp://user:secret@192.0.2.57/main'
        for failure, reason in (
            (subprocess.TimeoutExpired(['ffprobe', url], 20, stderr=url), 'timed out'),
            (subprocess.CalledProcessError(1, ['ffprobe', url], stderr=url + '\nmethod DESCRIBE failed: 401 Unauthorized'), 'authentication rejected')):
            with self.subTest(reason=reason), patch('tools.field_preflight.subprocess.run', side_effect=failure):
                with self.assertRaisesRegex(ValueError, reason) as error:
                    probe_video(url)
                self.assertNotIn('secret', str(error.exception))
                self.assertNotIn('rtsp://', str(error.exception))

    def test_private_config_write_removes_preexisting_world_readability(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text('{}')
            path.chmod(0o644)
            write_private(path, {'password': 'local-only'})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_all_smart_topics_match_protect_listener_state_classifier(self):
        accepted = {'tns1:UserAlarm/IVA/HumanShapeDetect': 'person',
                    'tns1:VehicleAlarm/IVB/VehicleDetect': 'vehicle',
                    'tns1:RuleEngine/MyRuleDetector/DogCatDetect': 'animal',
                    'tns1:RuleEngine/MyRuleDetector/Package': 'package'}
        import xml.etree.ElementTree as ET
        for event_type in ('person', 'vehicle', 'animal', 'package'):
            message = render_notification_message({'topic': TOPICS[event_type], 'data': {'State': True}},
                video_source_config_token='VideoSource_1', video_analytics_config_token='VideoAnalytics_1')
            wrapper = '<root xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2" xmlns:tt="http://www.onvif.org/ver10/schema">' + message + '</root>'
            root = ET.fromstring(wrapper)
            topic = root.find('.//{http://docs.oasis-open.org/wsn/b-2}Topic').text
            self.assertEqual(accepted[topic], event_type)
            item = root.find('.//{http://www.onvif.org/ver10/schema}Data/{http://www.onvif.org/ver10/schema}SimpleItem')
            self.assertEqual(item.attrib, {'Name': 'State', 'Value': 'true'})


class FieldTransactionTests(unittest.TestCase):
    def test_image_tool_reports_safe_camera_failure_and_completed_progress(self):
        def failed_check(args, *, stdout, stderr, text):
            stdout.write('camera 1 main: H265 3840x2160 7 fps\n')
            stdout.flush()
            stderr.write('rtsp://user:secret@192.0.2.57/main\nFAIL: camera 2 main: source video probe failed (timed out)\n')
            stderr.flush()
            return SimpleNamespace(poll=lambda: 1, returncode=1)
        with tempfile.TemporaryDirectory() as directory, \
             patch('tools.field_deploy.subprocess.Popen', side_effect=failed_check), \
             redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(RuntimeError, 'camera 2 main.*timed out') as error:
                image_tool({'stage': directory, 'image': 'fake'}, ['tools/field_preflight.py'])
            self.assertIn('camera 1 main', output.getvalue())
            self.assertNotIn('secret', output.getvalue() + str(error.exception))
            for private_log in Path(directory).glob('*.stderr.log'):
                self.assertEqual(private_log.stat().st_mode & 0o777, 0o600)

    def interfaces(self):
        camera = migrated_config()['cameras'][0]
        return [
            {'ifindex': 2, 'ifname': 'ens19'},
            {'ifindex': 3, 'ifname': 'vcam-1', 'link_index': 2, 'address': camera['nicMac'],
             'linkinfo': {'info_kind': 'macvlan'},
             'addr_info': [{'local': camera['staticIp'], 'prefixlen': 24, 'family': 'inet'}]}]

    def test_capture_rejects_host_address_and_wrong_parent(self):
        config = migrated_config()
        config['cameras'] = config['cameras'][:1]
        interfaces = self.interfaces()
        interfaces[1]['linkinfo']['info_kind'] = 'ether'
        with self.assertRaisesRegex(ValueError, 'not a macvlan'):
            capture_interfaces(config, interfaces)
        interfaces = self.interfaces()
        interfaces[0]['ifname'] = 'ens18'
        with self.assertRaisesRegex(ValueError, 'parent interface differs'):
            capture_interfaces(config, interfaces)

    def test_capture_uses_exact_mac_ip_not_interface_prefix_alone(self):
        config = migrated_config()
        config['cameras'] = config['cameras'][:1]
        interfaces = self.interfaces()
        interfaces.append({'ifindex': 9, 'ifname': 'vcam-unrelated', 'address': '02:00:00:00:00:99'})
        captured = capture_interfaces(config, interfaces)
        self.assertEqual([item['name'] for item in captured], ['vcam-1'])

    def test_failed_new_runtime_automatically_rolls_back(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config = migrated_config()
            write_private(path / 'camera_config.json', config)
            inventory = [{'name': 'vcam-1', 'parent': 'ens19', 'mac': '02:42:ac:11:00:01', 'addresses': ['192.0.2.101/24']}]
            plan = {'stage': directory, 'phase': 'prepared', 'interfaces': inventory,
                    'configSha256': hashlib.sha256((path / 'camera_config.json').read_bytes()).hexdigest(),
                    'legacyContainer': 'old', 'legacyService': 'legacy.service', 'serviceEnabled': 'enabled',
                    'image': 'fake', 'soakSeconds': 60}
            plan.update(legacyId='original-id', legacyImage='original-image')
            with patch('tools.field_deploy.run', return_value=SimpleNamespace(stdout='[]')) as commands, \
                 patch('tools.field_deploy.capture_interfaces', return_value=inventory), \
                 patch('tools.field_deploy.inspect_container', return_value={'State': {'Running': True}, 'Id': 'original-id', 'Image': 'original-image'}), \
                 patch('tools.field_deploy.wait_acceptance', side_effect=RuntimeError('gate failed')), \
                 patch('tools.field_deploy.rollback') as restore:
                with self.assertRaisesRegex(RuntimeError, 'gate failed'):
                    deploy(plan)
                restore.assert_called_once_with(plan)
                self.assertIn(['docker', 'stop', '--time', '60', 'old'], [call.args[0] for call in commands.call_args_list])

    def test_deferred_probe_failure_restores_old_bridge_before_starting_candidate(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            write_private(stage / 'camera_config.json', migrated_config())
            inventory = [{'name': 'vcam-1', 'parent': 'ens19', 'mac': '02:42:ac:11:00:01', 'addresses': ['192.0.2.101/24']}]
            plan = {'stage': directory, 'phase': 'prepared', 'interfaces': inventory,
                    'configSha256': hashlib.sha256((stage / 'camera_config.json').read_bytes()).hexdigest(),
                    'legacyContainer': 'old', 'legacyService': 'legacy.service', 'serviceEnabled': 'disabled',
                    'legacyId': 'original-id', 'legacyImage': 'original-image',
                    'image': 'fake', 'soakSeconds': 60, 'sourceProbesDeferred': True, 'expectedCameras': 29}
            with patch('tools.field_deploy.run', return_value=SimpleNamespace(stdout='[]')) as commands, \
                 patch('tools.field_deploy.capture_interfaces', return_value=inventory), \
                 patch('tools.field_deploy.inspect_container', return_value={'State': {'Running': True}, 'Id': 'original-id', 'Image': 'original-image'}), \
                 patch('tools.field_deploy.image_tool', side_effect=RuntimeError('camera 2 main timed out')) as check, \
                 patch('tools.field_deploy.rollback') as restore:
                with self.assertRaisesRegex(RuntimeError, 'camera 2 main'):
                    deploy(plan)
                restore.assert_called_once_with(plan)
                self.assertIn('--refresh-metadata', check.call_args.args[1])
                self.assertEqual([call.args[0][0:2] for call in commands.call_args_list],
                                 [['ip', '-j'], ['docker', 'stop']])

    def test_deferred_metadata_is_copied_before_candidate_start(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            config = migrated_config()
            write_private(stage / 'camera_config.json', config)
            inventory = [{'name': 'vcam-1', 'parent': 'ens19', 'mac': '02:42:ac:11:00:01', 'addresses': ['192.0.2.101/24']}]
            plan = {'stage': directory, 'phase': 'prepared', 'interfaces': inventory,
                    'configSha256': hashlib.sha256((stage / 'camera_config.json').read_bytes()).hexdigest(),
                    'legacyContainer': 'old', 'legacyService': 'legacy.service', 'serviceEnabled': 'disabled',
                    'legacyId': 'original-id', 'legacyImage': 'original-image',
                    'image': 'fake', 'soakSeconds': 0, 'sourceProbesDeferred': True, 'expectedCameras': 29}
            def check_image(plan, command):
                if '--refresh-metadata' in command:
                    config['cameras'][0]['mainFramerate'] = 9
                    write_private(stage / 'camera_config.json', config)
            def execute(command, **kwargs):
                if command[:3] == ['docker', 'run', '-d']:
                    loaded = json.loads((stage / 'data/camera_config.json').read_text())
                    self.assertEqual(loaded['cameras'][0]['mainFramerate'], 9)
                return SimpleNamespace(stdout='[]')
            with patch('tools.field_deploy.run', side_effect=execute), \
                 patch('tools.field_deploy.capture_interfaces', return_value=inventory), \
                 patch('tools.field_deploy.inspect_container', return_value={'State': {'Running': True}, 'Id': 'original-id', 'Image': 'original-image'}), \
                 patch('tools.field_deploy.image_tool', side_effect=check_image), \
                 patch('tools.field_deploy.wait_acceptance', return_value={}):
                deploy(plan)
            self.assertEqual(plan['phase'], 'video-verified')
