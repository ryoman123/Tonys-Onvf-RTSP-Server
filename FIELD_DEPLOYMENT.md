# VM104 field deployment

Release branch: `ryoman123/Tonys-Onvf-RTSP-Server:unified-grand-design`.
The existing `onvif-vcam-server` container and `onvif-macvlan.service` remain
rollback targets. Nothing in this runbook installs or modifies Protect itself.
Bridge-only acceptance is insufficient for full deployment. Tony's local YOLO,
alert snapshots, notifications, GridFusion and Protect listener manager are
retained. Frigate is optional. See [FEATURE_PARITY.md](FEATURE_PARITY.md).

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
  --local-ai \
  --stage /opt/onvif-unified-field-01
```

`prepare` requires exactly 29 cameras, static IPv4 macvlans, unique IP/MAC/UUID,
unique identity fields and stream paths, ONVIF port 80, and RTSP port 8554. It
preserves recorder-specific credentials, MAC-derived discovery UUIDs, profile
and encoder tokens. It probes all 58 source streams **sequentially** and writes
the observed codec/resolution/frame rate into the staged config. The source YAML
is preserved. Only verified `vcam-*` interfaces with the exact camera MAC and IP
are eligible for removal. The image is already present before downtime begins.
`--local-ai` enables Tony's built-in detector on all migrated cameras, using LQ
where available. Measure inference and queue latency on the actual VM; a
single-camera image test does not establish 29-camera AI capacity. Without this
flag migration does not enable local object detection.

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

The initial gate requires all 29 identities and IPs, HTTP/discovery readiness,
fresh frames and successful warm-up inference for enabled local AI, and
connectivity for enabled external producers. It probes all 58 **MediaMTX output streams** through loopback,
then runs a five-minute continuity soak. Failure triggers rollback automatically;
Ctrl+C during cutover also triggers rollback. A machine power loss cannot execute
an in-process rollback: retain the stage and use the command below after reboot.

This stops at `video-verified`, never full deployment acceptance. A first install
must allow the bridge UI to run so the Protect recorder can be configured next;
requiring its listener before this setup would cause an unavoidable rollback.
Disabled analytics producers do not count as configured or tested integrations.

## Complete the smart-detection path

1. Open `http://192.168.50.250:5552`. Confirm local AI, desired targets and smart
   ONVIF topics are enabled. Confirm actual detections and annotated alert history;
   a test-event button does not prove YOLO recognized an object.
2. In Settings, open **UniFi NVR ONVIF Listener**, add the actual Protect recorder
   and its SSH credentials, and use the retained install/status controls. This
   installs `onvif-recorder` on Protect's recorder, not VM104. Set periodic checks
   to **5 minutes**, enable monitoring and run a status check. Credentials stay
   in the local encrypted config; do not put them in shell arguments or Git.
3. Wait for PullPoint subscribers on every camera. Run inside VM104:

```bash
sudo python3 tools/field_deploy.py verify-smart --stage /opt/onvif-unified-field-01
```

This requires a healthy configured smart producer per camera, active PullPoints,
and an active Protect listener checked within six minutes, then repeats the
continuity soak. Missing targets, disabled detection, stale frames, failed model
warm-up and missing/stale recorder checks fail this gate. A verification failure
leaves the candidate available for diagnosis; use rollback when needed.

The general CLI offers the same strict gate:

```bash
python3 tools/acceptance_check.py --expected-cameras 29 \
  --require-analytics --require-smart-pipeline --soak-seconds 300 --interval-seconds 10
```

`smart-pipeline-verified` and `fullStack.readyForLiveTest` mean the runtime is ready
for real Protect observations. `fullStack.timelineVerified` stays false:
subscription activity and a running process do not prove timeline insertion.

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
- The Tony dashboard plays both profiles and GridFusion layouts still compose,
  with saved layouts, zones, models and notification preferences surviving restart.

The old bridge exposed recorder RTSP paths, while this runtime exposes
`/<pathName>_main` and `/<pathName>_sub`. Profile/encoder tokens are preserved, but
Protect may need to refresh stream URIs during reconnection. If existing records
continue requesting cached old URLs, roll back and investigate; do not remove
and re-adopt cameras merely to make the field gate pass.

## Smart detections and reference sources

Native ONVIF motion and external object classification are separate paths.
Tony's built-in YOLO works without Frigate. Its default COCO model maps bags and
luggage to the "package" target; this is a heuristic, not a parcel-specific model.
Plate recognition uses a separate detector and OCR. Validate actual model
performance on the intended views rather than inferring accuracy from a target.
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
