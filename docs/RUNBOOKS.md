# Operations Runbooks

## Startup Checklist
- Copy `config/config.yaml` and `config/secrets.yaml.example` into `/etc/spyoncino` (or mount the repo’s `config/` directory in Docker). Generate TLS cert/key pairs under `config/certs/` for the control API and websocket gateway.
- For Docker, run `docker compose -f docker/compose.modular.yaml up -d` after exporting `SPYONCINO_PRESET` and `SPYONCINO_EXTRA_ARGS` as needed. For bare metal, install the project into `/opt/spyoncino`, copy `docker/systemd/spyoncino-modular.service`, and `systemctl enable --now spyoncino-modular`.
- Verify surfaces: `curl -k https://localhost:8443/health`, `curl -k https://localhost:9443/health`, `curl http://localhost:9093/metrics`. If any call fails, inspect the container logs (`docker compose logs orchestrator`) or `journalctl -u spyoncino-modular`.
- Confirm S3/minio credentials by watching `storage.s3.synced` and `storage.discrepancy` events (`tests/tools/subscribe.py status.*`). Create a test detection and ensure Telegram/webhook resumes.
- Record build metadata (`git rev-parse HEAD`, `spyoncino-modular --version`) in the ops log before handing off.

## Blue/Green & Rollback
- Always deploy a standby node (`docker compose -f docker/compose.modular.yaml -f docker/compose.dev-ha.yaml --profile ha up -d orchestrator_canary`). The canary uses the same config bundle but ships with resilience tester enabled.
- Exercise readiness by publishing `config.update` and `dashboard.control.command` samples; watch `status.health.summary` for regressions.
- Flip traffic by updating DNS/reverse proxy to the new node (`caddy` in the HA compose file exposes 8443/9443). Keep the previous node hot for at least 30 minutes.
- Roll back via `docker compose stop orchestrator` (or `systemctl stop spyoncino-modular`) and re-point DNS. Config snapshots are emitted on every change; replay the latest `config.rollback` fingerprint to ensure parity.

## Queue Saturation Remediation
- Symptoms: `status.bus` depth > 80%, rising `analytics.persistence.cursor.lag_seconds`, delayed Telegram messages.
- Immediate steps: publish `dashboard.control.command` with `{"command": "pipeline.pause", "module": "modules.output.telegram_notifier"}` to shed notifications. Raise rate-limiter thresholds in `config/config.yaml` and republish via `config.update`.
- Trigger the resilience tester scenario named `slow-webhook` (see config) to reproduce under controlled conditions. Use the HA compose profile with `SPYONCINO_EXTRA_ARGS="--module modules.status.resilience_tester"` to keep scenarios on standby.

## Websocket Dashboard Troubleshooting
- `wss://host:9443/ws` should respond with keepalives every 30 seconds. If clients stall, inspect `modules.dashboard.websocket_gateway` health (`status.health.summary.details.clients`).
- TLS failures: ensure `/certs/websocket.crt` + `.key` exist inside the container/systemd host. Replace certificates by copying new files into `config/certs` and reloading (`docker compose restart caddy orchestrator`).
- If HA profile is active, confirm `caddy` is proxying to the expected orchestrator container. Use `docker exec caddy caddy reload --config /etc/caddy/Caddyfile` after edits.

## S3 Sync Monitoring
- MinIO (dev) exposes a console on `http://localhost:9001`. Create the bucket referenced in `config/config.yaml` (`storage.remote.bucket_name`).
- Watch `storage.s3.synced` for successes and `storage.discrepancy` for drift. If lag grows, check network reachability and the IAM credentials loaded from `config/secrets.yaml`.
- During incidents, toggle the storage module via `dashboard.control.command` (`{"command":"module.disable","module":"modules.storage.s3_uploader"}`), drain the backlog locally, then re-enable once credentials/network recover.
