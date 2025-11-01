#!/usr/bin/env python3
"""
Professional Security System Main Runner

This script initializes and runs the complete AI-powered security system
with motion detection, person recognition, and Telegram notifications.
"""

import os
import logging
import json
import sys
from pathlib import Path
from typing import Dict, Any

# Import our improved professional classes
from spyoncino.capture import Capture
from spyoncino.security import SecuritySystem  # Updated from Security
from spyoncino.manager import SecurityEventManager  # Updated from SecurityManager  
from spyoncino.bot import SecurityTelegramBot, BotConfig  # Updated from SecurityBot

class SafeFormatter(logging.Formatter):
    """Custom formatter that handles Unicode encoding issues."""
    
    def format(self, record):
        try:
            formatted = super().format(record)
            return formatted.encode('utf-8', errors='replace').decode('utf-8')
        except UnicodeError:
            return record.getMessage()

class SecuritySystemRunner:
    """
    Professional runner for the complete security system.
    
    Handles configuration loading, component initialization,
    and graceful startup/shutdown of all system components.
    """
    
    def __init__(self, config_dir: str = "config"):
        """
        Initialize the security system runner.
        
        Args:
            config_dir: Path to configuration directory containing setting.json and secrets.json
            
        Raises:
            ValueError: If config_dir is empty or invalid
        """
        if not config_dir or not config_dir.strip():
            raise ValueError("config_dir cannot be empty")
        
        # Resolve paths relative to project root (parent of src/spyoncino)
        project_root = Path(__file__).parent.parent.parent
        self.config_dir = project_root / config_dir
        self.config_path = self.config_dir / "setting.json"
        self.secrets_path = self.config_dir / "secrets.json"
        self.config = self._load_configuration()
        
        # Components (initialized later)
        self.capture = None
        self.security_system = None
        self.event_manager = None
        self.bot = None
        
        # Setup logging
        self._setup_logging()
        self.logger = logging.getLogger(self.__class__.__name__)
    
    def _load_configuration(self) -> Dict[str, Any]:
        """Load configuration from config/setting.json and optional config/secrets.json."""
        config = {}
        
        # Load main configuration (required)
        if not self.config_path.exists():
            raise FileNotFoundError(
                f"Configuration file not found: {self.config_path}\n"
                f"Please create config/setting.json based on the example or use an existing configuration file."
            )
        
        try:
            with open(self.config_path, "r", encoding='utf-8') as f:
                config = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ValueError(f"Invalid JSON in config file: {e}")
        except (OSError, IOError) as e:
            raise RuntimeError(f"Cannot read config file: {e}")
        
        # Load secrets file (optional - fallback to env vars)
        if self.secrets_path.exists():
            try:
                with open(self.secrets_path, "r", encoding='utf-8') as f:
                    secrets = json.load(f)
                    config.update(secrets)
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                raise ValueError(f"Invalid JSON in secrets file: {e}")
            except (OSError, IOError) as e:
                raise RuntimeError(f"Cannot read secrets file: {e}")
        else:
            # Fall back to environment variables
            import os
            config["TELEGRAM_TOKEN"] = os.environ.get("TELEGRAM_BOT_TOKEN", config.get("TELEGRAM_TOKEN", ""))
            config["CHAT_ID"] = os.environ.get("TELEGRAM_CHAT_ID", config.get("CHAT_ID"))
            config["SETUP_PASSWORD"] = os.environ.get("SECURITY_SETUP_PASSWORD", config.get("SETUP_PASSWORD"))

        # Convert CHAT_ID to int if provided
        if config.get("CHAT_ID"):
            try:
                config["CHAT_ID"] = int(config["CHAT_ID"])
            except (ValueError, TypeError):
                if hasattr(self, 'logger'):
                    self.logger.warning("Invalid CHAT_ID format, ignoring")
                config["CHAT_ID"] = None

        # Flatten nested config for backward compatibility
        flattened = {}
        for key, value in config.items():
            if isinstance(value, dict) and not key.startswith('_'):
                flattened.update(value)
            elif not key.startswith('_'):
                flattened[key] = value
        
        # Validate required settings
        if not flattened.get("TELEGRAM_TOKEN"):
            raise ValueError("TELEGRAM_TOKEN is required (add to config/secrets.json or set TELEGRAM_BOT_TOKEN env var)")
        
        return flattened

    def _setup_logging(self) -> None:
        """Setup comprehensive logging configuration with Unicode safety."""
        import logging.handlers
        
        try:
            log_level = getattr(logging, self.config.get("LOG_LEVEL", "INFO").upper())
            log_format = self.config.get(
                "LOG_FORMAT", 
                "%(asctime)s [%(levelname)8s] %(name)s: %(message)s"
            )
            
            # Create safe formatter
            safe_formatter = SafeFormatter(log_format)
            
            # Create rotating file handler
            log_dir = Path(self.config.get("STORAGE_PATH", "recordings"))
            log_dir.mkdir(parents=True, exist_ok=True)

            file_handler = logging.handlers.RotatingFileHandler(
                filename=log_dir / "security_system.log",
                maxBytes=self.config.get("LOG_MAX_SIZE_MB", 10) * 1024 * 1024,
                backupCount=self.config.get("LOG_BACKUP_COUNT", 3),
                encoding='utf-8'  # Explicit UTF-8 for file
            )
            file_handler.setFormatter(safe_formatter)
            
            # Console handler with safe encoding
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setFormatter(safe_formatter)
            
            # Configure root logger
            logging.basicConfig(
                level=log_level,
                handlers=[console_handler, file_handler]
            )
            
            # Set specific log levels for noisy libraries
            logging.getLogger("ultralytics").setLevel(logging.WARNING)
            logging.getLogger("telegram").setLevel(logging.INFO)
            logging.getLogger("apscheduler").setLevel(logging.WARNING)
            logging.getLogger("httpx").setLevel(logging.WARNING)
            logging.getLogger("httpcore").setLevel(logging.WARNING)
        except (OSError, PermissionError) as e:
            print(f"Failed to setup logging: {e}", file=sys.stderr)
            raise

    def _initialize_components(self) -> None:
        """Initialize all system components with configuration."""
        self.logger.info("Initializing security system components...")
        
        try:
            if not self.config.get("TELEGRAM_TOKEN"):
                raise ValueError("TELEGRAM_TOKEN is required (add to config/secrets.json or set TELEGRAM_BOT_TOKEN env var)")
            usb_port = self.config.get("USB_PORT", 0)
            if isinstance(usb_port, int) and usb_port < 0:
                raise ValueError("USB_PORT must be non-negative")

            # 1. Initialize Capture with advanced options
            capture_config = {
                'width': self.config.get("CAMERA_WIDTH", 1280),
                'height': self.config.get("CAMERA_HEIGHT", 720),
                'fps': self.config.get("CAMERA_FPS", 30),
                'brightness': self.config.get("CAMERA_BRIGHTNESS"),
                'contrast': self.config.get("CAMERA_CONTRAST"),
            }

            for key, value in capture_config.items():
                if value is not None and not isinstance(value, (int, float)):
                    raise ValueError(f"Invalid {key} value: must be numeric")
                if value is not None and value <= 0:
                    raise ValueError(f"Invalid {key} value: must be positive")

            # Remove None values
            capture_config = {k: v for k, v in capture_config.items() if v is not None}
            
            try:
                self.capture = Capture(source=usb_port, **capture_config)
            except Exception as e:
                raise RuntimeError(f"Failed to initialize capture: {e}")
            
            # 2. Initialize SecuritySystem with professional settings
            try:
                self.security_system = SecuritySystem(
                    capture=self.capture,
                    event_folder=self.config.get("STORAGE_PATH", "recordings"),
                    config_dir=str(self.config_dir),
                    interval=max(0.1, self.config.get("INTERVAL", 2.0)),
                    record_frames=max(1, self.config.get("RECORD_FRAMES", 50)),
                    confidence=max(0.1, min(1.0,self.config.get("DETECTION_CONFIDENCE", 0.25))),
                    max_batch_size=self.config.get("MAX_BATCH_SIZE", 32),
                    motion_threshold=self.config.get("MOTION_THRESHOLD", 5),
                    gif_fps=self.config.get("GIF_FPS", 15),
                    max_gif_frames=self.config.get("MAX_GIF_FRAMES", 20),
                    person_cooldown_seconds=self.config.get("PERSON_COOLDOWN_SECONDS", 15.0),
                    bbox_overlap_threshold=self.config.get("BBOX_OVERLAP_THRESHOLD", 0.6),
                )
            
            except Exception as e:
                raise RuntimeError(f"Failed to initialize security system: {e}")
            
            # 3. Initialize SecurityEventManager
            try:
                self.event_manager = SecurityEventManager(
                    security_system=self.security_system,
                    event_folder=self.config.get("STORAGE_PATH", "recordings"),
                    check_interval=self.config.get("CHECK_INTERVAL", 5.0),
                    retention_hours=self.config.get("RETENTION_HOURS", 24),
                    low_space_threshold_gb=self.config.get("LOW_SPACE_THRESHOLD_GB", 1.0),
                    aggressive_cleanup_hours=self.config.get("AGGRESSIVE_CLEANUP_HOURS", 12)
                )

            except Exception as e:
                raise RuntimeError(f"Failed to initialize event manager: {e}")
            
            # 4. Initialize Bot with professional configuration
            try:
                bot_config = BotConfig(
                    gif_for_motion=self.config.get("GIF_FOR_MOTION", False),
                    gif_for_person=self.config.get("GIF_FOR_PERSON", True),
                    gif_duration=self.config.get("GIF_DURATION", 3),
                    gif_fps=self.config.get("NOTIFICATION_GIF_FPS", 10),
                    max_file_size_mb=self.config.get("MAX_FILE_SIZE_MB", 50.0),
                    notification_rate_limit=self.config.get("NOTIFICATION_RATE_LIMIT", 5),
                    user_whitelist=self.config.get("USER_WHITELIST", []),
                    superuser_id=self.config.get("SUPERUSER_ID"),
                    setup_password=self.config.get("SETUP_PASSWORD"),
                    notification_chat_id=self.config.get("NOTIFICATION_CHAT_ID"),
                    allow_group_commands=self.config.get("ALLOW_GROUP_COMMANDS", True),
                    silent_unauthorized=self.config.get("SILENT_UNAUTHORIZED", True)
                )

                self.bot = SecurityTelegramBot(
                    token=self.config["TELEGRAM_TOKEN"],
                    event_manager=self.event_manager,
                    chat_id=self.config.get("CHAT_ID"),
                    config=bot_config
                )

            except Exception as e:
                raise RuntimeError(f"Failed to initialize bot: {e}")
        
            self.logger.info("All components initialized successfully")
        
        except Exception as e:
            self.logger.error(f"Component initialization failed: {e}")
            raise
    
    def run(self) -> None:
        """
        Run the complete security system.
        
        This method starts all components and runs until interrupted.
        """
        try:
            self.logger.info("=" * 50)
            self.logger.info("Starting AI-Powered Security System")
            self.logger.info("=" * 50)
            
            # Initialize all components
            self._initialize_components()
            
            # Log system information
            self._log_system_info()
            
            # Start the event manager (this will start the security system)
            if not self.event_manager.start():
                raise RuntimeError("Failed to start security event manager")
            
            self.logger.info("Security monitoring started successfully")
            
            # Start the Telegram bot (this will block until interrupted)
            self.logger.info("Starting Telegram bot interface...")
            self.bot.run()  # This blocks until Ctrl+C or error
            
        except KeyboardInterrupt:
            self.logger.info("Shutdown requested by user (Ctrl+C)")
        except Exception as e:
            self.logger.error(f"System error: {e}", exc_info=True)
            raise
        finally:
            self._shutdown()
    
    def _log_system_info(self) -> None:
        """Log important system information."""
        self.logger.info("System Configuration:")
        self.logger.info(f"  • Camera Source: {self.capture.source}")
        self.logger.info(f"  • Detection Interval: {self.security_system.interval}s")
        self.logger.info(f"  • Recording Frames: {self.security_system.record_frames}")
        self.logger.info(f"  • Storage Path: {self.event_manager.event_folder}")
        self.logger.info(f"  • Retention: {self.event_manager.retention_hours}h")
        self.logger.info(f"  • GPU Available: {self.security_system.status['gpu_available']}")
        self.logger.info(f"  • YOLO Batch Size: {self.security_system.batch_size}")
    
    def _shutdown(self) -> None:
        """Gracefully shutdown all system components."""
        self.logger.info("Shutting down security system...")
        
        shutdown_timeout = 10.0

        # Stop components in reverse order
        if self.bot:
            try:
                # Note: Bot shutdown is handled by its own signal handling
                self.logger.info("Telegram bot stopped")
            except Exception as e:
                self.logger.error(f"Error stopping bot: {e}")
        
        if self.event_manager:
            try:
                self.event_manager.stop(timeout=shutdown_timeout)
                self.logger.info("Event manager stopped")
            except Exception as e:
                self.logger.error(f"Error stopping event manager: {e}")
        
        if self.security_system:
            try:
                self.security_system.stop()
                self.logger.info("Security system stopped")
            except Exception as e:
                self.logger.error(f"Error stopping security system: {e}")
        
        if self.capture:
            try:
                self.capture.disconnect()
                self.logger.info("Camera capture stopped")
            except Exception as e:
                self.logger.error(f"Error stopping capture: {e}")
        
        self.logger.info("Security system shutdown complete")


def main() -> None:
    """Main entry point for the security system."""
    try:
        # Create and run the security system
        if len(sys.argv) > 2:
            print("Usage: python run.py [config_dir]", file=sys.stderr)
            sys.exit(1)

        config_dir = sys.argv[1] if len(sys.argv) == 2 else "config"

        runner = SecuritySystemRunner(config_dir)
        runner.run()
        
    except KeyboardInterrupt:
        print("Interrupted by user", file=sys.stderr)
        sys.exit(0)
    except (FileNotFoundError, ValueError) as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)


# ==============================
# ENTRY POINT
# ==============================
if __name__ == "__main__":
    main()