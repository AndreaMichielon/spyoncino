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
- **Encrypted configuration management with separate secrets file**

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
- ✅ Install all dependencies
- ✅ Run Spyoncino

To manually override GPU detection:
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
   
   Create `config/setting.json`:
   ```json
   {
     "USB_PORT": 0,
     "CAMERA_WIDTH": 1280,
     "CAMERA_HEIGHT": 720,
     "DETECTION_CONFIDENCE": 0.25,
     "MOTION_THRESHOLD": 5000,
     "STORAGE_PATH": "recordings",
     "GIF_FOR_MOTION": false,
     "GIF_FOR_PERSON": true
   }
   ```
   
   Create `config/secrets.json`:
   ```bash
   cp config/secrets.json.example config/secrets.json
   ```
   
   Edit with your credentials:
   ```json
   {
     "TELEGRAM_TOKEN": "your_bot_token_here",
     "CHAT_ID": null,
     "SETUP_PASSWORD": "YourSecurePassword123!",
     "SUPERUSER_ID": null,
     "USER_WHITELIST": []
   }
   ```

4. **Secure the secrets file**
   ```bash
   chmod 600 config/secrets.json
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

**Admin (Superuser Only):**
- `/whitelist_add <user_id>` - Authorize users
- `/whitelist_remove <user_id>` - Remove user access  
- `/whitelist_list` - Show authorized users
- `/cleanup` - Force file cleanup
- `/whoami` - Show your user ID and status

## Configuration Files

### config/setting.json (Safe to commit)
| Setting | Default | Description |
|---------|---------|-------------|
| `USB_PORT` | `0` | Camera device index |
| `INTERVAL` | `2.0` | Detection frequency (seconds) |
| `DETECTION_CONFIDENCE` | `0.25` | AI sensitivity (0.1-0.9) |
| `RETENTION_HOURS` | `24` | Recording storage duration |
| `MOTION_THRESHOLD` | `5000` | Motion detection sensitivity |

### config/secrets.json (Never commit - already in .gitignore)
| Setting | Required | Description |
|---------|----------|-------------|
| `TELEGRAM_TOKEN` | Yes | Bot token from @BotFather |
| `SETUP_PASSWORD` | No | First-time setup password |
| `CHAT_ID` | No | Auto-detected from first message |
| `SUPERUSER_ID` | No | Set automatically during setup |
| `USER_WHITELIST` | No | Managed via bot commands |

## Security Features

### Multi-layer Protection
- **Password-based setup**: Prevents unauthorized superuser access
- **Rate limiting**: 5 failed attempts = temporary lockout
- **Input sanitization**: Prevents command injection
- **Separate secrets file**: Isolated sensitive data
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

**PyTorch installation issues:**
- For manual control: `export SPYONCINO_PYTORCH=cpu` or `=cuda`
- Check GPU drivers: `nvidia-smi` should show your GPU
- For CPU-only: `uv pip install -e . --index-url https://download.pytorch.org/whl/cpu`
- For CUDA: `uv pip install -e . --index-url https://download.pytorch.org/whl/cu118`

**Bot unresponsive:**
- Verify `TELEGRAM_TOKEN` is correct
- Check internet connectivity
- Review logs in `recordings/security_system.log`

**Security issues:**
- Ensure `config/secrets.json` has proper permissions (600)
- Use strong setup password
- Monitor failed login attempts in logs

## Deployment

**Production:**
- Use environment variables for secrets
- Set file permissions: `chmod 600 config/secrets.json`
- Regularly review logs and user whitelist

**Maintenance:**
- Monitor storage usage
- Update dependencies: `uv pip install --upgrade -e .`
- Backup `config/setting.json` only

## Technical Details

- **Motion Detection**: Background subtraction with configurable thresholds
- **Person Recognition**: YOLOv8n with confidence filtering
- **GIF Optimization**: Temporal importance sampling, 640px max
- **Storage**: Auto-cleanup based on age and disk space
- **Security**: Rate limiting, input sanitization, encrypted secrets
- **PyTorch**: Auto-detects GPU, uses optimized index URLs

## Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on how to contribute to this project.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.