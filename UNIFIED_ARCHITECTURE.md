# Unified Grand Design

This branch uses **Tony's ONVIF-RTSP Server as the product/runtime model** and ports the hardened behavior from
`ryoman123/onvif-virtual-camera` into it. The goal is one application, not two side-by-side bridges.

## Ground rules

- `main` remains a clean mirror of `BigTonyTones/Tonys-Onvf-RTSP-Server` so upstream changes stay easy to inspect and merge.
- `unified-grand-design` is our production-candidate branch.
- The current `ryoman123/onvif-virtual-camera` deployment remains the known-good reference and rollback target until the unified build passes the same 29-camera field gate.
- Existing Protect-facing identity (MAC/IP/UUID/profile behavior) must not change accidentally.
- No production deployment is accepted on CI alone: it must pass real Protect adoption/stream/event checks and a soak.

## What stays from Tony's project

- Web UI and camera CRUD
- MediaMTX orchestration
- FFmpeg probing/transcoding/audio controls
- Virtual NIC lifecycle
- Local AI, zones and notifications
- Native ONVIF event forwarding
- Diagnostics and operational tooling
- Cross-platform installer/runtime support

## What we port/harden from onvif-virtual-camera

1. Deterministic lifecycle and identity guarantees
2. Strict ONVIF Device/Media behavior and SOAP faults
3. PullPoint subscription semantics, retained property state, synchronization, renew/unsubscribe
4. Stable HQ/LQ metadata and HQ snapshot compatibility
5. Normalized motion/person/vehicle/animal/package event state
6. Frigate MQTT adapter
7. Lorex/Dahua native recorder event adapter
8. Multi-source event aggregation so one source cannot clear another source's active property
9. Health/readiness telemetry suitable for automated acceptance
10. Identity manifests, soak gates, immutable-image deployment and rollback
11. Secret references instead of requiring credentials in persistent config
12. Regression/CI coverage for Protect-facing behavior

## Phase 1 — lifecycle correctness

The first change on this branch fixes the restart race documented by Tony issue #65:

- camera stop is synchronous and bounded
- the HTTP listener is closed before a replacement can bind
- the old Flask server thread must be joined before restart
- WS-Discovery is explicitly stopped and joined
- the physical-camera ONVIF event-forwarder thread is explicitly joined
- reconnect sleeps are interruptible
- a stale listener is treated as an error instead of silently reporting “already running”

This is foundational because every later subsystem depends on camera edit/restart being transactional.

## Phase 2 — ONVIF protocol core

Port the proven behavior from `onvif-virtual-camera` into Tony's Python service while preserving Tony's API/UI:

- Device service capability/identity behavior
- Media profile tokens and exact stream metadata
- proper action dispatch/faults
- PullPoint state engine and topic descriptions
- synchronization/renew/unsubscribe
- bounded long polls and queue accounting
- Protect-compatible event wire format

## Phase 3 — unified event producers

All producers feed one normalized state engine:

```
Tony local AI ─┐
Native camera ONVIF ─┤
Lorex/Dahua events ──┼─> normalized property state ─> ONVIF PullPoint ─> Protect
Frigate MQTT ─────────┘
```

Each producer is independently selectable per camera. Overlapping producers are reference-counted/aggregated.

## Phase 4 — production acceptance

Before replacing the current VM104 runtime, the unified image must demonstrate:

- expected 29/29 camera identities
- unique/stable MAC, UUID, serial and hardware identity
- HQ/LQ stream metadata matching live video
- all ONVIF listeners and WS-Discovery responders healthy
- active Protect PullPoint subscribers
- event delivery on every camera
- recorder/Frigate reconnect recovery
- controlled container restart recovery
- no camera identity replacement in Protect
- sustained soak without listener, stream or subscription loss

Only after that gate passes do we make the Tony-based unified build the primary runtime.
