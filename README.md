<div align="center">
  <img src="assets/logo_simple.png" alt="Spyoncino Logo" width="200"/>
  
  # Spyoncino
  
  **AI-powered security system with motion detection, person recognition, and Telegram notifications.**
  
  [![Python Version](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/downloads/)
  [![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
  [![Status](https://img.shields.io/badge/status-alpha-orange)]()
  [![Tests](https://github.com/AndreaMichielon/spyoncino/workflows/Tests/badge.svg)](https://github.com/AndreaMichielon/spyoncino/actions)
  [![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
</div>

> ⚠️ **Alpha Version (0.0.1-alpha)** - This project is under active development. Features may change and bugs may exist.

## Table of Contents

- [Features](#features)
- [Quick Start](#quick-start)
  - [Prerequisites](#prerequisites)
  - [Installation](#installation)
    - [Quick Start](#quick-start-recommended)
    - [GPU Detection Modes](#gpu-detection-modes)
    - [Launcher Troubleshooting](#launcher-troubleshooting)
  - [Manual Installation](#manual-installation)
- [Configuration](#configuration)
- [Security & Secrets](#security--secrets)
- [Contributing](#contributing)
- [License](#license)



## Features

- Real-time motion detection with OpenCV background subtraction
- YOLOv8 person recognition with GPU acceleration
- Interactive Telegram bot with instant notifications
- Smart GIF generation with temporal sampling and compression
- Automatic storage cleanup with configurable retention
- Secure multi-user access control with password-based setup
- Well-organized YAML configuration with separate secrets file

## Quick Start

1. **Grab the project** – Clone the repo or download the latest release.
2. **Have your Telegram bot token ready** – You will add it during setup.
3. **Run the launcher** – It bootstraps everything for you and starts the modular orchestrator.

> 💡 The launcher is fully automated. Sit back and watch while it checks Python, sets up UV, creates the environment, installs PyTorch, and launches the app.

### Prerequisites
- Internet connection *(the launcher fetches Python 3.12+ and dependencies automatically if needed)*
- 2GB RAM, 1GB storage
- USB webcam or IP camera
- Telegram bot token from [@BotFather](https://t.me/botfather)

### Installation

#### Quick Start (Recommended)

**Windows**
```bash
run.bat
```

**Linux/Mac**
```bash
chmod +x run.sh
./run.sh
```

When you run the launcher it will:
- ✅ Check or install Python 3.12+
- ✅ Install UV if missing and create a virtual environment
- ✅ Auto-detect your GPU and pull the right PyTorch build
- ✅ Verify PyTorch and fix mismatches automatically
- ✅ Install all other dependencies
- ✅ Launch Spyoncino (async modular orchestrator)

> 🕹️ **Need the classic stack?** Set `SPYONCINO_LEGACY=1` (or `set SPYONCINO_LEGACY=1` on Windows) before running the launcher to start the legacy pipeline instead. Advanced users can also override the entrypoint with `SPYONCINO_ENTRYPOINT=spyoncino-legacy`.
> 🎥 **Camera selection:** The modular runner now reads `config/config.yaml` and automatically boots USB and/or RTSP camera modules for every configured camera. If no hardware inputs are defined it falls back to the simulator; force simulator mode anytime with `spyoncino --preset sim`.

#### GPU Detection Modes

1. **Automatic (Default)**: Detects NVIDIA GPU and installs correct PyTorch
   ```bash
   ./run.sh  # or run.bat
   ```

2. **Force GPU mode**: Install CUDA-enabled PyTorch even if no GPU detected
   ```bash
   export SPYONCINO_PYTORCH=cuda  # Linux/Mac
   set SPYONCINO_PYTORCH=cuda     # Windows
   ./run.sh  # or run.bat
   ```

3. **Force CPU mode**: Install CPU-only PyTorch (smaller, faster install)
   ```bash
   export SPYONCINO_PYTORCH=cpu  # Linux/Mac
   set SPYONCINO_PYTORCH=cpu     # Windows
   ./run.sh  # or run.bat
   ```

#### Launcher Troubleshooting
- Needs one of `curl`/`wget` (Unix) or PowerShell with internet to download UV/Python.
- If the machine is offline or downloads are blocked, install Python 3.12 manually and rerun.
- After bootstrapping once, cached UV/Python are reused—delete the virtualenv to force a refresh.

### Manual Installation

0. **Prerequisite**: Make sure Python 3.12+ and the UV package manager are already installed on your system.

1. **Create and activate virtual environment**
   ```bash
   uv venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

2. **Install Spyoncino with PyTorch**
   
   **GPU with CUDA:**
   ```bash
   uv pip install -e . --index-url https://download.pytorch.org/whl/cu118
   ```
   
   **CPU only:**
   ```bash
   uv pip install -e . --index-url https://download.pytorch.org/whl/cpu
   ```
   
   **Standard:**
   ```bash
   uv pip install -e .
   ```

3. **Configure system**
   
   The configuration files are already created with sensible defaults. You only need to create the secrets file:
   
   ```bash
   cp config/secrets.yaml.example config/secrets.yaml
   ```
   
   Edit `config/secrets.yaml` with your Telegram credentials:
   ```yaml
   telegram:
     token: "your_bot_token_here"    # Get from @BotFather
     chat_id: null                   # Defaults to superuser + whitelisted chats when unset

   authentication:
     setup_password: "YourSecurePassword123!"  # For /setup command
     superuser_id: null              # Auto-set during setup
     user_whitelist: []              # Managed via bot commands
   ```

4. **Secure the secrets file**
   ```bash
   chmod 600 config/secrets.yaml
   ```

5. **Run**
   ```bash
   spyoncino  # modular orchestrator (default)
   # or, for the old synchronous CLI:
   spyoncino-legacy
   ```

### Legacy Runner (Retrocompatibility)

The modular orchestrator (`spyoncino` / `spyoncino-modular`) is now the default runtime. If you need the previous synchronous CLI for an existing deployment, use:

```bash
spyoncino-legacy --help
```

- Accepts the same configuration files as the new stack.
- Receives security updates but no new features; expect the orchestrator to diverge quickly.
- You can flip launchers into legacy mode with `SPYONCINO_LEGACY=1`.

## Security Setup

### First-Time Configuration
1. Start the bot
2. Message your bot in Telegram
3. Run `/setup <setup_password>` using the value you set in `config/secrets.yaml` (this makes you the superuser)

### User Roles
- **Superuser**: Full control, manages users
- **Whitelisted Users**: View recordings, snapshots
- **Unauthorized**: Blocked with rate limiting

## Usage

### Telegram Bot Commands

The modular Telegram bot is split into two roles:

- **Security bot (legacy stack)**: rich, single-camera control (unchanged).
- **Dashboard control bot (modular stack)**: thin, multi-camera control surface that talks over the event bus.

The table below documents the **dashboard Telegram control bot** commands exposed by `modules.dashboard.telegram_bot.TelegramControlBot`.

**Before You're Authorized:**

| Command | Function |
|---------|----------|
| `/start` | Intro message with next steps |
| `/help` | Shows limited help and how to request access |
| `/whoami` | Show your Telegram ID and access status |
| `/setup <password>` | First-time superuser setup (only before a superuser exists) |

> ℹ️ **Note:** Not whitelisted yet? Run `/whoami`, copy the ID, and share it with the superuser to get access.

**Status & Health:**

| Command | Function |
|---------|----------|
| `/status` | System health summary from `status.health.summary` |
| `/stats` | Storage and basic telemetry (`StorageStats`) |

**Camera Control (per-camera or global):**

| Command | Function |
|---------|----------|
| `/enable [camera_id]` | Enable a camera (`camera.state` with `enabled=true`) |
| `/disable [camera_id]` | Disable a camera (`enabled=false`) |
| `/snapshot [camera_id]` or `/snap [camera_id]` | Request a snapshot from a camera (`camera.snapshot`) |
| `/start_monitor [camera_id]` | Start monitoring globally or for a specific camera (`system.monitor.start`) |
| `/stop_monitor [camera_id]` | Stop monitoring globally or for a specific camera (`system.monitor.stop`) |

**Recordings & Playback:**

| Command | Function |
|---------|----------|
| `/recordings [camera_id]` | Ask the backend for recordings list; shows **inline buttons** grouped as Today/Yesterday/Older |
| (tap button) | Sends a `recordings.get` command for that item and replies with the GIF/clip |
| `/get <event_name>` | Request a specific recording by filename stem (e.g. `person_20250101_121500`) |
| `/get <camera_id>` | Request the **latest recording** whose filename contains `<camera_id>` (best-effort) |

**Analytics & Debug:**

| Command | Function |
|---------|----------|
| `/timeline [hours]` | Request an analytics timeline image for the last N hours (`analytics.timeline`) |
| `/analytics [hours]` | Request an analytics summary for the last N hours (`analytics.summary`) |
| `/test` | Queue a test notification through the dashboard (`system.notification.test`) |

**Configuration (Dashboard & Bot):**

| Command | Function |
|---------|----------|
| `/config <key> <value>` | Superuser-only; forwards a generic configuration update as `config.update` |
| `/show_config` | Superuser-only; shows current Telegram control bot settings (rate limit, whitelist, topics, etc.) |

**Admin (Superuser Only):**

| Command | Function |
|---------|----------|
| `/whitelist_add <user_id>` | Add a Telegram user id to the whitelist |
| `/whitelist_remove <user_id>` | Remove a user from the whitelist |
| `/whitelist_list` | Show all whitelisted users |
| `/cleanup` | Request an aggressive storage cleanup (`storage.cleanup`) |

> 💡 **Tip:** For permanent changes (camera manifests, detection thresholds, etc.) prefer editing `config/config.yaml` and reloading via `config.update`, and use `/config` for quick, runtime experiments.

## Configuration Files

Configuration is split into three YAML files for better organization:

### config/config.yaml (Safe to commit)
General system settings organized by category:

**Camera Settings (`cameras[]` entries):**
| Setting | Default | Description |
|---------|---------|-------------|
| `camera_id` | `"default"` | Identifier that becomes `camera.<id>.frame` |
| `usb_port` / `rtsp_url` | `0` / `null` | Select USB device index or RTSP URL |
| `width` / `height` | `1280x720` | Resolution (`null` keeps native; single-axis overrides maintain aspect) |
| `fps` | `15` | Frames per second (`null` keeps driver timing) |
| `notes` | `null` | Optional metadata for operators |

**Multiple Camera Inputs**
- Populate `cameras:` with one entry per physical feed; the orchestrator now instantiates an input module per entry automatically.
- Mixed transports are supported: specify `usb_port` for USB devices, `rtsp_url` for network streams, or leave both for simulator-only configs.
- Downstream modules automatically subscribe to all `camera.<id>.frame` topics (motion, YOLO, GIF/clip builders, etc.); no manual wiring needed.
- Each entry must keep a unique `camera_id` so topics, snapshots, and notifications remain distinguishable.
- See `tests/unit/test_dual_camera_pipeline.py` for a representative multi-camera orchestration test.

**Detection Settings:**
| Setting | Default | Description |
|---------|---------|-------------|
| `interval` | `2.0` | Detection frequency (seconds) |
| `confidence` | `0.25` | AI sensitivity (0.1-0.9) |
| `motion_threshold` | `5` | Motion detection sensitivity |
| `label_cooldown_seconds` | `30.0` | Cooldown between repeated alert labels |

**Storage Settings:**
| Setting | Default | Description |
|---------|---------|-------------|
| `path` | `recordings` | Storage directory |
| `retention_hours` | `24` | Recording retention duration |
| `low_space_threshold_gb` | `1.0` | Free space threshold |

**Notification Settings:**
| Setting | Default | Description |
|---------|---------|-------------|
| `gif_for_motion` | `false` | Generate GIF for motion events |
| `gif_for_person` | `true` | Generate GIF for person events |
| `gif_fps` | `15` | Internal GIF quality |
| `notification_gif_fps` | `10` | Telegram GIF quality |

> ℹ️ **Telegram fan-out:** The modular `telegram_notifier` automatically sends alerts to every chat listed in its `chat_targets`. When not provided, it builds this list from `telegram.chat_id`, `system.security.notification_chat_id`, the configured superuser, and every whitelisted user (private chats use the same numeric ID as the user). You can override or extend per media type with `gif_chat_targets` / `clip_chat_targets` inside `outputs.modules`.

#### Notification Message Format

- Captions are legacy-style and hardcoded (no template configuration required).
- The leading timestamp appears in square brackets as `[dd/mm/'yy hh:mm.ss]` (UTC).
- The event label derives from the detection type:
  - Motion events → `Motion on <camera_id>`
  - Object/person detections → `Detection on <camera_id>`
- The emoji reflects the delivery mode:
  - Text: `📝`
  - Photo (snapshot): `📸`
  - Animation (GIF): `🎞️`
  - Video (MP4): `🎥`

Examples:
```
[16/11/'25 21:45.07] 📝 Motion on default
[16/11/'25 21:45.07] 📸 Motion on garage
[16/11/'25 21:45.07] 🎞️ Detection on default
[16/11/'25 21:45.07] 🎥 Detection on backyard
```

Rate limiting:
- Motion snapshots are throttled by the `outputs.rate_limit` module.
- Ensure the Telegram notifier listens to the throttled topic:
  - `outputs.modules[].options.topic: "event.snapshot.allowed"`

### Telegram bot settings (inside `config/config.yaml`)
Telegram bot and security settings now live alongside the rest of the system configuration:

| Setting | Default | Description |
|---------|---------|-------------|
| `notification_chat_id` | `null` | Target chat for notifications |
| `allow_group_commands` | `true` | Enable group chat commands |
| `silent_unauthorized` | `true` | Silent mode for unauthorized users |
| `notification_rate_limit` | `5` | Max notifications per minute |

### config/secrets.yaml (Never commit - already in .gitignore)
Sensitive credentials and authentication:

| Setting | Required | Description |
|---------|----------|-------------|
| `telegram.token` | Yes | Bot token from @BotFather |
| `telegram.chat_id` | No | Falls back to superuser/whitelisted chats |
| `authentication.setup_password` | Recommended | First-time setup password |
| `authentication.superuser_id` | No | Set automatically during `/setup` |
| `authentication.user_whitelist` | No | Managed via bot commands |

## Security Features

### Multi-layer Protection
- **Password-based setup**: Prevents unauthorized superuser access
- **Rate limiting**: 5 failed attempts = temporary lockout
- **Input sanitization**: Prevents command injection
- **Separate secrets file**: YAML-based isolated sensitive data
- **User whitelisting**: Granular access control

**Alternative: Environment Variables**
```bash
export TELEGRAM_BOT_TOKEN="your_token"
export SECURITY_SETUP_PASSWORD="your_password"
export TELEGRAM_CHAT_ID="123456789"
```

## Architecture

```
 Entry Point    →    Control API    ↔    Event Bus    ↔    Processing Modules
(orchestrator)        (FastAPI)         (async core)        (vision, alerts)
                          ↓                  ↓                     ↓
                     Notifications      Auto-cleanup           Camera Feed
```

**Core Components:**
- **SecuritySystem**: Motion detection, YOLO inference, GIF generation
- **EventManager**: Event coordination, storage management, analytics
- **TelegramBot**: Command interface, notifications, user management
- **Capture**: Camera wrapper with error handling

## Troubleshooting

**Camera not found:**
```bash
ls /dev/video*  # Linux - list cameras
# Try USB_PORT: 0, 1, 2...
```

**High resource usage:**
- Increase `INTERVAL` for less frequent checks
- Reduce camera resolution in settings
- Lower `MAX_BATCH_SIZE` for GPU memory

**PyTorch/GPU issues:**
- Check GPU drivers: `nvidia-smi` should show your GPU
- For manual control: `export SPYONCINO_PYTORCH=cpu` or `=cuda`
- For CPU-only: `uv pip install -e . --index-url https://download.pytorch.org/whl/cpu`
- For CUDA: `uv pip install -e . --index-url https://download.pytorch.org/whl/cu118`

**System shows "CPU-only" despite having GPU:**
1. The launcher auto-fixes this! Just run `run.bat` or `run.sh` again
2. It will detect the wrong PyTorch version and reinstall with CUDA automatically
3. Or manually force reinstall:
   ```bash
   # Windows
   set SPYONCINO_PYTORCH=cuda
   run.bat
   
   # Linux/Mac
   export SPYONCINO_PYTORCH=cuda
   ./run.sh
   ```

**Bot unresponsive:**
- Verify `TELEGRAM_TOKEN` is correct
- Check internet connectivity
- Review logs in `recordings/security_system.log`

**Security issues:**
- Ensure `config/secrets.yaml` has proper permissions (600)
- Use strong setup password
- Monitor failed login attempts in logs

## Deployment

- **Docker Compose (recommended):** use `docker/compose.modular.yaml` for a single-node stack (`docker compose -f docker/compose.modular.yaml up -d`). The HA/chaos profile in `docker/compose.dev-ha.yaml` adds a canary orchestrator plus a TLS front proxy (`docker compose -f docker/compose.modular.yaml -f docker/compose.dev-ha.yaml --profile ha up -d`).
- **Systemd service:** copy `docker/systemd/spyoncino-modular.service` into `/etc/systemd/system/`, adjust `Environment=` lines for your preset/config path, and run `systemctl enable --now spyoncino-modular`.
- **TLS defaults:** both the Control API (port 8080) and the websocket gateway (8765) accept `tls_*` options in `config/config.yaml`. Place certificates under `config/certs/` (or mount them into `/certs` inside Docker) and set `system.control_api.tls.enabled = true`, `dashboards.websocket_gateway.tls.enabled = true`. If you prefer terminating TLS at the edge, point the included `docker/Caddyfile` at your certificate/key pair.
- **Runbooks:** day-2 procedures (startup, blue/green migrations, queue backpressure drills, S3 sync monitoring) live in `docs/RUNBOOKS.md`.
- **Secrets:** keep `config/secrets.yaml` on disk with `chmod 600` (or mount via Docker secrets). The example file now documents Telegram tokens, SMTP credentials, webhook headers, and S3 IAM profiles.
- **Maintenance:** monitor disk usage under `recordings/`, update dependencies through `uv pip install --upgrade -e .`, and back up `config/config.yaml` whenever camera manifests or dashboards change.

## Technical Details

- **Motion Detection**: Background subtraction with configurable thresholds
- **Person Recognition**: YOLOv8n with confidence filtering
- **GIF Optimization**: Temporal importance sampling, 640px max
- **Storage**: Auto-cleanup based on age and disk space
- **Analytics**: SQLAlchemy ORM with connection pooling for event tracking
- **Security**: Rate limiting, input sanitization, encrypted secrets
- **PyTorch**: Auto-detects GPU, uses optimized index URLs

## Contributing

Contributions are welcome! We have a comprehensive development setup with code quality tools.

### For Contributors

1. **Setup Development Environment:**
   ```bash
   # Windows
   dev\setup_dev.bat
   
   # Linux/Mac
   ./dev/setup_dev.sh
   
   # Or using Make
   make dev-install
   ```

2. **Setup Guide:** See [dev/SETUP.md](dev/SETUP.md)
3. **Development Reference:** See [dev/DEVELOPMENT.md](dev/DEVELOPMENT.md)
4. **Contributing Guidelines:** See [CONTRIBUTING.md](CONTRIBUTING.md)

### Code Quality Tools

- **Ruff** - Lightning-fast linter and formatter
- **mypy** - Static type checker
- **pytest** - Testing framework with coverage
- **pre-commit** - Automated code quality checks
- **bandit** - Security vulnerability scanner

Pre-commit hooks run automatically on every commit to ensure code quality.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
