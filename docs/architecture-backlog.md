# Architecture backlog

Gaps and follow-ups. **Operational detail** (paths, `data_root`, face, retention, hygiene, install) lives in the **[README](../README.md)** and **[configuration.md](configuration.md)**; this file is for **product/architecture** work not yet implemented.

**Direction:** **Standalone processes** (web, bot, capture/inference workers, etc.) coordinated by the **orchestrator** — not a single bundled monolith long term.

**Shipping (in tree):** `pyproject.toml` entry point + extras, Docker CPU/GPU + compose + `.dockerignore`, tag `v*` → wheel/sdist, smoke, GHCR push, GitHub Release — see README **Distribution & deployment**, **`CHANGELOG.md`**, **`docs/ops.md`**.

---

## Backlog

- **PyPI** — Publish wheels to PyPI (or a private index); document the index URL if not PyPI; optional **trusted publishing** or `PYPI_API_TOKEN`.
- **Docker Hub** — Optional mirror/registry in addition to GHCR.
- **Ops docs** — Expand **`docs/ops.md`** (backup, upgrades, runbooks) as behavior stabilizes.
- **Docs alignment** — Periodic pass: README, `docs/`, CLI `--help`, and `data/config/*.example` vs actual defaults and recipe keys.
- **YOLOv7** — Add integration (or compatibility layer) alongside the current Ultralytics/YOLOv8-style path; evaluate weights and pipeline differences.
- **Multi-process split** — Move from today’s layout toward standalone processes with clear boundaries, IPC, and restart behavior (orchestrator-owned).
- **Typed recipe** (e.g. Pydantic) — load-time validation, explicit schema versions.
- **Recipe ↔ software versioning** — recipe **format** vs **library (package) version** — compatibility matrix, optional fields in YAML, fail fast on mismatch.
- **Throttling** — events, CPU, HTTP driven by metrics.
- **Disaster recovery** — backup/restore runbooks, schedules, integrity checks (ops; see **[configuration.md](configuration.md)** for a minimal backup table).
