## Current Architecture Snapshot (Week 7)

### Highlights
- Persistence & resilience scope is live: S3 artifact replication, analytics database cursors, WebSocket dashboards, chaos/resilience hooks, and orchestrator rollback drills.
- Contracts/config expanded with storage sync payloads, analytics cursors, shutdown progress, resilience events, and builder coverage for S3, websocket, and chaos modules.
- Bus/topic surface now includes `storage.s3.synced`, `storage.discrepancy`, `analytics.persistence.cursor`, `status.shutdown.progress`, `status.resilience.event`, and `dashboard.events`.
- Multi-camera pipelines are now first-class: `config.yaml` accepts an array of `cameras[]`, ConfigService emits per-camera module configs, and the orchestrator instantiates USB/RTSP/simulator inputs plus downstream motion/YOLO consumers for every defined feed.

### New Code
- `core.contracts`: adds `StorageSyncResult`, `StorageDiscrepancy`, `AnalyticsCursor`, `ConfigRollbackPayload`, `ShutdownProgress`, and `ResilienceEvent`.
- `core.bus`: interceptor pipeline so resilience tooling can inject latency/drops without modifying publishers.
- `core.config`: new sections for `S3SyncSettings`, `AnalyticsSettings.database_url/cursor_topic`, `WebsocketGatewaySettings`, and `ResilienceSettings`.
- `core.orchestrator`: staged shutdown telemetry plus scheduled rollback drills emitting snapshot fingerprints.
- `modules.storage.s3_uploader`: async boto3 uploader with lifecycle tags + sync telemetry; retention module now reconciles remote vs. local artifacts.
- `modules.analytics.db_logger`: SQLModel-backed persistence with cursor updates for dashboards.
- `modules.dashboard.websocket_gateway`: FastAPI app exposing `/events` + `/ws` streaming of status/analytics topics.
- `modules.status.resilience_tester`: bus interceptor-driven chaos scenarios toggled via `ControlCommand`.
- `modules.__init__` and package exports updated to include the new modules.

### Tests Added / Updated
- `tests/unit/test_s3_uploader.py`, `test_analytics_db_logger.py`, `test_websocket_gateway.py`, `test_resilience_tester.py`.
- `tests/unit/test_config.py`: asserts S3/websocket/resilience/db-logger builders.
- `tests/unit/test_storage_retention.py`: extended to cover remote reconciliation hooks.
- Suites run via `spyoncino_env\Scripts\python -m pytest tests/unit/...` with new dependencies (`sqlmodel`, `boto3`) declared in `pyproject.toml`.

### Operations & Notes
- Remote storage: monitor `storage.s3.synced` for success metrics and `storage.discrepancy` for drift; retention publishes augmented stats.
- Dashboards: Websocket gateway buffers latest `status.*`/`notify.*` events for `/events` polling and `/ws` streaming; keepalive frames help clients detect idle timeouts.
- Resilience: `resilience.toggle` commands (over `dashboard.control.command`) enable/disable chaos scenarios; telemetry lands on `status.resilience.event`.
- Orchestrator emits `status.shutdown.progress` during staged shutdowns and `config.rollback` records after drills—surface both in Ops dashboards.

### Next Up
- Week 8 production hardening: systemd/docker packaging, HA + chaos validation under load, TLS/secrets review, and publishing runbooks + migration docs (see `TODO_ARCHITECTURE.md` Week 8 plan).
