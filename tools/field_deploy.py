#!/usr/bin/env python3
"""Transactional, static-IP VM104 cutover. Run on the VM as root.

prepare pulls an immutable image, captures rollback state and probes staged
sources while the old bridge remains running. deploy stops the old runtime,
releases only its verified camera macvlans and gates the new runtime. rollback
restores the original links/service/container. No NVR database changes occur.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.field_preflight import validate_config, write_private
from tools.acceptance_check import fetch_json, continuity_failures
from app.runtime_health import evaluate_acceptance
from app.device_identity import virtual_nic_name

NEW_CONTAINER = 'onvif-unified'
IMAGE_RE = re.compile(r'^(?:ghcr\.io/ryoman123/tonys-onvif-rtsp-server@)?sha256:[a-f0-9]{64}$')


def run(args, *, timeout=120, check=True):
    # Output/errors may include secrets. Keep them local; never echo commands.
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f'{args[0]} operation failed; see local service/container logs')
    return result


def inspect_container(name):
    return json.loads(run(['docker', 'inspect', name]).stdout)[0]


def image_tool(plan, command):
    stage = str(Path(plan['stage']).resolve())
    return run(['docker', 'run', '--rm', '--network', 'host',
        '--env-file', stage + '/legacy.env', '-v', stage + ':/work',
        plan['image'], 'python', *command], timeout=1800)


def capture_interfaces(config, interfaces):
    """Capture only a matching MAC+IP macvlan; reject aliasing or host IPs."""
    found = []
    for camera in config['cameras']:
        matches = [item for item in interfaces if item.get('address', '').lower() == camera['nicMac'].lower()
            and any(address.get('local') == camera['staticIp'] for address in item.get('addr_info', []))]
        if len(matches) != 1:
            raise ValueError(f"camera {camera['id']}: expected one existing MAC/IP interface")
        item = matches[0]
        if item.get('linkinfo', {}).get('info_kind') != 'macvlan':
            raise ValueError(f"camera {camera['id']}: existing interface is not a macvlan")
        name = item['ifname'].split('@', 1)[0]
        if not name.startswith('vcam-'):
            raise ValueError(f"camera {camera['id']}: unexpected legacy interface name")
        parent_index = item.get('link_index')
        parent = next((entry['ifname'].split('@', 1)[0] for entry in interfaces
                       if entry['ifindex'] == parent_index), item.get('link'))
        if parent != camera['parentInterface']:
            raise ValueError(f"camera {camera['id']}: legacy parent interface differs")
        addresses = [f"{entry['local']}/{entry['prefixlen']}" for entry in item.get('addr_info', [])
                     if entry.get('family') == 'inet']
        found.append({'name': name, 'parent': parent, 'mac': item['address'], 'addresses': addresses})
    return found


def prepare(args):
    if not IMAGE_RE.fullmatch(args.image):
        raise ValueError('image must be a published GHCR digest or immutable local image ID')
    if args.legacy_container == NEW_CONTAINER:
        raise ValueError('legacy and candidate containers must differ')
    stage = Path(args.stage).resolve()
    if stage.exists():
        raise ValueError('stage already exists; use a fresh directory')
    old = inspect_container(args.legacy_container)
    if not old['State']['Running'] or old['HostConfig']['NetworkMode'] != 'host':
        raise ValueError('legacy container must be running with host networking')
    if run(['docker', 'inspect', NEW_CONTAINER], check=False).returncode == 0:
        raise ValueError('candidate container already exists; handle the previous transaction first')
    source = Path(args.source_config).resolve()
    if not source.is_file():
        raise ValueError('source config does not exist')
    stage.mkdir(mode=0o700, parents=True)
    os.chmod(stage, 0o700)
    write_private(stage / 'legacy-inspect.json', old)
    shutil.copyfile(source, stage / 'source.yml')
    os.chmod(stage / 'source.yml', 0o600)
    env = old['Config'].get('Env') or []
    if any('\n' in value or '\r' in value for value in env):
        raise ValueError('multiline container environment cannot be safely migrated')
    (stage / 'legacy.env').write_text('\n'.join(env) + '\n')
    os.chmod(stage / 'legacy.env', 0o600)
    if args.image.startswith('ghcr.io/'):
        run(['docker', 'pull', args.image], timeout=1800)
    else:
        run(['docker', 'image', 'inspect', args.image])
    enabled = run(['systemctl', 'is-enabled', args.legacy_service], check=False).stdout.strip()
    active = run(['systemctl', 'is-active', args.legacy_service], check=False).stdout.strip() == 'active'
    if enabled not in ('enabled', 'disabled', 'static'):
        raise ValueError('legacy macvlan service must exist and have a supported enable state')
    plan = {'stage': str(stage), 'image': args.image, 'legacyContainer': args.legacy_container,
            'legacyId': old['Id'], 'legacyImage': old['Image'],
            'legacyService': args.legacy_service, 'serviceEnabled': enabled,
            'serviceActive': active, 'expectedCameras': args.expected_cameras,
            'soakSeconds': args.soak_seconds, 'phase': 'preparing'}
    result = image_tool(plan, ['tools/import_vcam_config.py', '/work/source.yml', '--output',
                             '/work/camera_config.json', '--parent-interface', args.parent, '--preserve-port-80'])
    print(result.stdout.strip())
    config = json.loads((stage / 'camera_config.json').read_text())
    validate_config(config, args.expected_cameras)
    # Validate/probe in the new image, avoiding extra host Python dependencies.
    result = image_tool(plan, ['tools/field_preflight.py', '/work/camera_config.json',
        '--expected-cameras', str(args.expected_cameras), '--refresh-metadata', '--manifest', '/work/identity.json'])
    print(result.stdout.strip())
    config = json.loads((stage / 'camera_config.json').read_text())
    interfaces = json.loads(run(['ip', '-j', '-d', 'address', 'show']).stdout)
    plan['interfaces'] = capture_interfaces(config, interfaces)
    plan['candidateNics'] = [virtual_nic_name(camera['uuid']) for camera in config['cameras']]
    if any(item['ifname'] in plan['candidateNics'] for item in interfaces):
        raise ValueError('candidate NIC already exists')
    plan['configSha256'] = __import__('hashlib').sha256((stage / 'camera_config.json').read_bytes()).hexdigest()
    plan['phase'] = 'prepared'
    write_private(stage / 'deployment.json', plan)
    print(f'PREPARED: {args.expected_cameras} cameras; old bridge remains running')


def restore_interface(item):
    existing = run(['ip', '-j', 'link', 'show', item['name']], check=False)
    if existing.returncode == 0:
        current = json.loads(existing.stdout)[0]
        if current.get('address', '').lower() != item['mac'].lower():
            raise RuntimeError('rollback interface name has been reused by another device')
    else:
        run(['ip', 'link', 'add', item['name'], 'link', item['parent'], 'type', 'macvlan', 'mode', 'bridge'])
        run(['ip', 'link', 'set', item['name'], 'address', item['mac']])
    run(['ip', 'link', 'set', item['name'], 'up'])
    for address in item['addresses']:
        run(['ip', 'address', 'replace', address, 'dev', item['name']])
    run(['sysctl', '-w', f"net.ipv4.conf.{item['name']}.arp_ignore=1"])
    run(['sysctl', '-w', f"net.ipv4.conf.{item['name']}.arp_announce=2"])


def rollback(plan):
    print('ROLLBACK: stopping candidate and restoring original camera interfaces')
    candidate = run(['docker', 'inspect', NEW_CONTAINER], check=False)
    if candidate.returncode == 0:
        run(['docker', 'stop', '--time', '120', NEW_CONTAINER], timeout=150)
        run(['docker', 'rm', NEW_CONTAINER])
    for name in plan['candidateNics']:
        if run(['ip', 'link', 'show', name], check=False).returncode == 0:
            run(['ip', 'link', 'delete', name])
    for item in plan['interfaces']:
        restore_interface(item)
    if plan['serviceEnabled'] == 'enabled':
        run(['systemctl', 'enable', plan['legacyService']])
    if plan['serviceActive']:
        run(['systemctl', 'restart', plan['legacyService']])
    run(['docker', 'start', plan['legacyContainer']])
    if not inspect_container(plan['legacyContainer'])['State']['Running']:
        raise RuntimeError('legacy container did not resume')
    plan['phase'] = 'rolled-back'
    write_private(Path(plan['stage']) / 'deployment.json', plan)
    print('ROLLBACK COMPLETE: original container is running; verify its streams in Protect')


def wait_acceptance(plan, timeout=300):
    manifest = json.loads((Path(plan['stage']) / 'identity.json').read_text())
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status = fetch_json('http://127.0.0.1:5552/api/readiness')
            result = evaluate_acceptance(status, expected_cameras=plan['expectedCameras'],
                expected_manifest=manifest, require_pullpoint_subscribers=True, require_analytics=True)
            if result['passed']:
                return status
        except (OSError, ValueError):
            pass
        time.sleep(5)
    raise RuntimeError('candidate failed the 29-camera identity/listener/PullPoint/analytics gate')


def deploy(plan):
    if plan['phase'] != 'prepared':
        raise ValueError('deployment requires a freshly prepared transaction')
    stage = Path(plan['stage'])
    if __import__('hashlib').sha256((stage / 'camera_config.json').read_bytes()).hexdigest() != plan['configSha256']:
        raise ValueError('staged config changed; prepare a new transaction')
    # Confirm the legacy links/container still match the prepared inventory.
    config = json.loads((stage / 'camera_config.json').read_text())
    current = capture_interfaces(config, json.loads(run(['ip', '-j', '-d', 'address', 'show']).stdout))
    old = inspect_container(plan['legacyContainer'])
    if (current != plan['interfaces'] or not old['State']['Running']
            or old['Id'] != plan['legacyId'] or old['Image'] != plan['legacyImage']):
        raise ValueError('legacy inventory changed since prepare')
    data = stage / 'data'
    data.mkdir(mode=0o700)
    shutil.copyfile(stage / 'camera_config.json', data / 'camera_config.json')
    os.chmod(data / 'camera_config.json', 0o600)
    plan['phase'] = 'cutover-started'
    write_private(stage / 'deployment.json', plan)
    try:
        run(['docker', 'stop', '--time', '60', plan['legacyContainer']], timeout=90)
        run(['systemctl', 'stop', plan['legacyService']])
        if plan['serviceEnabled'] == 'enabled':
            run(['systemctl', 'disable', plan['legacyService']])
        for item in plan['interfaces']:
            run(['ip', 'link', 'delete', item['name']])
        run(['sysctl', '-w', 'net.ipv4.igmp_max_memberships=128'])
        run(['docker', 'run', '-d', '--name', NEW_CONTAINER, '--network', 'host',
            '--cap-add', 'NET_ADMIN', '--stop-timeout', '120', '--restart', 'unless-stopped',
            '-v', str(data) + ':/app/data', plan['image']])
        baseline = wait_acceptance(plan)
        output_check = image_tool(plan, ['tools/field_preflight.py', '/work/camera_config.json',
            '--expected-cameras', str(plan['expectedCameras']), '--output-streams'])
        print(output_check.stdout.strip())
        # The output probe can run for several minutes. Recheck Protect and identity afterwards.
        baseline = wait_acceptance(plan)
        print('PASS: candidate identity/listener/PullPoint/analytics gate; starting soak')
        deadline = time.monotonic() + plan['soakSeconds']
        while time.monotonic() < deadline:
            time.sleep(min(10, max(0, deadline - time.monotonic())))
            current = wait_acceptance(plan, timeout=15)
            if continuity_failures(baseline, current):
                raise RuntimeError('candidate continuity failed during soak')
            baseline = current
        plan['phase'] = 'accepted'
        write_private(stage / 'deployment.json', plan)
        print('ACCEPTED: automated gate passed. Verify HQ/LQ playback and real smart events in Protect.')
    except BaseException:
        rollback(plan)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--image', required=True)
    prep.add_argument('--source-config', default='/opt/onvif-vcam/config.yml')
    prep.add_argument('--legacy-container', default='onvif-vcam-server')
    prep.add_argument('--legacy-service', default='onvif-macvlan.service')
    prep.add_argument('--parent', default='ens19')
    prep.add_argument('--stage', required=True)
    prep.add_argument('--expected-cameras', type=int, default=29)
    prep.add_argument('--soak-seconds', type=int, default=300)
    for operation in ('deploy', 'rollback'):
        child = sub.add_parser(operation)
        child.add_argument('--stage', required=True)
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        raise ValueError('run on VM104 with sudo/root')
    if args.operation == 'prepare':
        if args.soak_seconds < 60:
            raise ValueError('cutover soak must be at least 60 seconds')
        return prepare(args)
    plan = json.loads((Path(args.stage) / 'deployment.json').read_text())
    if args.operation == 'deploy':
        deploy(plan)
    else:
        rollback(plan)

if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
        print(f'FAILED: {error}', file=sys.stderr)
        raise SystemExit(1)
