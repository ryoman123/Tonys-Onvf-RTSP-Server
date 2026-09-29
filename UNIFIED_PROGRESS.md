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

## Next

1. Stable configurable Protect-facing identity fields with persistence and UI/API plumbing.
2. Strict remaining Media configuration operations and nested ONVIF fault subcodes.
3. WS-Discovery scopes/identity consistency audit.
4. Frigate MQTT producer feeding the unified event engine.
5. Lorex/Dahua native recorder event producer.
6. Multi-producer state aggregation so one event source cannot clear another source's active state.
7. Runtime readiness/status endpoints suitable for automated acceptance.
8. Port the 29-camera identity manifest, acceptance/soak gate and transactional rollback tooling.
9. Immutable image build/publish workflow.
10. Live Protect field validation before any VM104 cutover.

## Production gate

CI success is necessary but not sufficient. Do not replace the current VM104 runtime until the candidate demonstrates all expected camera identities, correct HQ/LQ metadata, active Protect PullPoints, per-camera event delivery, reconnect/restart recovery and a sustained soak with a verified rollback path.
