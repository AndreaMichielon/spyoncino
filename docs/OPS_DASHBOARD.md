## Ops Dashboard Notes

### Metrics
- `spyoncino_bus_queue_*` gauges (depth, capacity, lag, totals) exposed by `modules.status.prometheus_exporter` on `http://127.0.0.1:9093/metrics`.
- Extend dashboards with rate-limit counters by scraping `event.snapshot.allowed` vs `event.snapshot.ready` via Prometheus recording rules.

### Health Stream
- Orchestrator publishes `status.health.summary` every 10s with a `HealthSummary` payload (`status`, per-module `HealthStatus` map).
- Consumers (CLI/dashboard) should subscribe to that topic to render overall and per-module states; treat `error` as red, `degraded` as yellow.

### Event Hygiene
- `modules.event.deduplicator` suppresses repeated detections within a configurable window to reduce alert fatigue; defaults come from `config/dedupe`.
- `modules.output.rate_limiter` enforces per-camera throughput on snapshots before notifications (`rate_limit` section in config).

### Dual-Camera Expectations
- Integration tests cover two simultaneous cameras, ensuring dedupe + rate limiting still deliver snapshots for each camera.
- Ops dashboards should chart per-camera activity to validate both feeds remain healthy.

### Remote Storage Sync
- `modules.storage.s3_uploader` publishes `storage.s3.synced` with `StorageSyncResult` metadata (bucket, key, duration, status). Scrape delivered/failed counts to ensure remote replicas keep pace with local retention.
- `modules.storage.retention` subscribes to sync events and emits `storage.discrepancy` whenever local vs. remote artifacts drift. Alert on non-empty `missing_remote` or `orphaned_remote`.

### Realtime Gateway
- `modules.dashboard.websocket_gateway` exposes `/ws` and `/events` feeds for dashboards. The default topic set mirrors health, bus telemetry, notifications, and analytics cursors; extend via config to include custom topics.
- Web clients should listen for `{"type": "keepalive"}` frames to detect idle timeouts and reconnect proactively.

### Chaos & Drills
- `modules.status.resilience_tester` injects latency/drops for configured topics using bus interceptors. Toggle scenarios via `ControlCommand` (`resilience.toggle`) to run ad-hoc chaos experiments.
- The orchestrator automatically runs weekly rollback drills: staged module restarts are reported on `config.rollback`, and shutdown phases stream via `status.shutdown.progress`. Surface both topics in dashboards to monitor recovery KPIs.
