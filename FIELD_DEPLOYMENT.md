# VM104 field deployment

Release branch: `ryoman123/Tonys-Onvf-RTSP-Server:unified-grand-design`.
The existing `onvif-vcam-server` container and `onvif-macvlan.service` remain
rollback targets. Nothing in this runbook installs or modifies Protect itself.

## Before cutover

Run these commands **inside VM104**, using its console or SSH. Keep the console
available during cutover. `/opt/onvif-vcam/config.yml` must be the YAML currently
used by the original bridge. If your path or container name differs, supply the
corresponding `prepare` flags. The VM must have Docker, `ip`, `systemctl`, Python 3,
and Git; the candidate image supplies FFprobe and other Python dependencies.

Use the exact release commit whose unit tests, image build, and startup smoke
check succeeded. Do not deploy from a moving branch without recording its commit.

```bash
sudo git clone --branch unified-grand-design --single-branch \
  https://github.com/ryoman123/Tonys-Onvf-RTSP-Server.git /opt/onvif-unified-release
cd /opt/onvif-unified-release
git rev-parse HEAD
```

Prefer the GHCR **digest** recorded by the successful production workflow:

```bash
IMAGE='ghcr.io/ryoman123/tonys-onvif-rtsp-server@sha256:REPLACE_WITH_PUBLISHED_DIGEST'
```

If registry access is unavailable, build the checked-out, verified release on the
VM and use the immutable local image ID. This leaves the old bridge running:

```bash
sudo docker build --label "org.opencontainers.image.revision=$(git rev-parse HEAD)" \
  -t onvif-unified:field .
IMAGE=$(sudo docker image inspect --format '{{.Id}}' onvif-unified:field)
```

Stage the migration and source probes without taking down the old runtime:

```bash
sudo python3 tools/field_deploy.py prepare \
  --image "$IMAGE" \
  --source-config /opt/onvif-vcam/config.yml \
  --legacy-container onvif-vcam-server \
  --legacy-service onvif-macvlan.service \
  --parent ens19 \
  --stage /opt/onvif-unified-field-01
```

`prepare` requires exactly 29 cameras, static IPv4 macvlans, unique IP/MAC/UUID,
unique identity fields and stream paths, ONVIF port 80, and RTSP port 8554. It
preserves recorder-specific credentials, MAC-derived discovery UUIDs, profile
and encoder tokens. It probes all 58 source streams **sequentially** and writes
the observed codec/resolution/frame rate into the staged config. The source YAML
is preserved. Only verified `vcam-*` interfaces with the exact camera MAC and IP
are eligible for removal. The image is already present before downtime begins.

The stage directory is mode 0700. Configuration, environment capture, container
inspection and rollback state are mode 0600 and may contain credentials. Keep
these local and out of GitHub. A failed prepare leaves the running bridge intact;
use a fresh stage directory after fixing the cause.

## Cutover and automatic rollback

```bash
sudo python3 tools/field_deploy.py deploy --stage /opt/onvif-unified-field-01
```

This stops the old container and macvlan service, disables the old enabled service
for reboot safety, releases only the captured camera links, then starts
`onvif-unified` with host networking, NET_ADMIN, persistent data and restart policy.
It sets IGMP membership capacity to 128 in the host network namespace.

The startup gate requires all 29 identities and IPs, HTTP/discovery readiness,
active PullPoint subscribers on every camera, and connectivity for all enabled
analytics producers. It probes all 58 **MediaMTX output streams** through loopback,
then runs a five-minute continuity soak. Failure triggers rollback automatically;
Ctrl+C during cutover also triggers rollback. A machine power loss cannot execute
an in-process rollback: retain the stage and use the command below after reboot.

An active PullPoint consumer proves subscription activity, not that a person event
was written into Protect. The automated success message explicitly leaves that
final observation to live validation. Disabled analytics producers do not count
as configured or tested integrations.

## Manual rollback

```bash
cd /opt/onvif-unified-release
sudo python3 tools/field_deploy.py rollback --stage /opt/onvif-unified-field-01
```

Rollback stops/removes the candidate, removes its camera NICs, recreates the
original interfaces with their MAC/IP settings, restores the old service enable
state, and starts the original container. It retains the original image, config,
and volumes throughout. Verify stream recovery in Protect after rollback.

## Protect-side acceptance

Before committing to the new runtime, confirm in Protect:

- All 29 existing camera records remain present; no replacement identities.
- HQ and LQ each play correctly, with actual H.265 where configured.
- Motion starts and clears on the correct camera.
- Real person/vehicle/animal/package detections appear on the correct timeline
  when their producer and Protect-side listener are enabled.
- Thumbnails and notifications work if configured; do not confuse a generated
  bridge event with a Protect timeline event.
- A controlled candidate restart restores identities, streams and subscriptions.

The old bridge exposed recorder RTSP paths, while this runtime exposes
`/<pathName>_main` and `/<pathName>_sub`. Profile/encoder tokens are preserved, but
Protect may need to refresh stream URIs during reconnection. If existing records
continue requesting cached old URLs, roll back and investigate; do not remove
and re-adopt cameras merely to make the field gate pass.

## Smart detections and reference sources

Native ONVIF motion and external object classification are separate paths.
Tony's Protect-side integration references
[`danielwoz/ubiquiti-protect-onvif-event-listener`](https://github.com/danielwoz/ubiquiti-protect-onvif-event-listener).
That third-party service consumes PullPoints and writes smart events into Protect;
it also patches Protect database flags/UI and may need repair after firmware
updates. **It is not Ubiquiti software and it is not installed by this cutover.**
Check its version and status on the NVR before expecting smart timeline events:
`systemctl status onvif-recorder`. Person/vehicle and animal/package topic mappings
in this release were checked against its `src/detection_recorder.cpp` State-topic
classifier. This source inspection is not a live interoperability test.

Protocol references are [ONVIF released specifications](https://www.onvif.org/profiles/specifications/),
[`onvif/specs`](https://github.com/onvif/specs), and
[`onvif/testspecs`](https://github.com/onvif/testspecs). The specifications GitHub
contains development work; released specifications are the baseline. These
regressions are not official ONVIF certification. Media1 H.265 compatibility with
Protect remains a field check; passing our XML tests does not establish universal
standards conformance.

[`uilibs/uiprotect`](https://github.com/uilibs/uiprotect) is a community client/API
reference, not the Protect server implementation. Ubiquiti's
[third-party camera guidance](https://help.ui.com/hc/en-us/articles/26301104828439-Third-Party-Cameras-in-UniFi-Protect)
remains the supported integration reference.
