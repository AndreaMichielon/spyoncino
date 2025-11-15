#!/usr/bin/env python3
"""
Professional Security System Main Runner

This script initializes and runs the complete AI-powered security system
with motion detection, person recognition, and Telegram notifications.
"""

import logging
import os
import sys
from pathlib import Path
from typing import Any

import yaml

from spyoncino.bot import BotConfig, SecurityTelegramBot  # Updated from SecurityBot

# Import our improved professional classes
from spyoncino.capture import Capture
from spyoncino.manager import SecurityEventManager  # Updated from SecurityManager
from spyoncino.security import SecuritySystem  # Updated from Security


class SafeFormatter(logging.Formatter):
    """Custom formatter that handles Unicode encoding issues."""

    def format(self, record):
        try:
            formatted = super().format(record)
            return formatted.encode("utf-8", errors="replace").decode("utf-8")
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
            config_dir: Path to configuration directory containing YAML config files

        Raises:
            ValueError: If config_dir is empty or invalid
        """
        if not config_dir or not config_dir.strip():
            raise ValueError("config_dir cannot be empty")

        # Resolve paths relative to project root (two levels above src/)
        project_root = Path(__file__).resolve().parent.parent.parent.parent
        self.config_dir = project_root / config_dir
        self.config_path = self.config_dir / "config.yaml"
        self.telegram_path = self.config_dir / "telegram.yaml"
        self.secrets_path = self.config_dir / "secrets.yaml"
        self.config = self._load_configuration()

        # Components (initialized later)
        self.capture = None
        self.security_system = None
        self.event_manager = None
        self.bot = None

        # Setup logging
        self._setup_logging()
        self.logger = logging.getLogger(self.__class__.__name__)

    def _load_configuration(self) -> dict[str, Any]:
        """Load configuration from YAML files: config.yaml, telegram.yaml, and optional secrets.yaml."""
        config = {}

        # Load main configuration (required)
        if not self.config_path.exists():
            raise FileNotFoundError(
                f"Configuration file not found: {self.config_path}\n"
                f"Please create config/config.yaml based on the example or copy from the template."
            )

        try:
            with open(self.config_path, encoding="utf-8") as f:
                main_config = yaml.safe_load(f) or {}
                config.update(main_config)
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML in config file: {e}")
        except OSError as e:
            raise RuntimeError(f"Cannot read config file: {e}")

        # Load telegram configuration (optional - now typically embedded in config.yaml)
        if self.telegram_path.exists():
            try:
                with open(self.telegram_path, encoding="utf-8") as f:
                    telegram_config = yaml.safe_load(f) or {}
                    config.update(telegram_config)
            except yaml.YAMLError as e:
                raise ValueError(f"Invalid YAML in telegram config file: {e}")
            except OSError as e:
                raise RuntimeError(f"Cannot read telegram config file: {e}")
        else:
            logging.getLogger(__name__).debug(
                "config/telegram.yaml not found; assuming Telegram settings reside in config.yaml."
            )

        # Load secrets file (optional - fallback to env vars)
        if self.secrets_path.exists():
            try:
                with open(self.secrets_path, encoding="utf-8") as f:
                    secrets = yaml.safe_load(f) or {}
                    config.update(secrets)
            except yaml.YAMLError as e:
                raise ValueError(f"Invalid YAML in secrets file: {e}")
            except OSError as e:
                raise RuntimeError(f"Cannot read secrets file: {e}")
        else:
            # Fall back to environment variables
            config.setdefault("telegram", {})
            config.setdefault("authentication", {})
            config["telegram"]["token"] = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            config["telegram"]["chat_id"] = os.environ.get("TELEGRAM_CHAT_ID")
            config["authentication"]["setup_password"] = os.environ.get("SECURITY_SETUP_PASSWORD")

        # Flatten nested config for backward compatibility with existing code
        flattened = {}

        # Map YAML structure to flat config keys
        if "camera" in config:
            flattened["USB_PORT"] = config["camera"].get("usb_port", 0)
            flattened["CAMERA_WIDTH"] = config["camera"].get("width", 1280)
            flattened["CAMERA_HEIGHT"] = config["camera"].get("height", 720)
            flattened["CAMERA_FPS"] = config["camera"].get("fps", 15)
            flattened["CAMERA_BRIGHTNESS"] = config["camera"].get("brightness")
            flattened["CAMERA_CONTRAST"] = config["camera"].get("contrast")

        if "detection" in config:
            flattened["INTERVAL"] = config["detection"].get("interval", 2.0)
            flattened["RECORD_FRAMES"] = config["detection"].get("record_frames", 50)
            flattened["DETECTION_CONFIDENCE"] = config["detection"].get("confidence", 0.25)
            flattened["MAX_BATCH_SIZE"] = config["detection"].get("max_batch_size", 32)
            flattened["MOTION_THRESHOLD"] = config["detection"].get("motion_threshold", 5)
            flattened["PERSON_COOLDOWN_SECONDS"] = config["detection"].get(
                "person_cooldown_seconds", 30.0
            )
            flattened["BBOX_OVERLAP_THRESHOLD"] = config["detection"].get(
                "bbox_overlap_threshold", 0.6
            )

        if "storage" in config:
            flattened["STORAGE_PATH"] = config["storage"].get("path", "recordings")
            flattened["RETENTION_HOURS"] = config["storage"].get("retention_hours", 24)
            flattened["LOW_SPACE_THRESHOLD_GB"] = config["storage"].get(
                "low_space_threshold_gb", 1.0
            )
            flattened["AGGRESSIVE_CLEANUP_HOURS"] = config["storage"].get(
                "aggressive_cleanup_hours", 12
            )

        if "notifications" in config:
            flattened["GIF_FOR_MOTION"] = config["notifications"].get("gif_for_motion", False)
            flattened["GIF_FOR_PERSON"] = config["notifications"].get("gif_for_person", True)
            flattened["GIF_DURATION"] = config["notifications"].get("gif_duration", 3)
            flattened["GIF_FPS"] = config["notifications"].get("gif_fps", 15)
            flattened["NOTIFICATION_GIF_FPS"] = config["notifications"].get(
                "notification_gif_fps", 10
            )
            flattened["MAX_FILE_SIZE_MB"] = config["notifications"].get("max_file_size_mb", 50.0)
            flattened["NOTIFICATION_RATE_LIMIT"] = config["notifications"].get(
                "notification_rate_limit", 5
            )
            flattened["MAX_GIF_FRAMES"] = config["notifications"].get("max_gif_frames", 20)

        if "system" in config:
            flattened["CHECK_INTERVAL"] = config["system"].get("check_interval", 5.0)
            flattened["LOG_LEVEL"] = config["system"].get("log_level", "INFO")
            flattened["LOG_FORMAT"] = config["system"].get(
                "log_format", "%(asctime)s [%(levelname)8s] %(name)s: %(message)s"
            )
            flattened["LOG_MAX_SIZE_MB"] = config["system"].get("log_max_size_mb", 10)
            flattened["LOG_BACKUP_COUNT"] = config["system"].get("log_backup_count", 3)

        if "advanced" in config:
            flattened["YOLO_WARMUP_SIZE"] = config["advanced"].get("yolo_warmup_size", 224)
            flattened["GPU_MEMORY_RESERVE"] = config["advanced"].get("gpu_memory_reserve", 0.2)
            flattened["GIF_MAX_DIMENSION"] = config["advanced"].get("gif_max_dimension", 640)
            flattened["GIF_WORKER_THREADS"] = config["advanced"].get("gif_worker_threads", 2)
            flattened["BG_DETECT_SHADOWS"] = config["advanced"].get("bg_detect_shadows", True)
            flattened["TELEGRAM_READ_TIMEOUT"] = config["advanced"].get("telegram_read_timeout", 30)
            flattened["TELEGRAM_WRITE_TIMEOUT"] = config["advanced"].get(
                "telegram_write_timeout", 60
            )
            flattened["ANALYTICS_FIGURE_WIDTH"] = config["advanced"].get(
                "analytics_figure_width", 22
            )
            flattened["ANALYTICS_FIGURE_HEIGHT"] = config["advanced"].get(
                "analytics_figure_height", 5.5
            )
            flattened["ANALYTICS_INTERVALS"] = config["advanced"].get(
                "analytics_intervals", [5, 15, 60, 120]
            )
            flattened["UI_SCALE_BASE"] = config["advanced"].get("ui_scale_base", 320)

        if "security" in config:
            flattened["NOTIFICATION_CHAT_ID"] = config["security"].get("notification_chat_id")
            flattened["ALLOW_GROUP_COMMANDS"] = config["security"].get("allow_group_commands", True)
            flattened["SILENT_UNAUTHORIZED"] = config["security"].get("silent_unauthorized", True)

        if "telegram" in config:
            flattened["TELEGRAM_TOKEN"] = config["telegram"].get("token", "")
            flattened["CHAT_ID"] = config["telegram"].get("chat_id")

        if "authentication" in config:
            flattened["SETUP_PASSWORD"] = config["authentication"].get("setup_password")
            flattened["SUPERUSER_ID"] = config["authentication"].get("superuser_id")
            flattened["USER_WHITELIST"] = config["authentication"].get("user_whitelist", [])

        # Convert CHAT_ID to int if provided
        if flattened.get("CHAT_ID"):
            try:
                flattened["CHAT_ID"] = int(flattened["CHAT_ID"])
            except (ValueError, TypeError):
                if hasattr(self, "logger"):
                    self.logger.warning("Invalid CHAT_ID format, ignoring")
                flattened["CHAT_ID"] = None

        # Validate required settings
        if not flattened.get("TELEGRAM_TOKEN"):
            raise ValueError(
                "TELEGRAM_TOKEN is required (add to config/secrets.yaml or set TELEGRAM_BOT_TOKEN env var)"
            )

        return flattened

    def _setup_logging(self) -> None:
        """Setup comprehensive logging configuration with Unicode safety."""
        import logging.handlers

        try:
            log_level = getattr(logging, self.config.get("LOG_LEVEL", "INFO").upper())
            log_format = self.config.get(
                "LOG_FORMAT", "%(asctime)s [%(levelname)8s] %(name)s: %(message)s"
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
                encoding="utf-8",  # Explicit UTF-8 for file
            )
            file_handler.setFormatter(safe_formatter)

            # Console handler with safe encoding
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setFormatter(safe_formatter)

            # Configure root logger
            logging.basicConfig(level=log_level, handlers=[console_handler, file_handler])

            # Set specific log levels for noisy libraries
            logging.getLogger("ultralytics").setLevel(logging.WARNING)
            logging.getLogger("telegram").setLevel(logging.WARNING)
            logging.getLogger("apscheduler").setLevel(logging.WARNING)
            logging.getLogger("httpx").setLevel(logging.WARNING)
            logging.getLogger("httpcore").setLevel(logging.WARNING)
        except (OSError, PermissionError) as e:
            print(f"Failed to setup logging: {e}", file=sys.stderr)
            raise

    def _initialize_components(self) -> None:
        """Initialize all system components with configuration."""
        self.logger.info("Initializing components...")

        try:
            if not self.config.get("TELEGRAM_TOKEN"):
                raise ValueError(
                    "TELEGRAM_TOKEN is required (add to config/secrets.yaml or set TELEGRAM_BOT_TOKEN env var)"
                )
            usb_port = self.config.get("USB_PORT", 0)
            if isinstance(usb_port, int) and usb_port < 0:
                raise ValueError("USB_PORT must be non-negative")

            # 1. Initialize Capture with advanced options
            capture_config = {
                "width": self.config.get("CAMERA_WIDTH", 1280),
                "height": self.config.get("CAMERA_HEIGHT", 720),
                "fps": self.config.get("CAMERA_FPS", 30),
                "brightness": self.config.get("CAMERA_BRIGHTNESS"),
                "contrast": self.config.get("CAMERA_CONTRAST"),
            }

            for key, value in capture_config.items():
                if value is not None and not isinstance(value, int | float):
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
                    confidence=max(0.1, min(1.0, self.config.get("DETECTION_CONFIDENCE", 0.25))),
                    max_batch_size=self.config.get("MAX_BATCH_SIZE", 32),
                    motion_threshold=self.config.get("MOTION_THRESHOLD", 5),
                    gif_fps=self.config.get("GIF_FPS", 15),
                    max_gif_frames=self.config.get("MAX_GIF_FRAMES", 20),
                    person_cooldown_seconds=self.config.get("PERSON_COOLDOWN_SECONDS", 15.0),
                    bbox_overlap_threshold=self.config.get("BBOX_OVERLAP_THRESHOLD", 0.6),
                    # Advanced settings
                    yolo_warmup_size=self.config.get("YOLO_WARMUP_SIZE", 224),
                    gpu_memory_reserve=self.config.get("GPU_MEMORY_RESERVE", 0.2),
                    gif_max_dimension=self.config.get("GIF_MAX_DIMENSION", 640),
                    gif_worker_threads=self.config.get("GIF_WORKER_THREADS", 2),
                    bg_detect_shadows=self.config.get("BG_DETECT_SHADOWS", True),
                    ui_scale_base=self.config.get("UI_SCALE_BASE", 320),
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
                    aggressive_cleanup_hours=self.config.get("AGGRESSIVE_CLEANUP_HOURS", 12),
                    analytics_figure_width=self.config.get("ANALYTICS_FIGURE_WIDTH", 22),
                    analytics_figure_height=self.config.get("ANALYTICS_FIGURE_HEIGHT", 5.5),
                    analytics_intervals=self.config.get("ANALYTICS_INTERVALS", [5, 15, 60, 120]),
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
                    silent_unauthorized=self.config.get("SILENT_UNAUTHORIZED", True),
                    read_timeout=self.config.get("TELEGRAM_READ_TIMEOUT", 30),
                    write_timeout=self.config.get("TELEGRAM_WRITE_TIMEOUT", 60),
                )

                self.bot = SecurityTelegramBot(
                    token=self.config["TELEGRAM_TOKEN"],
                    event_manager=self.event_manager,
                    chat_id=self.config.get("CHAT_ID"),
                    config=bot_config,
                )

            except Exception as e:
                raise RuntimeError(f"Failed to initialize bot: {e}")

            # Log initialization summary with GPU diagnostics
            import torch

            gpu_available = torch.cuda.is_available()
            if gpu_available:
                gpu_name = torch.cuda.get_device_name(0)
                gpu_status = f"GPU: {gpu_name}"
            else:
                gpu_status = "CPU-only"
                # Log warning if CUDA not detected
                if hasattr(torch.version, "cuda") and torch.version.cuda:
                    self.logger.warning(
                        f"CUDA not detected! PyTorch built with CUDA {torch.version.cuda} but no GPU found"
                    )
                else:
                    self.logger.warning("PyTorch installed without CUDA support - using CPU only")

            self.logger.info(
                f"✓ All components ready [{gpu_status}, Batch: {self.security_system.batch_size}]"
            )

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

            # Print configuration preview
            self._print_configuration_preview()

            # Initialize all components
            self._initialize_components()

            # Start the event manager (this will start the security system)
            if not self.event_manager.start():
                raise RuntimeError("Failed to start security event manager")

            # Log system ready status
            self._log_system_ready()

            # Start the Telegram bot (this will block until interrupted)
            self.logger.info("✓ Telegram bot starting...")
            self.bot.run()  # This blocks until Ctrl+C or error

        except KeyboardInterrupt:
            self.logger.info("Shutdown requested by user (Ctrl+C)")
        except Exception as e:
            self.logger.error(f"System error: {e}", exc_info=True)
            raise
        finally:
            self._shutdown()

    def _print_configuration_preview(self) -> None:
        """Print current configuration with sensitive data masked."""
        print("\n" + "=" * 60)
        print("CURRENT CONFIGURATION")
        print("=" * 60)

        # Camera settings
        print("\n📹 CAMERA:")
        print(f"  • Source: {self.config.get('USB_PORT', 0)}")
        print(
            f"  • Resolution: {self.config.get('CAMERA_WIDTH')}x{self.config.get('CAMERA_HEIGHT')}"
        )
        print(f"  • FPS: {self.config.get('CAMERA_FPS')}")

        # Detection settings
        print("\n🔍 DETECTION:")
        print(f"  • Interval: {self.config.get('INTERVAL')}s")
        print(f"  • Confidence: {self.config.get('DETECTION_CONFIDENCE')}")
        print(f"  • Motion Threshold: {self.config.get('MOTION_THRESHOLD')}")
        print(f"  • Person Cooldown: {self.config.get('PERSON_COOLDOWN_SECONDS')}s")

        # Storage settings
        print("\n💾 STORAGE:")
        print(f"  • Path: {self.config.get('STORAGE_PATH')}")
        print(f"  • Retention: {self.config.get('RETENTION_HOURS')}h")
        print(f"  • Low Space Threshold: {self.config.get('LOW_SPACE_THRESHOLD_GB')}GB")

        # Notification settings
        print("\n🔔 NOTIFICATIONS:")
        print(f"  • GIF for Motion: {self.config.get('GIF_FOR_MOTION')}")
        print(f"  • GIF for Person: {self.config.get('GIF_FOR_PERSON')}")
        print(f"  • Rate Limit: {self.config.get('NOTIFICATION_RATE_LIMIT')}/min")

        # Security settings (masked)
        print("\n🔐 SECURITY:")
        token = self.config.get("TELEGRAM_TOKEN", "")
        masked_token = f"{token[:10]}...{token[-4:]}" if len(token) > 14 else "****"
        print(f"  • Telegram Token: {masked_token}")

        chat_id = self.config.get("CHAT_ID")
        print(f"  • Chat ID: {chat_id if chat_id else 'Auto-detect'}")

        password = self.config.get("SETUP_PASSWORD")
        print(f"  • Setup Password: {'****' if password else 'Not set'}")

        superuser = self.config.get("SUPERUSER_ID")
        print(f"  • Superuser ID: {superuser if superuser else 'Not configured'}")

        whitelist = self.config.get("USER_WHITELIST", [])
        print(f"  • Whitelisted Users: {len(whitelist)}")

        # System settings
        print("\n⚙️  SYSTEM:")
        print(f"  • Log Level: {self.config.get('LOG_LEVEL')}")
        print(f"  • Check Interval: {self.config.get('CHECK_INTERVAL')}s")

        # Advanced settings (optional, can be hidden)
        if self.config.get("LOG_LEVEL") == "DEBUG":  # Only show in debug mode
            print("\n🔧 ADVANCED:")
            print(f"  • YOLO Warmup Size: {self.config.get('YOLO_WARMUP_SIZE')}px")
            print(f"  • GPU Memory Reserve: {self.config.get('GPU_MEMORY_RESERVE')*100:.0f}%")
            print(f"  • GIF Max Dimension: {self.config.get('GIF_MAX_DIMENSION')}px")
            print(f"  • GIF Workers: {self.config.get('GIF_WORKER_THREADS')}")
            print(f"  • Background Shadows: {self.config.get('BG_DETECT_SHADOWS')}")
            print(
                f"  • Telegram Timeouts: {self.config.get('TELEGRAM_READ_TIMEOUT')}s/{self.config.get('TELEGRAM_WRITE_TIMEOUT')}s"
            )

        print("\n" + "=" * 60 + "\n")

    def _log_system_ready(self) -> None:
        """Log system ready status."""
        self.logger.info(f"✓ Camera connected: {self.capture.source}")
        self.logger.info(f"✓ Monitoring active (interval: {self.security_system.interval}s)")

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
