## Current Architecture Snapshot (Week 5)

### Highlights
- Advanced-processing scope landed: zoning filter, MP4 clip builder, FastAPI control API, and orchestrator-driven config hot reload now run on the async bus.
- Contracts + config schemas expanded with `MediaArtifact`, `ControlCommand`, `ConfigUpdate`, `ConfigSnapshotPayload`, `ZoneDefinition`, `ClipSettings`, and `ControlApiSettings`.
- Bus/topic surface now includes `process.motion.zoned`, `event.clip.ready`, `dashboard.control.command`, `config.update`, and `config.snapshot`, keeping UI ↔ core interactions asynchronous.

### New Code
- `core.contracts`: new payloads for media artifacts, dashboard commands, and config events.
- `core.config`: zoning/clip/control sections, `ZoneDefinition` validator, `apply_changes()` helper, and module builder coverage for new components.
- `core.orchestrator`: subscribes to `config.update`, reapplies module configs, publishes `config.snapshot`.
- `modules.process.zoning_filter`: annotates detections with zone metadata and optional exclusion logic.
- `modules.event.clip_builder`: buffers frames and emits MP4 `MediaArtifact` payloads.
- `modules.dashboard.control_api`: FastAPI server (or in-process app) exposing camera state + zoning endpoints that publish `ControlCommand`/`ConfigUpdate`.
- `modules.process.yolo_detector`: now stamps per-frame width/height in detection attributes for zoning math.
- Package exports updated (`modules.__init__`, process/event/dashboard init files).

### Tests Added / Updated
- `tests/unit/test_zoning_filter.py`, `test_clip_builder.py`, `test_control_api.py`, `test_config_hot_reload.py`.
- `tests/unit/test_config.py`: asserts new module builders and `apply_changes()` wiring.
- Existing pipelines continue to pass; targeted suites run via `spyoncino_env\Scripts\python -m pytest …`.

### Operations & Notes
- Config hot reload: publish `ConfigUpdate` on `config.update` to refresh modules without restarts; successful merges emit `config.snapshot`.
- Detection pipeline now flows through dedupe → zoning → snapshot/clip writers → rate limiter → Telegram.
- Control API currently runs in “embedded” mode (serve_api=false) during tests; enable FastAPI/Uvicorn when exposing externally.

### Next Up
- Week 6 scope (packaging + load): docker/compose envs, load tests, multi-channel notifiers, doc refresh per `TODO_ARCHITECTURE.md`.
