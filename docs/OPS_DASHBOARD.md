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
