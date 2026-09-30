"""Authenticated SOAP integration checks and official event Action regressions."""
import json
import unittest
from types import SimpleNamespace
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

from app.camera import VirtualONVIFCamera
from app.onvif_service import ONVIFService
from app.frigate_mqtt import FrigateMqttRuntime
from app.event_engine import TOPICS
from test_field_deployment import migrated_config

WSA = '{http://www.w3.org/2005/08/addressing}'
WSNT = '{http://docs.oasis-open.org/wsn/b-2}'
TT = '{http://www.onvif.org/ver10/schema}'


def envelope(body):
    return '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tev="http://www.onvif.org/ver10/events/wsdl" xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"><s:Body>' + body + '</s:Body></s:Envelope>'


class EventSoapIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.cameras = [VirtualONVIFCamera(config) for config in migrated_config()['cameras']]
        self.clients = []
        for camera in self.cameras:
            camera.status = 'running'
            camera.assigned_ip = camera.static_ip
            camera.onvif_service = ONVIFService(camera)
            self.clients.append(camera.onvif_service.create_app().test_client())

    def post(self, index, path, body):
        camera = self.cameras[index]
        return self.clients[index].post(path, data=envelope(body), auth=(camera.onvif_username, camera.onvif_password))

    def subscribe(self, index):
        response = self.post(index, '/onvif/events_service', '<tev:CreatePullPointSubscription/>')
        self.assertEqual(response.status_code, 200)
        return urlparse(ET.fromstring(response.data).find('.//' + WSA + 'Address').text).path

    def test_event_actions_match_official_test_spec_annex_a3(self):
        path = self.subscribe(0)
        actions = [
            ('/onvif/events_service', '<tev:GetEventProperties/>', 'EventPortType/GetEventPropertiesResponse'),
            (path, '<tev:SetSynchronizationPoint/>', 'PullPointSubscription/SetSynchronizationPointResponse'),
            (path, '<tev:PullMessages><tev:Timeout>PT0S</tev:Timeout><tev:MessageLimit>256</tev:MessageLimit></tev:PullMessages>', 'PullPointSubscription/PullMessagesResponse'),
        ]
        for route, body, suffix in actions:
            response = self.post(0, route, body)
            self.assertEqual(response.status_code, 200)
            action = ET.fromstring(response.data).find('.//' + WSA + 'Action')
            self.assertIsNotNone(action)
            self.assertEqual(action.text, 'http://www.onvif.org/ver10/events/wsdl/' + suffix)

    def test_unsupported_content_filter_is_rejected_and_not_advertised(self):
        response = self.post(0, '/onvif/events_service', '<tev:GetEventProperties/>')
        self.assertNotIn(b'MessageContentFilterDialect', response.data)
        response = self.post(0, '/onvif/events_service', '<tev:CreatePullPointSubscription><tev:Filter><wsnt:MessageContent>boolean(//State)</wsnt:MessageContent></tev:Filter></tev:CreatePullPointSubscription>')
        self.assertNotEqual(response.status_code, 200)
        self.assertIn(b'Fault', response.data)
        self.assertEqual(len(self.cameras[0].onvif_service.subscriptions), 0)

    def test_29_cameras_232_frigate_transitions_have_no_cross_camera_delivery(self):
        manager = SimpleNamespace(cameras=self.cameras)
        manager.resolve_camera_reference = lambda ref: next((camera for camera in self.cameras if ref in (camera.name, camera.path_name, camera.id)), None)
        runtime = FrigateMqttRuntime(manager, {'enabled': True, 'cameraMap': {}, 'autoMap': True})
        paths = [self.subscribe(index) for index in range(29)]
        pull = '<tev:PullMessages><tev:Timeout>PT0S</tev:Timeout><tev:MessageLimit>256</tev:MessageLimit></tev:PullMessages>'
        labels = {'person': 'person', 'vehicle': 'car', 'animal': 'dog', 'package': 'package'}
        for index, camera in enumerate(self.cameras):
            for event_type, label in labels.items():
                for state in ('new', 'end'):
                    payload = {'type': state, 'after': {'id': f'{index}-{label}', 'camera': camera.path_name, 'label': label, 'false_positive': False}}
                    runtime.route_message('frigate/events', json.dumps(payload))
                    response = self.post(index, paths[index], pull)
                    self.assertEqual(response.status_code, 200)
                    messages = ET.fromstring(response.data).findall('.//' + WSNT + 'NotificationMessage')
                    matched = []
                    for message in messages:
                        if message.find(WSNT + 'Topic').text == 'tns1:' + TOPICS[event_type]:
                            matched.append(message.find('.//' + TT + 'Data/' + TT + 'SimpleItem').attrib)
                    self.assertIn({'Name': 'State', 'Value': 'true' if state == 'new' else 'false'}, matched)
            for other in range(29):
                if other != index:
                    response = self.post(other, paths[other], pull)
                    self.assertEqual(response.status_code, 200)
                    self.assertFalse(ET.fromstring(response.data).findall('.//' + WSNT + 'NotificationMessage'))
