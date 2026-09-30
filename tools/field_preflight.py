#!/usr/bin/env python3
"""Offline configuration validation and sequential live source probes.

Never prints source URLs, credentials, ffprobe stderr or subprocess arguments.
Use --refresh-metadata on the staged config to replace guessed defaults with
observations. This never changes the original bridge YAML or running cameras.
"""
from __future__ import annotations
import argparse
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
from fractions import Fraction

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.device_identity import identity_manifest, normalize_identity, normalize_mac, normalize_uuid, virtual_nic_name


def write_private(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(data, stream, indent=2)
        stream.write('\n')


def validate_config(config, expected=29):
    cameras = config.get('cameras', [])
    if not cameras or len(cameras) != expected:
        raise ValueError(f'expected {expected} cameras, found {len(cameras)}')
    fields = ('id', 'name', 'pathName', 'uuid', 'nicMac', 'staticIp')
    seen = {field: set() for field in fields}
    nics = set()
    manifest = []
    for index, camera in enumerate(cameras, 1):
        for field in fields:
            value = camera.get(field)
            if field == 'nicMac':
                value = normalize_mac(value)
            elif field == 'uuid':
                value = normalize_uuid(value) if value else None
            if value is None or value == '' or value in seen[field]:
                raise ValueError(f'camera {index}: missing or duplicate {field}')
            seen[field].add(value)
        if not camera.get('useVirtualNic') or camera.get('ipMode') != 'static':
            raise ValueError(f'camera {index}: field cutover requires a static virtual NIC')
        interface = ipaddress.ip_interface(f"{camera['staticIp']}/{camera.get('netmask', '24')}")
        if interface.version != 4 or interface.ip.is_loopback or interface.ip.is_multicast:
            raise ValueError(f'camera {index}: invalid field camera IP')
        if not camera.get('parentInterface'):
            raise ValueError(f'camera {index}: parent interface missing')
        if camera.get('onvifPort') != 80 or camera.get('rtspPort') != 8554:
            raise ValueError(f'camera {index}: field cutover requires ONVIF 80 and RTSP 8554')
        if not camera.get('autoStart'):
            raise ValueError(f'camera {index}: Auto Start must be enabled')
        if camera.get('transcodeMain') or camera.get('transcodeSub'):
            raise ValueError(f'camera {index}: field profile requires video passthrough')
        for kind in ('main', 'sub'):
            if not str(camera.get(kind + 'StreamUrl', '')).startswith('rtsp://'):
                raise ValueError(f'camera {index}: {kind} source URL missing')
            if camera.get(kind + 'Encoding') not in ('H264', 'H265'):
                raise ValueError(f'camera {index}: {kind} codec missing')
            for suffix in ('Width', 'Height', 'Framerate'):
                if int(camera.get(kind + suffix, 0)) <= 0:
                    raise ValueError(f'camera {index}: invalid {kind} {suffix}')
        nic = virtual_nic_name(camera['uuid'])
        if nic in nics:
            raise ValueError('virtual NIC name collision')
        nics.add(nic)
        identity = normalize_identity(camera.get('identity'), camera_name=camera['name'], mac=camera['nicMac'])
        item = identity_manifest(device_uuid=camera['uuid'], mac=camera['nicMac'], identity=identity)
        manifest.append({'name': camera['name'], 'effectiveIp': camera['staticIp'],
                         'onvifPort': 80, 'rtspPort': 8554, **item})
    for field in ('serialNumber', 'hardwareId'):
        if len({item[field] for item in manifest}) != len(manifest):
            raise ValueError(f'duplicate {field}')
    return {'cameras': manifest}


def probe_video(url, timeout=20):
    try:
        result = subprocess.run(['ffprobe', '-v', 'error', '-rtsp_transport', 'tcp',
            '-select_streams', 'v:0', '-show_entries',
            'stream=codec_name,width,height,avg_frame_rate,r_frame_rate', '-of', 'json', url],
            capture_output=True, text=True, timeout=timeout, check=True)
        stream = json.loads(result.stdout)['streams'][0]
        codec = {'h264': 'H264', 'hevc': 'H265', 'h265': 'H265'}[stream['codec_name']]
        try:
            fps = Fraction(stream.get('avg_frame_rate', '0/1'))
        except (ValueError, ZeroDivisionError):
            fps = Fraction(0)
        if fps <= 0:
            fps = Fraction(stream['r_frame_rate'])
        width, height = int(stream['width']), int(stream['height'])
        if min(width, height) <= 0 or fps <= 0:
            raise ValueError('invalid video metadata')
        return {'Encoding': codec, 'Width': width, 'Height': height, 'Framerate': max(1, round(float(fps)))}
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, IndexError, ZeroDivisionError):
        raise ValueError('source video probe failed (details suppressed to protect credentials)') from None


def probe_config(config, refresh=False, timeout=20):
    for index, camera in enumerate(config['cameras'], 1):
        for kind in ('main', 'sub'):
            try:
                observed = probe_video(camera[kind + 'StreamUrl'], timeout)
            except ValueError:
                raise ValueError(f'camera {index} {kind}: source video probe failed') from None
            for suffix, value in observed.items():
                if refresh:
                    camera[kind + suffix] = value
                elif camera.get(kind + suffix) != value:
                    raise ValueError(f'camera {index} {kind}: configured {suffix} differs from source')
            print(f"camera {index} {kind}: {observed['Encoding']} {observed['Width']}x{observed['Height']} {observed['Framerate']} fps", flush=True)
    return config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config')
    parser.add_argument('--expected-cameras', type=int, default=29)
    parser.add_argument('--probe-streams', action='store_true')
    parser.add_argument('--refresh-metadata', action='store_true')
    parser.add_argument('--output-streams', action='store_true', help='Probe loopback MediaMTX outputs after cutover')
    parser.add_argument('--manifest')
    args = parser.parse_args(argv)
    config = json.loads(Path(args.config).read_text())
    validate_config(config, args.expected_cameras)
    if args.output_streams and args.refresh_metadata:
        raise ValueError('output probing cannot refresh source config')
    if args.output_streams:
        import copy
        output = copy.deepcopy(config)
        for camera in output['cameras']:
            for kind in ('main', 'sub'):
                camera[kind + 'StreamUrl'] = f"rtsp://127.0.0.1:8554/{camera['pathName']}_{kind}"
        probe_config(output)
    elif args.probe_streams or args.refresh_metadata:
        probe_config(config, refresh=args.refresh_metadata)
    manifest = validate_config(config, args.expected_cameras)
    if args.refresh_metadata:
        write_private(args.config, config)
    if args.manifest:
        write_private(args.manifest, manifest)
    print(f"PASS: {len(config['cameras'])} unique cameras; config and identity manifest validated")
    return 0

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        print(f'FAIL: {error}', file=sys.stderr)
        raise SystemExit(1)
