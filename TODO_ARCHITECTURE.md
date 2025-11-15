# Modular Architecture Blueprint

## Executive Summary

Spyoncino evolves into a modular, event-driven surveillance platform. The system favors incremental delivery, strong contracts, and operational visibility so new capabilities can be added with minimal coupling. Phase 1 delivers a fully working baseline; later phases layer advanced processing, scalability, and distributed options without rewriting the core.

## Architecture Principles

- **Do:** start with an in-memory `asyncio` bus, keep modules single-responsibility, type everything with Pydantic, prioritize observability (structlog, Prometheus), validate configuration before use, and design for graceful degradation.
- **Avoid:** over-engineering transport early (request/response, distributed tracing), blocking or synchronous code paths, bespoke infrastructure where solid libraries exist, and releasing modules without health checks or documentation.

## System Structure

```
src/spyoncino/
├── core/
│   ├── bus.py          # Async pub/sub (baseline) + telemetry hooks
│   ├── contracts.py    # Module ABCs, payload schemas, validation helpers
│   ├── config.py       # Dynaconf-backed config, sanitization, rollback
│   └── orchestrator.py # Lifecycle, wiring, health, reconfiguration
├── modules/
│   ├── input/          # Cameras (USB, RTSP, file replay)
│   ├── process/        # Detectors (motion, YOLO, zoning)
│   ├── event/          # Media builders (snapshot, GIF, clip)
│   ├── output/         # Notifiers (Telegram, email, webhook)
│   ├── storage/        # Persistence (local, S3, DB)
│   ├── analytics/      # Aggregations, dashboards
│   └── dashboard/      # CLI, REST, WebSocket control surfaces
├── schemas/            # Shared Pydantic models and JSON Schema exports
├── tests/              # Unit, contract, integration, load suites
├── config/             # Environment-specific YAML + secrets templates
└── docker/             # Containerization and deployment assets
```

## Core Layer

### Event Bus (`core/bus.py`)
- **Baseline:** async publish/subscribe with bounded queues, simple topic filters, structured telemetry, and periodic `status.bus` snapshots (queue depth, lag, subscriber counts).
- **Future Enhancements:** optional request/response with timeouts, correlation IDs, and alternate transports (`BusAdapter` protocol for Redis/NATS).
- **Status & Backpressure:** high-watermark warnings publish `BusStatus` events; mitigation strategies (drop oldest, pause publisher) are configurable.

### Contracts (`core/contracts.py`)
- **Module Interfaces:** `BaseModule` and category-specific ABCs defining `configure`, `start`, `stop`, `health`, and optional `handle_request`.
- **Payload Schemas:** canonical models (`FrameBatch`, `DetectionEvent`, `MediaArtifact`, `AlertNotification`, `ConfigUpdate`, `StatusReport`, etc.) with runtime validation and JSON Schema generation.
- **Schema Evolution:** every payload carries `schema_version`; additive changes use tolerant parsing (`extra="allow"`). Breaking changes require new topics (e.g. `process.detected.v2`) with orchestrator-managed adapters and deprecation warnings on `status.contract`.
- **Contract Tests:** reusable fixtures ensure third-party modules satisfy ABCs, schemas, and topic expectations before integration.

### Configuration Service (`core/config.py`)
- **Dynaconf Backbone:** layered config (`config/default.yaml`, environment overrides, environment variables, `.env` for local dev).
- **Sanitization & Validation:** pre-apply normalization, type coercion, and policy checks; rejected updates emit `ConfigRejected` and log to `status.contract`.
- **Transactional Rollback:** on partial failure, restore the last snapshot, emit `config.rollback` with diagnostics, and notify orchestrator.
- **Hot Reload:** accepted updates broadcast `config.snapshot` for subscribed modules to refresh in place, and `apply_changes()` merges `config.update` payloads without writing to disk.

### Orchestrator (`core/orchestrator.py`)
- **Lifecycle:** discover modules, load configuration, instantiate, register subscriptions, and coordinate start/stop with graceful shutdown.
- **Reconfiguration:** compute per-module diffs; `configure` is idempotent. Failures trigger rollback, `status.contract` alerts, and optional module quarantine. The orchestrator now subscribes to `config.update`, reapplies module configs, and republishes `config.snapshot` after a successful merge.
- **Health Aggregation:** poll `health()` hooks, subscribe to `status.*`, and expose unified health summaries for dashboards and readiness probes.
- **Extensibility:** supports custom module factories, alternate bus adapters, and health reporters without cross-module coupling.

## Event Flow & Topic Conventions

- **Naming:** `<domain>.<entity>[.<action>]` (e.g., `camera.front_door.frame`, `process.motion.detected`, `event.clip.ready`, `notify.telegram.sent`, `status.bus`, `config.snapshot`).
- **Standard Detection Flow:** `camera.{id}.frame → process.motion.detected → process.motion.unique → process.motion.zoned → event.snapshot.ready / event.clip.ready → notify.telegram.queued → notify.telegram.sent`, with optional branching into storage and analytics.
- **Health Flow:** `module.*.health → status.report → analytics.health.summary → dashboard.commands` for operator response.
- **Rules:** modules communicate only via the bus (including `config.update`, `config.snapshot`, and `dashboard.control.command`), assume async fire-and-forget semantics, handle missing subscribers gracefully, and design idempotent handlers for replay tolerance.

## Module Categories

- **Input (`modules/input/`):** acquire frames or streams; publish `camera.{id}.frame`; manage reconnect, FPS, and device health.
- **Processing (`modules/process/`):** analyze frames (`process.motion`, `process.detected`); support GPU batching, YOLO inference, and zoning filters (`process.motion.zoned`).
- **Event (`modules/event/`):** build artifacts (`event.media.ready`, `event.clip.ready`); optimize encoding, manage retention metadata.
- **Output (`modules/output/`):** deliver alerts (`notify.{channel}`); enforce rate limiting, retries, and delivery confirmations.
- **Storage (`modules/storage/`):** persist artifacts/events; publish `storage.*` acknowledgments and index updates.
- **Analytics (`modules/analytics/`):** aggregate detections/storage into `analytics.*` snapshots and respond to queries.
- **Dashboard (`modules/dashboard/`):** expose CLIs/APIs (FastAPI control API, bot); publish `dashboard.*.commands`, submit `config.update` requests, and consume status feeds.
- **Status (`modules/status/`):** optional aggregation of heartbeats and escalations for observability pipelines.

## Configuration Strategy

- **Hierarchy:** default YAML → environment-specific YAML → environment variables → secrets store (`.env`, Docker secrets). Validate presence of required secrets at startup.
- **Schemas:** each module ships a Pydantic config model used by `configure` to apply defaults, normalize units, and enforce limits.
- **Update Workflow:** dashboards or APIs publish `config.update`; config service validates, persists (with versioning), broadcasts `config.snapshot`, or emits `ConfigRejected` on failure.

## Implementation Roadmap (8 Weeks)

1. **Phase 1 – Core Foundation (Weeks 1-2):** baseline bus, module lifecycle, Dynaconf config, structlog logging, USB input, motion detector, local snapshots, Telegram notifier. *Success:* motion detected and alert sent.
2. **Phase 2 – Essential Modules (Weeks 3-4):** RTSP input, YOLO detection, GIF media, event deduplication, rate limiting, health monitoring, Prometheus metrics. *Success:* multi-camera object detection with metrics.
3. **Phase 3 – Enhanced Processing (Weeks 5-6):** zoning, video clip generation, multiple notification channels, FastAPI control API, configuration hot reload, Docker packaging. *Success:* zone-aware detection controllable via API.
4. **Phase 4 – Production Hardening (Weeks 7-8):** WebSocket updates, S3 storage, database event logging, graceful shutdown, systemd integration, full documentation. *Success:* production-ready deployment with monitoring.
5. **Phase 5 – Post-MVP Enhancements:** (as needed) Redis/NATS bus adapter, horizontal scaling, ML hot swapping, analytics extensions, multi-tenancy.

## Execution Plan

### Workstreams per Phase

| Phase | Core Platform | Modules & Features | Operations & QA |
|-------|---------------|--------------------|-----------------|
| 1 | Implement bus MVP, contracts, orchestrator skeleton | USB camera, motion detector, snapshot writer, Telegram notifier | Dynaconf config baseline, structlog setup, unit tests |
| 2 | Add bus telemetry, health aggregation hooks | RTSP input, YOLO detector, GIF media, dedupe, rate limiting | Prometheus metrics, health endpoints, integration smoke tests |
| 3 | Config hot-reload, schema version helpers | Zoning logic, clip generator, multi-channel notifier, FastAPI API | Docker build, contract test suite, load-test harness |
| 4 | Graceful shutdown, rollback automation, status aggregation | S3 storage, DB logging, WebSocket dashboard | systemd packaging, runbooks, HA validation, documentation |

### Iteration Breakdown (Weekly)

| Week | Focus | Key Deliverables | Reminders & References |
|------|-------|------------------|------------------------|
| ✅ 1 | Core scaffolding | Contracts & schemas, bus/orchestrator skeleton, baseline unit tests, sample frame→motion module | Delivered Nov 14: `core.bus`, `core.contracts`, `core.orchestrator`, `modules.input.camera_sim`, `modules.process.motion_detector`, unit tests |
| ✅ 2 | Baseline features | Dynaconf wiring, snapshot persistence, Telegram notifier, CI for lint/type/unit | Checklist 3-4, 8; Governance quality gate |
| ✅ 3 | Telemetry expansion | `status.bus` telemetry, RTSP input, YOLO pipeline, GIF builder, Prometheus exporters draft | Checklist 2, 5, 6; Appendix A status reporting |
| ✅ 4 | Reliability hardening | Event dedupe, rate limiting, health aggregation, dual-camera integration tests, ops dashboard docs | Checklist 4, 7, 8; Governance demo |
| ✅ 5 | Advanced processing | Zoning filter, MP4 clip builder, FastAPI control API, config hot reload, contract fixtures | Checklist 3, 5, 8; Appendix A backpressure |
| 6 | Packaging & load | Docker + compose env, load tests, multi-channel notifier support, documentation refresh | Checklist 5-8; Implementation Status “Media pipeline”, “Test suites” |
| 7 | Persistence & resilience | S3 storage, database logging, WebSocket updates, graceful shutdown + rollback drills | Checklist 6-7, 9; Appendix B migration |
| 8 | Production launch | systemd unit, production hardening checklist, HA validation, runbooks, exec sign-off | Checklist 7-9; Governance change management |

#### Week 5 Delivery Notes

- **Config Hot Reload:** `core.config.ConfigService` now exposes `apply_changes()` while `core.orchestrator.Orchestrator` listens on `config.update` and republishes `config.snapshot`, so modules reconfigure in-place without restarts.
- **Zoning Pipeline:** Introduced `modules.process.zoning_filter` plus new `ZoneDefinition` / `ZoningSettings` schemas; detections now annotate `attributes.zone_matches` on `process.motion.zoned`.
- **Media Clips:** Added `MediaArtifact` payloads and `modules.event.clip_builder` to publish MP4 clips (`event.clip.ready`) following Appendix A backpressure limits.
- **Control Surface:** Added FastAPI-based `modules.dashboard.control_api` that emits `ControlCommand` and `ConfigUpdate` events for camera toggles and zoning updates.
- **Contract/Test Fixtures:** `core.contracts` gained `ControlCommand`, `ConfigUpdate`, and `ConfigSnapshotPayload`; new unit suites cover zoning, clips, control API, and orchestrator hot reload flows.

### Governance & Checkpoints

- **Design Reviews:** lightweight review at start of each phase to validate scope and dependencies.
- **Quality Gates:** no phase closure without passing unit/contract/integration suites and updated documentation.
- **Change Management:** config updates use feature flags until validated in staging; rollback tested monthly.
- **Stakeholder Demos:** end-of-phase demo to showcase new capabilities and collect feedback for next iteration.

## Testing Strategy

- **Unit Tests (`tests/unit/`):** isolate module logic, mock external dependencies, ensure fast execution.
- **Contract Tests (`tests/contracts/`):** validate module ABC compliance, payload schemas, topic usage, and configuration parsing for first-party and partner modules.
- **Integration Tests (`tests/integration/`):** exercise end-to-end flows with real bus and media pipelines, covering config mutations and failure modes.
- **Load Tests (`tests/load/`):** stress inputs, detect bottlenecks, measure memory/CPU, and validate backpressure.
- **Coverage & Quality:** target ≥80% coverage, include happy/error paths, enforce linting, type checking (`mypy`), and pre-commit hooks.

## Observability & Operations

- **Metrics:** Prometheus exports for bus throughput, queue depths, per-module latency, detection accuracy, resource usage, and camera availability.
- **Logging:** structlog JSON in production, correlation IDs propagated via bus metadata, standardized levels (DEBUG/INFO/WARN/ERROR), automatic exception capture.
- **Health Checks:** `/health/live`, `/health/ready`, module-specific endpoints, and `status.bus` / `status.report` topics feeding dashboards.
- **Security & Deployment:** run as non-root, enforce TLS for external channels, rate-limit APIs, manage secrets securely, package via Docker/Compose with systemd service definitions.

## Tooling & Dependencies

- **Runtime:** Python 3.11+, asyncio, Pydantic v2, Dynaconf, structlog, Prometheus client, FastAPI, Uvicorn.
- **Computer Vision:** OpenCV, ultralytics, Pillow, ffmpeg-python.
- **Integration:** aiohttp, aiogram, aiosmtplib, aioboto3, aiofiles.
- **Development:** pytest (+asyncio, coverage), mypy, black, pre-commit, Docker 24+.

## Implementation Checklist

1. ✅ Implement `core/contracts.py` with ABCs, payload schemas, version helpers.
2. ✅ Deliver `core/bus.py` baseline with telemetry and `status.bus`.
3. ✅ Build `core/config.py` (Dynaconf, validation, rollback, snapshot broadcasting).
4. ✅ Implement `core/orchestrator.py` lifecycle, health aggregation, rollback hooks.
5. ✅ Extract existing logic into module directories aligned with contracts (legacy shims + new `modules/` skeleton).
6. ✅ Ship baseline media pipeline (snapshot/GIF/clip) and storage alignment.
7. ✅ Provide status aggregation (orchestrator or dedicated module).
8. ✅ Stand up testing layers (unit focus) and CI automation foundations (`tests/unit/test_bus.py`, `test_first_pipeline.py`).
9. ⏳ Update README + docs, publish migration guide, and seed dashboard UX.

## Implementation Status Matrix

| Item | Status | Owner | Notes |
|------|--------|-------|-------|
| Contracts ABCs & schemas | ✅ Complete | – | `core/contracts.py` shipped with BaseModule, Frame, DetectionEvent. |
| Event bus adapter + telemetry | ✅ Complete (baseline) | – | Async queue bus in `core/bus.py`; telemetry hooks stubbed for future expansion. |
| `status.bus` telemetry & Prometheus exporter | ✅ Complete | – | Bus now emits `BusStatus`; Prometheus exporter module publishes gauges. |
| Config service with rollback | ✅ Complete | – | Dynaconf snapshot builder ships `apply_changes()` and hot-reload schemas (`ConfigSnapshotPayload`). |
| Orchestrator lifecycle | ✅ Complete | – | `core/orchestrator.py` manages bus + module lifecycle basics. |
| Module extractions | ⏳ In progress | – | Legacy code moved under `spyoncino.legacy`, initial modular input/process modules live under `modules/`. |
| Media pipeline enhancements | ✅ Complete | – | Snapshot writer, GIF builder, and MP4 clip builder cover artifact surface area. |
| Event dedupe module | ✅ Complete | – | `modules.event.deduplicator` filters duplicate detections. |
| Snapshot rate limiter | ✅ Complete | – | `modules.output.rate_limiter` enforces per-camera throughput. |
| Health aggregation loop | ✅ Complete | – | Orchestrator publishes `status.health.summary`. |
| Dual-camera integration tests | ✅ Complete | – | `tests/unit/test_dual_camera_pipeline.py`. |
| Ops dashboard docs | ✅ Complete | – | `docs/OPS_DASHBOARD.md` outlines metrics & flows. |
| RTSP input module | ✅ Complete | – | `modules.input.rtsp_camera` ingests network streams with retries. |
| YOLO detector module | ✅ Complete | – | `modules.process.yolo_detector` wires Ultralytics/stub predictors. |
| GIF builder module | ✅ Complete | – | `modules.event.gif_builder` buffers frames and emits GIF artifacts. |
| Prometheus exporter module | ✅ Complete (draft) | – | `modules.status.prometheus_exporter` exposes bus telemetry via HTTP. |
| Status aggregation module | ✅ Complete | – | Orchestrator publishes `status.health.summary`; dedicated module deferred. |
| Zoning filter module | ✅ Complete | – | `modules.process.zoning_filter` annotates detections and enforces include/exclude zones. |
| Clip builder module | ✅ Complete | – | `modules.event.clip_builder` emits MP4 `MediaArtifact` payloads on `event.clip.ready`. |
| Control API module | ✅ Complete | – | FastAPI `modules.dashboard.control_api` publishes `ControlCommand` / `ConfigUpdate`. |
| Test suites (unit/contract/integration/load) | ✅ Unit baseline | – | Bus + first-pipeline pytest coverage automated. |
| Observability stack | Planned | – | structlog/Prometheus work scheduled for Phase 2. |
| Documentation & migration guide | ⏳ In progress | – | Architecture doc now tracks completed items; README refresh pending. |

## Appendix A: Event Bus Guidance

- **Baseline Behavior:** fire-and-forget publish/subscribe with best-effort delivery and bounded queues; modules treat metadata IDs as optional.
- **Status Reporting:** periodic `status.bus` events include queue depth, in-flight counts, and latency samples; on timeout or overflow the bus emits `BusStatus` alerts.
- **Backpressure:** queue watermarks trigger mitigation actions and warnings; publishers may shed load or slow capture based on policy.
- **Extending Transports:** alternate adapters implement the `BusAdapter` protocol, expose identical telemetry, and honor cancellation signals before being approved for production.

## Appendix B: Migration Strategy

- **Parallel Run:** operate new modular stack alongside legacy scripts until metrics match.
- **Gradual Cutover:** migrate cameras and notification channels incrementally with rollback ready.
- **Compatibility Layer:** support legacy configuration ingestion, maintain existing webhook and Telegram semantics, and preserve file naming conventions.
