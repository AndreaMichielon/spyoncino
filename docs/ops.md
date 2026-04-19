# Operations — deploy & shipping

Minimal reference for running Spyoncino outside a dev clone: install, layout, ports, containers, and releases.

## Install story (library + app)

- **From a git checkout (dev):** `uv sync` or `pip install -e ".[dev]"` — see the README.
- **From a wheel (CI artifact or future PyPI):**  
  `pip install path/to/spyoncino-*.whl`  
  Face identification (optional, heavy): `pip install 'spyoncino[face]'` (or `pip install .[face]` from repo root).
- **Entry point:** `spyoncino` → `spyoncino.orchestrator:main`. Subcommands: `spyoncino discover`, `spyoncino recipe-builder`.

PyTorch is pulled in via **Ultralytics**; for **GPU**, install the appropriate `torch`/`torchvision` wheels for your platform before or after installing Spyoncino (same as the README). The bundled **Dockerfile** pins **CPU** PyTorch.

## Data layout & config

- Process **working directory** should be the environment root (repo root, or `/app` in Docker).
- **`data_root`** (default `data` in the sample recipe) anchors SQLite, media, weights, etc.
- **`secrets_path`** in the recipe is **relative to cwd**, not joined with `data_root` — e.g. `data/config/secrets.yaml`.

Mount or copy at least:

| Path | Purpose |
|------|---------|
| `data/config/recipe.yaml` | Pipeline (cameras, interfaces, retention) |
| `data/config/secrets.yaml` | Telegram, auth — **never commit** |

See `data/config/*.example` and `docs/secrets-setup.md`.

## Environment variables (optional)

Secrets can also come from the environment (see `docs/secrets-setup.md`), including:

- `TELEGRAM_BOT_TOKEN`
- `SECURITY_SETUP_PASSWORD`, `TELEGRAM_CHAT_ID` (when applicable)

Discovery helper: `SPYONCINO_DISCOVERY_HOST`, `SPYONCINO_DISCOVERY_PORT` (see `spyoncino discover --help`).

## Ports (defaults)

| Service | Default | Notes |
|---------|---------|--------|
| Web dashboard (FastAPI) | `8000` | Recipe `interfaces` → `webapp` → `port` |
| First-run / bootstrap builder | `8000` | `--listen-port` when no default recipe |
| Standalone recipe builder | `8002` | `spyoncino recipe-builder` |
| Camera discovery UI | `8001` | `spyoncino discover` |

**Telegram bot** uses outbound HTTPS; no inbound port unless you add webhooks elsewhere.

## Docker

### Prebuilt images (GitHub Container Registry)

On each **`v*` tag**, the Release workflow pushes **`ghcr.io/<owner>/<repo>`** with:

| Variant | Tags | Dockerfile |
|--------|------|------------|
| **CPU** (default) | `<git tag>`, `latest` | `Dockerfile` |
| **GPU** (NVIDIA CUDA) | `<git tag>-gpu`, `latest-gpu` | `Dockerfile.gpu` |

Pull (`owner` / `repo` = GitHub repo, **lowercase**):

```bash
docker pull ghcr.io/<owner>/<repo>:latest          # CPU
docker pull ghcr.io/<owner>/<repo>:latest-gpu      # GPU
```

If the package is **private**, authenticate first: `echo $CR_PAT | docker login ghcr.io -u USERNAME --password-stdin` (use a PAT with `read:packages`). For **public** packages, no login is needed to pull.

Run **CPU** (mount your `data/`):

```bash
docker run --rm -p 8000:8000 -v "$PWD/data:/app/data" ghcr.io/<owner>/<repo>:latest
```

Run **GPU** (host needs [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html); mount `data/` the same way):

```bash
docker run --rm --gpus all -p 8000:8000 -v "$PWD/data:/app/data" ghcr.io/<owner>/<repo>:latest-gpu
```

The GPU image bundles **PyTorch CUDA 12.4** wheels (`cu124`); your **driver** must be new enough for that runtime. CPU and GPU images are separate tags — same app, different base and PyTorch build.

### Build locally

```bash
docker build -t spyoncino:local .
docker build -t spyoncino:local-gpu -f Dockerfile.gpu .
```

Run with **Compose** from the repo root (bind-mounts `./data`):

```bash
docker compose up --build
```

The image **CMD** is `spyoncino data/config/recipe.yaml` with **WORKDIR** `/app`. Ensure `data/config/recipe.yaml` and `data/config/secrets.yaml` exist under the mounted tree.

**Health check:** `GET http://127.0.0.1:8000/health` — matches the sample webapp on port `8000`. If you disable the web interface or change the port, adjust or remove the `HEALTHCHECK` in the Dockerfile.

**USB cameras:** Passing devices into containers is OS-specific (Linux: often `devices`, `privileged`, or `/dev/video*` mounts). RTSP/IP cameras usually need no extra device mapping.

## systemd (sketch)

Run from a venv or system install, with cwd set to your deployment root:

```ini
[Service]
WorkingDirectory=/opt/spyoncino
ExecStart=/opt/spyoncino/.venv/bin/spyoncino data/config/recipe.yaml
Restart=on-failure
```

Add `User=`, `Environment=`, and `ReadWritePaths=` for `data/` as needed.

## Releases & changelog

- **Version:** `pyproject.toml` → `[project].version` (PEP 440; e.g. `0.0.1a0`).
- **Tag a release:** push an annotated tag matching `v*` (e.g. `v0.0.1a0`). GitHub Actions **Release** workflow builds wheel + sdist, runs a **clean-venv smoke test**, builds and **pushes CPU and GPU Docker images to GHCR** (`:tag` / `:latest` and `:tag-gpu` / `:latest-gpu`), uploads `dist/` as a workflow artifact, and attaches artifacts to a **GitHub Release** with generated notes.
- **Changelog:** maintain `CHANGELOG.md` at the repo root; summarize user-visible changes per release.

### Publishing to PyPI (manual)

After `python -m build`:

```bash
python -m pip install twine
twine upload dist/*
```

Configure [trusted publishing](https://docs.pypi.org/trusted-publishers/) on PyPI if you want OIDC from GitHub Actions instead of tokens.

## CI reference

| Workflow | Trigger | Role |
|----------|---------|------|
| `tests.yml` | `push` / `pull_request` on `main` | Lint, mypy (non-blocking), tests |
| `release.yml` | `push` tags `v*` | Build wheel/sdist, smoke, Docker **CPU+GPU** build + **push to GHCR**, GitHub Release |
