#!/usr/bin/env python3
"""Offline production-image exercise: real inference, HEVC relay and ONVIF.

Run in the built image with --network none. It uses only a bundled sample and
loopback; no physical camera, NVR, credential or notification service is used.
This checks image capabilities, not object accuracy at a customer site.
"""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cv2
import numpy as np
import yaml
from ultralytics.utils import ASSETS

from app.ai_alerts import alert_store
from app.ai_device import get_shared_model, get_shared_plate_model, get_shared_ocr_reader
from app.camera import VirtualONVIFCamera
from app.mediamtx_manager import MediaMTXManager
from app.onvif_service import ONVIFService
from app.runtime_health import local_ai_readiness
from app.stream_paths import internal_rtsp_url, stream_path

WSA = '{http://www.w3.org/2005/08/addressing}'
WSNT = '{http://docs.oasis-open.org/wsn/b-2}'
TT = '{http://www.onvif.org/ver10/schema}'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def exercise():
    sample = Path(ASSETS) / 'bus.jpg'
    require(sample.is_file(), 'YOLO sample is not bundled')
    model = get_shared_model('yolov8n.pt')
    results = model(str(sample), verbose=False)
    labels = {result.names[int(box.cls)] for result in results for box in result.boxes}
    require({'person', 'bus'} <= labels, 'real YOLO inference did not identify expected sample objects')
    # Fail if the optional inherited plate/OCR features need a runtime download.
    get_shared_plate_model()(np.zeros((320, 320, 3), dtype=np.uint8), verbose=False)
    reader = get_shared_ocr_reader()
    image = np.full((120, 480, 3), 255, dtype=np.uint8)
    cv2.putText(image, 'ABC123', (20, 85), cv2.FONT_HERSHEY_SIMPLEX, 2.3, (0, 0, 0), 4)
    text = ''.join(reader.readtext(image, detail=0)).replace(' ', '').upper()
    require('ABC123' in text, 'bundled OCR failed the English plate-text sample')
    print('PASS: real YOLO detection, pinned plate-model inference and offline OCR', flush=True)

    auth_user, auth_password = 'smoke-relay', 'relay-test-password'
    # gortsplib's synthetic Basic-auth server rejects ':' in passwords.
    # Keep reserved URL characters without conflating that server limitation
    # with the source client's URL-decoding support.
    source_user, source_password = 'source@', 'p/?#'
    source_reads = []
    class AuthHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            expected_user, expected_password = auth_user, auth_password
            if data.get('action') == 'read' and data.get('path') == 'input':
                expected_user, expected_password = source_user, source_password
            valid = data.get('user') == expected_user and data.get('password') == expected_password
            if valid and data.get('path') == 'input' and data.get('action') == 'read':
                source_reads.append(True)
            self.send_response(200 if valid else 401)
            self.end_headers()
        def log_message(self, *args):
            pass

    auth = HTTPServer(('127.0.0.1', 5552), AuthHandler)
    auth_thread = threading.Thread(target=auth.serve_forever, daemon=True)
    auth_thread.start()
    notifications = []
    manager = SimpleNamespace(rtsp_port=18554, rtsp_auth_enabled=True,
        global_username=auth_user, global_password=auth_password, server_ip='127.0.0.1',
        notifier=SimpleNamespace(send_ai_detection=lambda **data: notifications.append(data)),
        onvif_events=[], is_ip_whitelisted=lambda ip: False)
    camera = VirtualONVIFCamera({'id': 1, 'name': 'Offline image smoke', 'pathName': 'smoke',
        'mainStreamUrl': 'rtsp://source%40:p%2F%3F%23@127.0.0.1:18554/input', 'subStreamUrl': '',
        'rtspPort': 18554, 'mainEncoding': 'H265', 'mainWidth': 640, 'mainHeight': 480,
        'mainFramerate': 4, 'disableSubstream': True, 'enableEventForwarding': True,
        'eventSource': 'ai', 'aiTargets': ['person', 'vehicle'], 'aiMotionDetectionEnabled': False,
        'aiConfidenceThreshold': 25, 'onvifUsername': 'onvif-smoke', 'onvifPassword': 'test-only',
        'notifyAiEnabled': True, 'notifyAiAttachImage': True}, manager)
    camera.status = 'running'
    dual = VirtualONVIFCamera({'id': 2, 'name': 'Dual-profile smoke', 'pathName': 'dual',
        'mainStreamUrl': camera.main_stream_url, 'subStreamUrl': camera.main_stream_url,
        'mainEncoding': 'H265', 'subEncoding': 'H265', 'mainWidth': 640, 'mainHeight': 480,
        'subWidth': 640, 'subHeight': 480, 'mainFramerate': 4, 'subFramerate': 4,
        'rtspPort': 18554}, manager)
    dual.status = 'running'
    camera.onvif_service = ONVIFService(camera)
    client = camera.onvif_service.create_app().test_client()
    def post(path, body):
        envelope = '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tev="http://www.onvif.org/ver10/events/wsdl"><s:Body>' + body + '</s:Body></s:Envelope>'
        response = client.post(path, data=envelope, auth=(camera.onvif_username, camera.onvif_password))
        require(response.status_code == 200, 'authenticated ONVIF request failed')
        return ET.fromstring(response.data)
    response = post('/onvif/events_service', '<tev:CreatePullPointSubscription><tev:InitialTerminationTime>PT600S</tev:InitialTerminationTime></tev:CreatePullPointSubscription>')
    pull_path = urlparse(response.find('.//' + WSA + 'Address').text).path
    pull = '<tev:PullMessages><tev:Timeout>PT0S</tev:Timeout><tev:MessageLimit>256</tev:MessageLimit></tev:PullMessages>'
    post(pull_path, pull)
    processes = []
    try:
        with tempfile.TemporaryDirectory() as directory:
            relay = MediaMTXManager()
            relay.config_file = str(Path(directory) / 'mediamtx.yml')
            relay.create_config([camera, dual], rtsp_port=18554, rtsp_username=auth_user, rtsp_password=auth_password)
            config = yaml.safe_load(Path(relay.config_file).read_text())
            config['paths']['input'] = {'source': 'publisher'}
            config['logLevel'] = 'error'
            Path(relay.config_file).write_text(yaml.safe_dump(config))
            logs = open(Path(directory) / 'media.log', 'w+')
            server = subprocess.Popen([str(Path('mediamtx').resolve()), relay.config_file], stdout=logs, stderr=logs)
            processes.append(server)
            time.sleep(1)
            require(server.poll() is None, 'production MediaMTX rejected the generated configuration')
            publisher = subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
                '-re', '-loop', '1', '-i', str(sample), '-vf',
                'scale=640:480:force_original_aspect_ratio=decrease,pad=640:480:(ow-iw)/2:(oh-ih)/2',
                '-c:v', 'libx265', '-preset', 'ultrafast', '-tune', 'zerolatency',
                '-x265-params', 'pools=1:frame-threads=1:log-level=error', '-pix_fmt', 'yuv420p',
                '-r', '4', '-g', '4', '-an', '-f', 'rtsp', '-rtsp_transport', 'tcp',
                f'rtsp://{auth_user}:{auth_password}@127.0.0.1:18554/input'],
                stdout=logs, stderr=logs)
            processes.append(publisher)
            camera.start_ai_detection()
            deadline = time.monotonic() + 90
            seen = set()
            def collect():
                values = set()
                for item in post(pull_path, pull).findall('.//' + WSNT + 'NotificationMessage'):
                    values.add((item.find(WSNT + 'Topic').text, item.find('.//' + TT + 'Data/' + TT + 'SimpleItem').attrib['Value']))
                return values
            while time.monotonic() < deadline:
                seen.update(collect())
                if all(('tns1:' + topic, 'true') in seen for topic in
                       ('UserAlarm/IVA/HumanShapeDetect', 'VehicleAlarm/IVB/VehicleDetect')):
                    break
                require(server.poll() is None and publisher.poll() is None, 'loopback HEVC source stopped')
                time.sleep(.5)
            require(local_ai_readiness(camera)['ready'], 'detector did not decode fresh authenticated HEVC frames')
            require(source_reads, 'URL-encoded recorder credentials were not authenticated')
            for topic in ('UserAlarm/IVA/HumanShapeDetect', 'VehicleAlarm/IVB/VehicleDetect'):
                require(('tns1:' + topic, 'true') in seen, 'real RTSP inference did not reach authenticated PullMessages')
            require(notifications and notifications[0].get('image_bytes'), 'annotated notification snapshot was not produced')
            require(alert_store.list_alerts(camera_id=1), 'AI alert history was not saved')
            probes = [(item, kind, browser, codec) for item, kinds in
                      ((camera, ('main',)), (dual, ('main', 'sub')))
                      for kind in kinds for browser, codec in ((False, 'hevc'), (True, 'h264'))]
            for item, kind, browser, codec in probes:
                url = internal_rtsp_url(item, kind).rsplit('/', 1)[0] + '/' + stream_path(item, kind, browser=browser)
                result = subprocess.run(['ffprobe', '-v', 'error', '-rtsp_transport', 'tcp', '-timeout', '30000000',
                    '-select_streams', 'v:0', '-show_entries', 'stream=codec_name', '-of', 'json', url],
                    capture_output=True, text=True, timeout=40)
                require(result.returncode == 0, 'recorder/browser RTSP probe failed')
                require(json.loads(result.stdout)['streams'][0]['codec_name'] == codec, 'recorder/browser codec drift')
            camera.stop_ai_detection()
            seen.update(collect())
            for topic in ('UserAlarm/IVA/HumanShapeDetect', 'VehicleAlarm/IVB/VehicleDetect'):
                require(('tns1:' + topic, 'false') in seen, 'local detector stop did not clear ONVIF state')
            print('PASS: authenticated HEVC -> local YOLO -> authenticated ONVIF start/clear, alert snapshots and browser H.264 preview', flush=True)
    except Exception:
        if 'logs' in locals():
            logs.flush()
            logs.seek(0)
            print('Loopback media diagnostics:', logs.read()[-5000:], flush=True)
        raise
    finally:
        camera.stop_ai_detection()
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        auth.shutdown()
        auth.server_close()


if __name__ == '__main__':
    exercise()
