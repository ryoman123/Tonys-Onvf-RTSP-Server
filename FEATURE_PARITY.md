# Tony feature parity and release acceptance

The audited upstream is [`BigTonyTones/Tonys-Onvf-RTSP-Server` at
`f15c6047a0eb9b3fc3361b2e65c005315c05a868`](https://github.com/BigTonyTones/Tonys-Onvf-RTSP-Server/commit/f15c6047a0eb9b3fc3361b2e65c005315c05a868),
version 9.2.5. It is an ancestor of this fork. The audit compared every upstream
application module, Python entry point, web route and named JavaScript function.
All 93 upstream web routes and 335 JavaScript functions are retained. The old
`VirtualSubscription` implementation and private `_requested_sub_profile` helper
were replaced by the event engine and Media helpers, covered by authenticated
SOAP regressions. Symbol retention proves source coverage, not UI usability,
protocol certification or live deployment success.

| Upstream capability | Retained implementation | Improvement / evidence | Remaining field observation |
|---|---|---|---|
| RTSP relay, HQ/LQ, audio and explicit transcoding | MediaMTX, camera and Media service | Native H.265 recorder paths; explicit transcode advertises H.264 | Protect live view, playback, codec and audio |
| Browser grid, matrix profile switching, HLS and WebRTC | Web template | Dedicated on-demand H.264 previews; idle encoders stop | Both playback modes and hover audio |
| GridFusion, layouts and looks | Original template and manager | Existing routes and functions preserved | Saved composition, stream and restart persistence |
| Virtual NIC, DHCP/static IP, keepalive and discovery | Network/lifecycle/identity modules | Collision-free NIC names; persistent adoption identity/tokens | All 29 adopted records recover |
| Physical ONVIF motion forwarding | Camera forwarder | Bounded shutdown and multi-producer property state | Real physical motion start/clear |
| Local YOLO, device selection, shared models and motion gate | Camera/device/CoreML modules | Authenticated relay, correct HQ fallback, warm-up and frame freshness | Accuracy, zones and 29-camera CPU/queue capacity |
| Smart person/vehicle/animal/package topics | Camera trigger and event service | Per-subscriber delivery and no cross-camera leakage | Correct real Protect timeline events |
| Plate detector and OCR | Camera LPR path/shared OCR | Available pinned plate weights; bundled EasyOCR and models | Actual plates on the intended views |
| Annotated history, cap, filter and deletion | Original alert store/template | Image test checks detector-generated history | History UI and cap/deletion |
| Notifications, snapshots, schedules and providers | Original notifier and controls | Image test checks annotated detection payload | Configured delivery and schedules |
| Protect listener install/status/repair and multi-NVR monitoring | Original listener/UI/API | Strict smart gate rejects missing/stale checks | Actual recorder and update recovery |
| Auto boot, settings, diagnostics, updates, backups and watchdog | Original modules/routes | Private data excluded from image context; staged rollback | Restart and saved preferences |
| Additional Frigate/Lorex/Dahua producers | Optional new adapters | 29-camera authenticated routing/start/clear tests | Actual broker/native detections |

## Image and code validation

`tests/test_tony_feature_parity.py` checks recorder/browser H.265 separation,
authenticated AI HQ fallback, transcode metadata, camera-edit identity/credential/
path preservation, local smart-state restart and strict pipeline readiness. The
existing identity, lifecycle, Media, event and adapter regressions remain required.

`tools/image_feature_smoke.py` runs in the production image with networking
disabled. It executes real YOLO on Ultralytics' bundled bus image, plate-model
inference, English OCR, authenticated loopback H.265 RTSP, the real local detector,
authenticated ONVIF start/clear, annotated history/notification payloads and a
separate H.264 browser preview, actual authenticated HLS playback and preview
encoder shutdown after viewers disconnect. It is not a Protect test and does not establish
real-world object accuracy. Publishing follows this exercise and startup smoke.

Plate weights are from [`joker5914/yolov8n-license-plate`](https://huggingface.co/joker5914/yolov8n-license-plate),
revision `8286762929bd4b111a19186f2a05e0a5940b6088`, file `best.pt` (model card:
AGPL-3.0). English EasyOCR and default YOLO weights are bundled so initial use
does not depend on camera-VLAN internet access. Application, dependency and
model licenses apply to their respective components.

## What counts as ready

Video, image-feature and smart-runtime gates are necessary checks. They cannot
independently establish full deployment acceptance. That requires
[FIELD_DEPLOYMENT.md](FIELD_DEPLOYMENT.md)'s real timeline, clearing, thumbnail/
notification, UI, persistence and restart observations. Frigate is optional;
built-in YOLO is the default local route. Disabled producers and unconfigured
recorders are incomplete setup, not passing AI tests.
