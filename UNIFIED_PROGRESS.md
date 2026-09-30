# Unified Grand Design — Progress

This file is the live handoff/checkpoint for the Tony-based unified runtime.

## Branch model

- `main`: clean upstream mirror of `BigTonyTones/Tonys-Onvf-RTSP-Server`.
- `unified-grand-design`: production-candidate integration branch.
- Short-lived `unified-*` branches: one hardened subsystem at a time, merged only after CI passes.
- `ryoman123/onvif-virtual-camera:grand-design`: known-good reference implementation and rollback source until the unified runtime passes live acceptance.

## Landed

- Deterministic camera stop/start lifecycle and bounded teardown.
- Explicit WS-Discovery teardown.
- Native physical-camera ONVIF event-forwarder teardown/join.
- CI compile + regression test workflow.
- Retained/property-aware PullPoint event engine.
- Subscription TTL/pruning, topic filters, Renew, Unsubscribe and SetSynchronizationPoint.
- Motion/person/vehicle/animal/package topic descriptions.
- `Changed` and `Initialized` PropertyOperation semantics.
- Tony local AI and physical-camera ONVIF events routed through the unified event engine.
- PullPoint health counters exposed in camera diagnostics.
- Schema-ordered Media profile renderer.
- Exact ProfileToken and VideoEncoder ConfigurationToken validation.
- GetStreamUri StreamSetup validation.
- GetProfiles issue #66 ordering regression fixed: AudioSourceConfiguration precedes VideoEncoderConfiguration.
- Shared VideoSourceConfiguration consistency across main/sub profiles.
- Truthful Device/GetCapabilities/GetServices behavior.
- Device GetServiceCapabilities support.
- Fabricated Analytics/Imaging/DeviceIO capability URLs removed.

## Landed PRs

- #1 — `feat: port hardened PullPoint event core`
- #2 — `fix: make ONVIF media profiles schema-strict`
- #3 — `fix: make ONVIF device capability advertising truthful`

## Deployment candidate additions

- Persistent Protect-facing identity and UI/API plumbing.
- Frigate MQTT and Lorex/Dahua recorder event producers.
- Multi-producer state aggregation.
- Per-stream H.264/H.265 Media metadata.
- Runtime readiness and identity acceptance endpoints.
- Offline bridge migration preserving recorder credentials and profile/encoder tokens.
- Full-UUID NIC naming for MAC-derived legacy UUIDs; failed NIC setup fails closed.
- Protect-listener-compatible person/vehicle/animal/package topic mappings.
- Sequential source/output video preflight, transactional cutover and automatic rollback.
- Immutable production image workflow, container startup smoke check and field runbook.

See [FIELD_DEPLOYMENT.md](FIELD_DEPLOYMENT.md) for VM104 prepare/deploy/rollback.

## Next

1. Complete candidate CI, image build and startup smoke gate, then integrate PR #6.
2. Prepare the actual 29-camera configuration on VM104 and verify both streams per camera.
3. Verify Protect-side listener availability for smart detections; it is a third-party dependency.
4. Run temporary field cutover with automatic rollback, real Protect event checks and restart recovery.
5. Extend the live soak before promoting the unified runtime as the primary deployment.

## Production gate

CI success is necessary but not sufficient. Do not replace the current VM104 runtime until the candidate demonstrates all expected camera identities, correct HQ/LQ metadata, active Protect PullPoints, per-camera event delivery, reconnect/restart recovery and a sustained soak with a verified rollback path.
