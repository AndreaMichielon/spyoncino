# Modular Architecture Blueprint

## Overview

Spyoncino transitions from monolithic scripts into a modular, event-driven system. Every capability is encapsulated within a module that interacts solely via the central event bus, while the orchestrator coordinates lifecycle, configuration, and health.

```
src/spyoncino/
├── core/
│   ├── bus.py
│   ├── config.py
│   ├── contracts.py
│   └── orchestrator.py
└── modules/
    ├── input/
    ├── process/
    ├── event/
    ├── output/
    ├── dashboard/
    ├── storage/
    ├── analytics/
    └── status/
```

**Event Flow (end-to-end)**

1. `camera.{id}.frames` — inputs publish raw `FrameBatch`; processors subscribe as needed.
2. `process.motion` / `process.detected` — processing modules emit motion/detection events for media, notification, and storage consumers.
3. `event.media.ready` — media builders publish artifacts that outputs and storage modules consume.
4. `notify.{channel}` — notification modules fan alerts to external systems.
5. `storage.recorded` / `storage.indexed` — storage modules acknowledge persistence so analytics/dashboards can react.
6. `analytics.*` — analytics publish aggregations or query results consumed by dashboards/orchestrator.
7. `dashboard.{channel}.commands` — dashboards send commands/config changes that the orchestrator/config service handle.
8. `status.report` / `status.heartbeat` — status modules and orchestrator share health signals for analytics/dashboards.
9. `config.snapshot` / `config.update` — config service broadcasts snapshots and accepted updates for modules to refresh.

**Topic Access Matrix**

| Module | Publishes | Subscribes |
|--------|-----------|------------|
| Input (`modules/input/`) | `camera.*` | – |
| Processing (`modules/process/`) | `process.*` | `camera.*` |
| Event (`modules/event/`) | `event.*` | `process.*` |
| Output (`modules/output/`) | `notify.*` | `event.*`, `process.*` |
| Storage (`modules/storage/`) | `storage.*` | `event.*`, `process.*` |
| Analytics (`modules/analytics/`) | `analytics.*` | `storage.*`, `status.*` |
| Dashboard (`modules/dashboard/`) | `dashboard.*` | `dashboard.*`, `config.*`, `analytics.*` |
| Status (`modules/status/`) | `status.*` | `status.*`, `analytics.*` |
| Orchestrator (`core/orchestrator.py`) | `config.snapshot`, `status.heartbeat` | `dashboard.*`, `config.update`, `status.*` |

The orchestrator and modules never talk directly—each module only needs to know which topics it publishes and which ones it listens to. Adding a new capability means choosing the relevant topic(s) and wiring it into the event flow above.

## Core Layer

### Event Bus (`core/bus.py`)
- **Responsibilities:** Topic-based publish/subscribe, request/response, backpressure management, message tracing.
- **Interfaces:**
  - `subscribe(topic: str, handler: Callable, *, filter: Optional[Callable]) -> SubscriptionHandle`
  - `publish(topic: str, payload: BaseModel, *, metadata: Optional[dict])`
  - `request(topic: str, payload: BaseModel, *, timeout: Optional[float]) -> BaseModel`
  - `unsubscribe(handle: SubscriptionHandle)`
- **Implementation Notes:**
  - Default adapter: in-memory `asyncio.Queue` per subscription (bounded, configurable).
  - Hook points for alternative transports (Redis, NATS) via `BusAdapter` protocol.
  - Emit `BusEvent` telemetry (publish, delivery, failure) for logging/metrics.

### Contracts (`core/contracts.py`)
- **Module ABCs:** `BaseModule`, `InputModule`, `ProcessingModule`, `EventModule`, `OutputModule`, `DashboardModule`, `StorageModule`, `AnalyticsModule`, `StatusModule`.
- **Lifecycle Hooks:** `configure(config: ModuleConfig)`, `start(bus: EventBus)`, `stop()`, `health() -> StatusReport`, optional `handle_request`.
- **Payload Schemas (Pydantic):**
  - `FrameBatch`, `DetectionEvent`, `MotionEvent`
  - `MediaArtifact` (`kind: gif|mp4|snapshot`, `path`, `metadata`)
  - `AlertNotification`, `DashboardCommand`, `DashboardResponse`
  - `StorageRecord`, `StorageRequest`
  - `AnalyticsQuery`, `AnalyticsResult`
  - `ConfigUpdate`, `ConfigSnapshot`
  - `StatusReport`, `Heartbeat`
- **Topic Conventions:** `camera.{camera_id}.frames`, `process.detected`, `event.media.ready`, `notify.{channel}`, `dashboard.{channel}.commands`, `config.update`, `config.snapshot`, `status.report`.

### Configuration Service (`core/config.py`)
- **Responsibilities:** Load `config.yaml`, expose typed accessors, persist updates, broadcast `ConfigUpdate` events.
- **Features:**
  - Pydantic models mirroring modular layout (`input`, `process`, `event`, `output`, `dashboard`, `storage`, `analytics`, `status`).
  - Versioning for optimistic concurrency; dashboards submit updates via bus.
  - Change propagation: orchestrator receives updates and calls `module.configure`.

### Orchestrator (`core/orchestrator.py`)
- **Responsibilities:** Module discovery, instantiation, dependency wiring, lifecycle control, health aggregation.
- **Workflow:**
  1. Load config snapshot from `core/config.py`.
  2. Build module registry using entry points or factory map.
  3. For each module definition in config, instantiate, configure, and register subscriptions.
  4. Monitor module health via periodic `health()` calls and `status` topics.
  5. Handle config updates by diffing and reconfiguring affected modules.
  6. Provide graceful shutdown and selective restarts.
- **Extension Points:** Module factory registration, bus adapters, health reporters.

## Module Categories

### Input Modules (`modules/input/`)
- **Purpose:** Acquire frames or media segments from cameras or files.
- **Examples:** `usb_camera.py`, `rtsp_camera.py`, `http_stream.py`, `file_replay.py`.
- **Config Section:** `input:` includes camera list with credentials, resolution, fps, retry policies.
- **Outputs:** `FrameBatch` on `camera.{id}.frames`, optional `StatusReport` for connectivity.
- **Tests:** Mock device streams, ensure reconnect logic, configuration parsing.

### Processing Modules (`modules/process/`)
- **Purpose:** Analyze frames for motion, detection, classification, enrichment.
- **Examples:** `motion_detector.py`, `yolo_detector.py`.
- **Config:** `process:` with detection thresholds, model paths, batching, GPU options.
- **Inputs:** `camera.{id}.frames`; **Outputs:** `DetectionEvent`, `MotionEvent`.
- **Tests:** Detection pipelines with synthetic frames, threshold behavior, error propagation.

### Event Modules (`modules/event/`)
- **Purpose:** Convert detection results into media artifacts and summaries.
- **Examples:** `gif_builder.py`, `mp4_encoder.py`, `snapshot_generator.py`.
- **Config:** `event:` controlling media formats, durations, storage paths, ffmpeg options.
- **Inputs:** `DetectionEvent`; **Outputs:** `MediaArtifact` on `event.media.ready`.
- **Tests:** Ensure GIF/MP4/snapshot generation, format choice, fallback handling.

### Output Modules (`modules/output/`)
- **Purpose:** Deliver alerts and artifacts to external channels.
- **Examples:** `telegram_notifier.py`, `email_notifier.py`, `webhook_notifier.py`.
- **Config:** `output:` per-channel settings, rate limits, recipients.
- **Inputs:** `AlertNotification`, `MediaArtifact`; may emit `DeliveryStatus`.
- **Tests:** Channel mocks, throttling, error retry.

### Dashboard Modules (`modules/dashboard/`)
- **Purpose:** Provide control surfaces (CLI, Telegram bot, web UI).
- **Examples:** `telegram_commands.py`, `terminal_cli.py`, `flask_dashboard.py`.
- **Config:** `dashboard:` channel-specific authentication, commands exposure.
- **Inputs:** `dashboard.{channel}.commands`; **Outputs:** `DashboardResponse`, `ConfigUpdate` requests.
- **Tests:** Command parsing, permission checks, config update flows.

### Storage Modules (`modules/storage/`)
- **Purpose:** Persist media, events, metadata.
- **Examples:** `local_disk.py`, `s3_storage.py`, `db_writer.py`.
- **Config:** `storage:` retention policies, paths, bucket info.
- **Inputs:** `MediaArtifact`, `DetectionEvent`; **Outputs:** `StorageRecord`, retrieval responses.
- **Tests:** Persistence, retention enforcement, failure recovery.

### Analytics Modules (`modules/analytics/`)
- **Purpose:** Provide reports, dashboards, aggregated metrics.
- **Config:** `analytics:` query windows, aggregation intervals.
- **Inputs:** `StorageRecord`, `DetectionEvent`; **Outputs:** `AnalyticsResult`, `AnalyticsSnapshot`.
- **Tests:** Query accuracy, performance, schema migrations.

### Status Modules (`modules/status/`)
- **Purpose:** Optional dedicated status aggregation and heartbeat management.
- **Config:** `status:` heartbeat intervals, alert thresholds.
- **Inputs:** `StatusReport`; **Outputs:** consolidated `SystemStatus`, escalations.
- **Tests:** Aggregation logic, alert triggers.

## Configuration Schema

```yaml
input:
  cameras:
    - id: "front-door"
      type: "rtsp"
      url: "rtsp://..."
      fps: 15
      resolution: "1280x720"
      retry:
        backoff: 2.0
        max_retries: 5
process:
  motion_detector:
    enabled: true
    sensitivity: medium
  yolo_detector:
    model: "weights/yolov8n.pt"
    confidence: 0.4
event:
  formats:
    gif:
      enabled: true
      duration: 3
    mp4:
      enabled: true
      duration: 10
    snapshot:
      enabled: true
output:
  telegram:
    chat_id: 123456
    rate_limit: 30
  email:
    smtp_server: smtp.example.com
    recipients:
      - user@example.com
dashboard:
  telegram:
    token_ref: "secrets.telegram.bot_token"
  flask:
    host: 0.0.0.0
    port: 8080
storage:
  type: local
  path: "recordings/"
analytics:
  refresh_interval: 300
status:
  heartbeat_interval: 30
```

Each module reads only its section; updates propagate through `ConfigUpdate` messages. Dashboards publish updates; config service validates, persists, and broadcasts snapshots.

## Testing Strategy

- **Unit Tests:** Each module directory contains targeted tests verifying configuration parsing, bus interactions, and core logic.
- **Bus Tests:** Validate subscription routing, backpressure handling, error propagation.
- **Orchestrator Tests:** Lifecycle (start/stop), config update handling, health aggregation, module restarts.
- **Integration Tests:** Scenario coverage (single camera, dual camera, multiple outputs), config mutation via dashboards, media generation workflows.

## Documentation Updates

- `README.md`: Update architecture overview, configuration instructions, extending modules, multi-channel setup.
- `docs/architecture/modular_architecture.md` (this file): Living blueprint, to be kept in sync with implementation.
- Module-specific READMEs (optional): Document extension points and usage.

## Implementation Checklist

1. Implement `core/contracts.py` with ABCs and Pydantic payloads.
2. Build `core/bus.py` with `asyncio` adapter and telemetry hooks.
3. Create `core/config.py` with modular schema and update propagation.
4. Implement `core/orchestrator.py` handling lifecycle + health.
5. Extract existing logic into module wrappers (input, process, event, output, dashboard, storage, analytics).
6. Implement media pipeline enhancements for MP4/GIF/snapshot.
7. Add status aggregation (via orchestrator or dedicated module).
8. Write unit/integration tests per module and core components.
9. Update README and docs to reflect new architecture and configuration.
10. Provide migration guide for contributors.

## Extension Guidance

- **Adding a Module:** Implement the appropriate ABC, declare config schema, register factory, subscribe/publish via bus topics.
- **New Media Format:** Extend `MediaArtifact` to include format metadata; add transformer module.
- **Alternative Bus Adapter:** Implement `BusAdapter` protocol, update orchestrator configuration.
- **Custom Dashboard:** Implement `DashboardModule`, define command handlers, integrate config update flow.

---

Maintain this document as the authoritative source on module responsibilities, interfaces, and supporting infrastructure. Update alongside code changes to keep contributors aligned.

