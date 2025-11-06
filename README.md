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

## Features

- Real-time motion detection with OpenCV background subtraction
- YOLOv8 person recognition with GPU acceleration
- Interactive Telegram bot with instant notifications
- Smart GIF generation with temporal sampling and compression
- Automatic storage cleanup with configurable retention
- **Secure multi-user access control with password-based setup**
- **Well-organized YAML configuration with separate secrets file**

## Quick Start

### Prerequisites
- Python 3.12+, 2GB RAM, 1GB storage
- USB webcam or IP camera
- Telegram bot token from [@BotFather](https://t.me/botfather)

### Installation

#### Quick Start (Recommended)

**Windows:**
```bash
run.bat
```

**Linux/Mac:**
```bash
chmod +x run.sh
./run.sh
```

The launcher will automatically:
- ✅ Check Python version (3.12+ required)
- ✅ Install UV package manager if missing
- ✅ Create virtual environment
- ✅ **Auto-detect GPU and install optimal PyTorch version**
- ✅ **Verify PyTorch installation and auto-fix if wrong version detected**
- ✅ Install all dependencies
- ✅ Run Spyoncino

**GPU Detection Modes:**

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

#### Manual Installation

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
     chat_id: null                   # Auto-detected from first message
   
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
   run.bat          # Windows
   ./run.sh         # Linux/Mac
   
   # Or directly:
   spyoncino
   ```

## Security Setup

### First-Time Configuration
1. Start the bot
2. Message your bot in Telegram
3. Run `/setup YourSecurePassword123!` (you become superuser)

### User Roles
- **Superuser**: Full control, manages users
- **Whitelisted Users**: View recordings, snapshots
- **Unauthorized**: Blocked with rate limiting

## Usage

### Telegram Bot Commands

**Essential:**
| Command | Function |
|---------|----------|
| `/setup <password>` | First-time superuser setup |
| `/start` | Initialize system |
| `/status` | System overview |
| `/recordings` | Browse with interactive buttons |
| `/snap` | Live camera snapshot |
| `/config <key> <value>` | Runtime configuration |

**Configuration Examples:**
```
/config interval 1.5          # Faster detection
/config confidence 0.15       # More sensitive AI
/config gif_motion on         # Enable motion GIFs
```

> 💡 **Tip:** You can also edit `config/config.yaml` directly for permanent changes

**Admin (Superuser Only):**
- `/whitelist_add <user_id>` - Authorize users
- `/whitelist_remove <user_id>` - Remove user access  
- `/whitelist_list` - Show authorized users
- `/cleanup` - Force file cleanup
- `/whoami` - Show your user ID and status

## Configuration Files

Configuration is split into three YAML files for better organization:

### config/config.yaml (Safe to commit)
General system settings organized by category:

**Camera Settings:**
| Setting | Default | Description |
|---------|---------|-------------|
| `usb_port` | `0` | Camera device index |
| `width` / `height` | `1280x720` | Video resolution |
| `fps` | `15` | Frames per second |
| `brightness` / `contrast` | `null` | Optional camera adjustments |

**Detection Settings:**
| Setting | Default | Description |
|---------|---------|-------------|
| `interval` | `2.0` | Detection frequency (seconds) |
| `confidence` | `0.25` | AI sensitivity (0.1-0.9) |
| `motion_threshold` | `5` | Motion detection sensitivity |
| `person_cooldown_seconds` | `30.0` | Cooldown between person detections |

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

### config/telegram.yaml (Safe to commit)
Telegram bot and security settings:

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
| `telegram.chat_id` | No | Auto-detected from first message |
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
Entry Point → Telegram Bot ↔ Event Manager ↔ Security System
   (run.py)    (interface)      (coordination)   (AI detection)
                    ↓               ↓               ↓
                Notifications    Auto-cleanup    Camera Feed
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

**Production:**
- Use environment variables for secrets
- Set file permissions: `chmod 600 config/secrets.yaml`
- Regularly review logs and user whitelist

**Maintenance:**
- Monitor storage usage
- Update dependencies: `uv pip install --upgrade -e .`
- Backup `config/config.yaml` and `config/telegram.yaml` only (never commit `secrets.yaml`)

## Technical Details

- **Motion Detection**: Background subtraction with configurable thresholds
- **Person Recognition**: YOLOv8n with confidence filtering
- **GIF Optimization**: Temporal importance sampling, 640px max
- **Storage**: Auto-cleanup based on age and disk space
- **Analytics**: SQLAlchemy ORM with connection pooling for event tracking
- **Security**: Rate limiting, input sanitization, encrypted secrets
- **PyTorch**: Auto-detects GPU, uses optimized index URLs

## Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on how to contribute to this project.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.