## Current Architecture Snapshot (Week 4)

### Highlights
- Week 4 “Reliability hardening” features are in: detection dedupe, snapshot rate limiting, automated health summaries, dual-camera integration coverage, and ops dashboard notes.
- Week rows 1‑3 marked ✅; Week 4 can now close after review.

### New Code
- `core.contracts`: adds `HealthSummary` payload for aggregated status reporting.
- `core.orchestrator`: emits `status.health.summary` at a configurable interval.
- `modules.event.deduplicator`: suppresses duplicate detections via configurable keys/windows.
- `modules.output.rate_limiter`: throttles snapshot events before notifiers.
- `modules.__init__` + subpackages export the new modules.
- `core.config` / `config/config.yaml`: new `dedupe` and `rate_limit` sections, module builder refactor, defaults wiring snapshot writer/telegram into the new stages.
- `docs/OPS_DASHBOARD.md`: describes Prometheus metrics, health summaries, and operational expectations.

### Tests Added / Updated
- `tests/unit/test_deduplicator.py`, `test_rate_limiter.py`, `test_orchestrator_health.py`.
- `tests/unit/test_dual_camera_pipeline.py`: ensures two simultaneous cameras succeed through dedupe + rate limiting.
- `tests/unit/test_first_pipeline.py`: pipeline now exercises dedupe + rate limiter by default.

### Operations & Notes
- Prometheus exporter now binds to localhost by default to satisfy Bandit.
- Snapshot flow: `process.motion.detected → modules.event.deduplicator → process.motion.unique → SnapshotWriter → event.snapshot.ready → RateLimiter → event.snapshot.allowed → Telegram`.
- Ops dashboard guidance lives in `docs/OPS_DASHBOARD.md`; subscribe to `status.health.summary` for rollups.

### Next Up
- Phase 5+ deliverables: zoning, clip builder, FastAPI control plane, persistence, etc., per `TODO_ARCHITECTURE.md`.
