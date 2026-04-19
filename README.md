<div align="center">
  <img src="assets/logo_simple.png" alt="Spyoncino Logo" width="200"/>

  # Spyoncino

  [![Python Version](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/downloads/)
  [![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
  [![Status](https://img.shields.io/badge/status-alpha-orange)]()
  [![Tests](https://github.com/AndreaMichielon/spyoncino/workflows/Tests/badge.svg)](https://github.com/AndreaMichielon/spyoncino/actions)
  [![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
</div>

Spyoncino is a personal project built to explore computer vision and AI in a practical way. The goal is simple: connect to cameras, watch what’s happening, and automatically detect motion, spot people, and tell apart familiar faces from potential intruders. It mixes hands-on engineering with a bit of experimental “vibe coding” to create something useful, lightweight, and reliable.

> ⚠️ **Alpha (0.0.1a0)** — This project is under active development. Features may change and bugs may exist. Versioning is **PEP 440** (`0.0.1a0`); git release tags use the **`v` prefix** (e.g. `v0.0.1a0`).

**Documentation index:** [`docs/README.md`](docs/README.md) (configuration, secrets, ops, optional face ID, backlog).

## Features

- Real-time motion detection with OpenCV background subtraction
- YOLOv8 person detection with GPU acceleration
- Optional **face identification** (known vs unknown identities) via DeepFace
- Interactive Telegram bot with instant notifications
- Smart GIF generation with temporal sampling and compression
- Automatic storage cleanup with configurable retention
- Secure multi-user access control with password-based setup
- Encrypted configuration management with a separate secrets file

## Quick start

### Prerequisites

- Python 3.12+, 2GB RAM, 1GB storage  
- USB webcam or IP camera  
- Telegram bot token from [@BotFather](https://t.me/botfather)

### Install and run (recommended)

Launchers live under **`scripts/`** (they `cd` to the repo root, so they work no matter where you invoke them from).

**Windows**

```bat
scripts\run.bat
```

**Linux / macOS**

```bash
chmod +x scripts/run.sh
./scripts/run.sh
```

The launcher will:

- Check Python 3.12+
- Install the `uv` package manager if missing
- Create a virtual environment (`.venv` on Unix, `spyoncino_env` on Windows — see `scripts/run.*`)
- Install the package in editable mode and start Spyoncino

### Manual installation

1. **Virtual environment**

   ```bash
   uv venv
   source .venv/bin/activate   # Windows: .venv\Scripts\activate
   ```

2. **Project dependencies** — `uv sync` installs Spyoncino and pulls **PyTorch via Ultralytics** (typically a **CPU** build good for development).

   ```bash
   uv sync
   # or: uv pip install -e .
   ```

3. **PyTorch variant (optional)** — Do this **inside the same venv** only if you need a specific wheel:

   - **NVIDIA GPU:** reinstall CUDA builds (adjust `cu118` to match your CUDA / [PyTorch install matrix](https://pytorch.org/get-started/locally/)):

     ```bash
     uv pip install torch torchvision --index-url https://download.pytorch.org/whl/cu118
     ```

   - **CPU-only pin:** only if you want to force CPU wheels from PyTorch’s index:

     ```bash
     uv pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
     ```

4. **Optional — face identification** (DeepFace; heavier deps)

   ```bash
   uv pip install -e ".[face]"
   ```

   See **[`docs/face-recognition.md`](docs/face-recognition.md)**.

5. **Configure**

   - Edit **`data/config/recipe.yaml`** (pipeline: cameras, motion, detector, interfaces, media paths), **or** use the first-run / recipe-builder flow below if you do not have a recipe yet.
   - Create **`data/config/secrets.yaml`** — **[`docs/secrets-setup.md`](docs/secrets-setup.md)**. Do not commit secrets.

## Running Spyoncino

Run from the **repository root** (or any fixed working directory) so relative paths in the recipe and `secrets_path` match how you start the process.

### With a recipe file

If you pass a YAML path, that file is used. If you omit it, Spyoncino looks for **`data/config/recipe.yaml`** under the current working directory.

```bash
spyoncino                                    # default: data/config/recipe.yaml
spyoncino data/config/recipe.yaml
spyoncino path/to/other-recipe.yaml
python -m spyoncino.orchestrator data/config/recipe.yaml
```

### Without a recipe yet (first-run)

If **`data/config/recipe.yaml` does not exist** and you did **not** pass a recipe path on the command line, Spyoncino starts an embedded **recipe builder** in the **same process**: open **http://127.0.0.1:8000** (use `--listen-host` / `--listen-port` to change bind address and port). Configure cameras, then **Save & start Spyoncino** — the orchestrator continues in that process (Ctrl+C stops everything).

### Other entry points

| Command | Purpose |
|---------|---------|
| `spyoncino recipe-builder` | Standalone recipe editor (default **http://127.0.0.1:8002**). See `spyoncino recipe-builder --help`. |
| `spyoncino discover` | Camera discovery UI (default **http://127.0.0.1:8001** — USB, optional LAN RTSP scan, manual IPs, vendor paths). `--host` / `--port` to override. |

## Wheels, Docker, and releases

### Install from a wheel

Useful for a machine without a full git checkout (e.g. artifact from a [GitHub Release](https://github.com/AndreaMichielon/spyoncino/releases)):

```bash
pip install /path/to/spyoncino-*.whl
# Optional face pipeline (heavier deps):
pip install 'spyoncino[face]'
```

You still need a **`data/`** layout: at least **`data/config/recipe.yaml`**, **`data/config/secrets.yaml`**, and paths for media/weights as in **[`docs/configuration.md`](docs/configuration.md)**. Run `spyoncino` from the directory you treat as the app root so recipe paths resolve correctly.

### Docker

From a clone of this repo:

```bash
docker compose up --build
```

This builds a local image, sets **`working_dir` to `/app`**, mounts **`./data` → `/app/data`**, and maps **8000** (web UI) by default. See **`docker-compose.yml`**, **`Dockerfile`** (CPU PyTorch), and **`Dockerfile.gpu`** (NVIDIA CUDA).

Tagged releases also publish images to **GitHub Container Registry** — CPU **`:latest`** and GPU **`:latest-gpu`** — with pull/run examples in **[`docs/ops.md`](docs/ops.md)**.

### Releases

Pushing a git tag **`v*`** (e.g. `v0.0.1a0`) runs the release workflow (wheel, smoke tests, Docker images). Changes are summarized in **`CHANGELOG.md`**.

**PyPI (when you enable it):** publish wheels/sdists for the same **PEP 440** version as `[project].version` in `pyproject.toml` (here **`0.0.1a0`**). The **`v`** prefix is **only** for git tags (e.g. `v0.0.1a0`), not for the package version on PyPI.

### Operators

Ports, environment variables, systemd-style notes, and GHCR pull commands — **[`docs/ops.md`](docs/ops.md)**.

## Security setup (Telegram)

1. Start the pipeline (`spyoncino` or `spyoncino data/config/recipe.yaml` after install — or complete the first-run recipe builder if you have no recipe yet).
2. Message your bot in Telegram.
3. Run `/setup YourSecurePassword123!`
4. You become the superuser.

**Roles:** superuser (full admin), whitelisted users (basic access), unauthorized users blocked with rate limiting.

## Usage (Telegram)

### Essential commands

| Command | Function |
|---------|----------|
| `/setup <password>` | First-time superuser setup |
| `/start` | Initialize system |
| `/status` | System overview |
| `/recordings` | Browse with interactive buttons |
| `/snap` | Live camera snapshot |
| `/config <key> <value>` | Runtime configuration |

### Configuration examples

```
/config interval 1.5
/config confidence 0.15
/config gif_motion on
```

### Admin (superuser)

- `/whitelist_add <user_id>`, `/whitelist_remove <user_id>`, `/whitelist_list`
- `/cleanup` — force file cleanup

### User

- `/whoami` — your user ID and authorization status

## Configuration (summary)

| File | Purpose |
|------|---------|
| `data/config/recipe.yaml` | Pipeline, retention, interfaces — safe to version |
| `data/config/secrets.yaml` | Telegram, auth, API keys — **never commit** |

**Deep dive:** paths, YOLO, retention, backup, and config troubleshooting — **[`docs/configuration.md`](docs/configuration.md)**.

## Security features

- Password-based `/setup`, rate limiting, input sanitization  
- Secrets isolated in **`data/config/secrets.yaml`** (or env vars — see **[`docs/secrets-setup.md`](docs/secrets-setup.md)**)

```bash
export TELEGRAM_BOT_TOKEN="your_token"
export SECURITY_SETUP_PASSWORD="your_password"
export TELEGRAM_CHAT_ID="123456789"
spyoncino data/config/recipe.yaml
```

## Architecture

```
Entry Point → Interfaces (Telegram, Web) ↔ Orchestrator ↔ Inputs / CV pipeline
(orchestrator)   (interfaces)              (main loop)        (recipe-driven stages)
```

Core pieces: motion + YOLO pipeline, event/storage/analytics, Telegram bot, camera capture.

## Troubleshooting

| Issue | What to try |
|-------|-------------|
| Camera not found | Linux: `ls /dev/video*`; try USB index `0`, `1`, … |
| High CPU/GPU | Increase detection interval; lower resolution; reduce YOLO batch in recipe |
| Bot not responding | Check `TELEGRAM_BOT_TOKEN` and connectivity; see app logs on stderr |
| Secrets / paths | **[`docs/configuration.md`](docs/configuration.md)** runbook |

**Repository hygiene:** committed **`uv.lock`** — use **`uv sync`**. Do not commit **`data/config/secrets.yaml`**, runtime media, `*.db`, weights, or gallery images — see **`.gitignore`**. Run from **repo root** when using relative paths in the recipe.

## Contributing

See **[CONTRIBUTING.md](CONTRIBUTING.md)**.

## License

MIT — see **[LICENSE](LICENSE)**.
