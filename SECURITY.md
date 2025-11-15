# Security Policy

Thank you for helping keep `spyoncino` safe. We follow a responsible disclosure process so that issues can be fixed before they become public.

## Reporting a Vulnerability

1. Submit a private report through GitHub’s security advisory form: <https://github.com/AndreaMichielon/spyoncino/security/advisories/new>.
2. If that isn’t possible, email the maintainer using the public contact address listed on the [GitHub profile](https://github.com/AndreaMichielon) (or replace this line with a dedicated security inbox you monitor).
3. Include a clear description, reproduction steps, potential impact, and any suggested fixes.

Please avoid filing public issues or sharing exploit details until a fix is released.

## What to Expect

- Acknowledgement within **3 business days**.
- Status updates at least once every **7 days** while we work on a fix.
- Coordinated disclosure so we can release a patch before going public. High-severity issues are prioritised; if remediation takes longer than 30 days, we will keep you informed.

We’re happy to credit researchers in release notes unless you request otherwise.

## Dependency & Secrets Policy

- Every dependency bump is reviewed monthly; high/critical advisories trigger out-of-band releases. See `pyproject.toml` for pinned versions and run `uv pip list --outdated` before release builds.
- Secrets live only in `config/secrets.yaml` (never committed) or Docker/OS-level secret stores. Rotate Telegram tokens, SMTP credentials, and S3 IAM keys every 90 days; the `config/secrets.yaml.example` file documents the required keys.
- A local pre-commit hook (`scripts/check_secrets_placeholders.py`) prevents placeholder removal from the example secrets file so that real credentials are never published.

## Transport Security

- External control surfaces (FastAPI control plane, websocket gateway, webhook deliveries) must use TLS. The configuration file exposes `system.control_api.tls` and `dashboards.websocket_gateway.tls`; enable them with certificates placed under `config/certs/` or terminate TLS via the packaged Caddy reverse proxy (`docker/Caddyfile`).
- Webhook outputs default to `require_https: true` to block accidental HTTP targets. Override per module only when the upstream network is fully trusted.
- Docker/systemd packaging scripts expose only TLS endpoints by default, and Simple queue consumers should pin certificates using the CA bundle you manage for the environment.
