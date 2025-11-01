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

Use the automated launcher that handles everything for you:

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
- ✅ Install all dependencies
- ✅ Run Spyoncino

#### Manual Installation

1. **Install PyTorch**
   ```bash
   # GPU (recommended)
   uv pip install torch torchvision --index-url https://download.pytorch.org/whl/cu118
   
   # CPU only
   uv pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
   ```

2. **Install dependencies**
   ```bash
   # Create and activate virtual environment
   uv venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   
   # Install the package and dependencies
   uv pip install -e .
   ```

3. **Configure system**
   
   Create `config/setting.json` (non-sensitive settings):
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
   
   Create `config/secrets.json` from the example template:
   ```bash
   cp config/secrets.json.example config/secrets.json
   ```
   
   Then edit `config/secrets.json` with your sensitive data:
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
   chmod 600 config/secrets.json  # Restrict file permissions
   # Note: secrets.json is already in .gitignore
   ```

5. **Run**
   ```bash
   # Option 1: Automated launcher (recommended)
   run.bat          # Windows
   ./run.sh         # Linux/Mac
   
   # Option 2: Using the installed command
   spyoncino
   
   # Option 3: Direct Python execution
   python src/spyoncino/run.py config
   ```

## Security Setup

### First-Time Configuration
1. Start the bot: `python src/spyoncino/run.py config`
2. Message your bot in Telegram
3. Run `/setup YourSecurePassword123!`
4. You become the superuser with full admin access

### User Management
- **Superuser**: Full system control, can manage other users
- **Whitelisted Users**: Basic system access (view recordings, snapshots)
- **Unauthorized Users**: Blocked with rate limiting

## Usage

### Essential Commands
| Command | Function |
|---------|----------|
| `/setup <password>` | First-time superuser setup |
| `/start` | Initialize system |
| `/status` | System overview |
| `/recordings` | Browse with interactive buttons |
| `/snap` | Live camera snapshot |
| `/config <key> <value>` | Runtime configuration |

### Configuration Examples
```
/config interval 1.5          # Faster detection
/config confidence 0.15       # More sensitive AI
/config gif_motion on         # Enable motion GIFs
```

### Admin Commands (Superuser Only)
- `/whitelist_add <user_id>` - Authorize users
- `/whitelist_remove <user_id>` - Remove user access  
- `/whitelist_list` - Show authorized users
- `/cleanup` - Force file cleanup

### User Info
- `/whoami` - Show your user ID and authorization status

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
- **Rate limiting**: Blocks brute force attempts (5 attempts = temporary lockout)
- **Input sanitization**: Prevents command injection attacks
- **Separate secrets file**: Sensitive data isolated from main config
- **User whitelisting**: Granular access control

### Environment Variable Support
Alternative to `config/secrets.json`:
```bash
export TELEGRAM_BOT_TOKEN="your_token"
export SECURITY_SETUP_PASSWORD="your_password"
export TELEGRAM_CHAT_ID="123456789"
python run.py
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
# List available cameras
ls /dev/video*  # Linux
# Try USB_PORT values: 0, 1, 2...
```

**High resource usage:**
- Increase `INTERVAL` for less frequent checks
- Reduce camera resolution in settings
- Lower `MAX_BATCH_SIZE` for GPU memory

**Bot unresponsive:**
- Verify `TELEGRAM_TOKEN` is correct
- Check internet connectivity
- Review logs in `recordings/security_system.log`

**Security issues:**
- Ensure `config/secrets.json` has proper permissions (600)
- Check that secrets file is in `.gitignore` (already configured)
- Use strong setup password
- Monitor failed login attempts in logs

## Deployment Best Practices

1. **Production Setup:**
   - Use environment variables instead of `config/secrets.json`
   - Set up proper file permissions
   - Configure firewall rules
   - Enable system logging

2. **Access Control:**
   - Use strong setup password
   - Regularly review user whitelist
   - Monitor authentication logs
   - Consider using dedicated notification chat

3. **Maintenance:**
   - Regular log review
   - Storage cleanup monitoring
   - Update dependencies periodically
   - Backup configuration files (excluding secrets)

## Technical Details

- **Motion Detection**: Background subtraction with configurable thresholds
- **Person Recognition**: YOLOv8n with confidence filtering and anti-spam
- **GIF Optimization**: Temporal importance sampling with 640px max resolution
- **Storage Management**: Automatic cleanup based on age and disk space
- **Error Handling**: Comprehensive logging with Unicode-safe formatting
- **Security**: Rate limiting, input sanitization, encrypted secrets management

## Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on how to contribute to this project.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.