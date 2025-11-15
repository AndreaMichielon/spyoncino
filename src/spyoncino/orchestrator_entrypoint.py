"""
CLI entrypoint that boots the modular orchestrator stack described in the Week 5 plan.

The previous ``spyoncino.run`` script still proxies the legacy manager; this entrypoint
creates a production-like pipeline powered by the new core modules.  It wires the
event bus, loads Dynaconf configuration, enables config hot reload, and exposes the
control API plus Prometheus telemetry out of the box.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from pathlib import Path

from .core.config import ConfigError, ConfigService
from .core.orchestrator import Orchestrator
from .modules import (
    AnalyticsDbLogger,
    AnalyticsEventLogger,
    CameraSimulator,
    ClipBuilder,
    ControlApi,
    DetectionEventRouter,
    EventDeduplicator,
    GifBuilder,
    MotionDetector,
    PrometheusExporter,
    RateLimiter,
    ResilienceTester,
    RtspCamera,
    S3ArtifactUploader,
    SnapshotWriter,
    StorageRetention,
    TelegramControlBot,
    TelegramNotifier,
    UsbCamera,
    WebsocketGateway,
    YoloDetector,
    ZoningFilter,
)

AbstractModule = type[CameraSimulator]

LOGGER = logging.getLogger(__name__)


MODULE_REGISTRY: dict[str, AbstractModule] = {
    "modules.input.camera_simulator": CameraSimulator,
    "modules.input.usb_camera": UsbCamera,
    "modules.input.rtsp_camera": RtspCamera,
    "modules.process.motion_detector": MotionDetector,
    "modules.process.yolo_detector": YoloDetector,
    "modules.process.detection_event_router": DetectionEventRouter,
    "modules.event.deduplicator": EventDeduplicator,
    "modules.event.snapshot_writer": SnapshotWriter,
    "modules.event.gif_builder": GifBuilder,
    "modules.event.clip_builder": ClipBuilder,
    "modules.process.zoning_filter": ZoningFilter,
    "modules.output.rate_limiter": RateLimiter,
    "modules.output.telegram_notifier": TelegramNotifier,
    "modules.dashboard.control_api": ControlApi,
    "modules.dashboard.telegram_bot": TelegramControlBot,
    "modules.dashboard.websocket_gateway": WebsocketGateway,
    "modules.status.prometheus_exporter": PrometheusExporter,
    "modules.status.resilience_tester": ResilienceTester,
    "modules.storage.retention": StorageRetention,
    "modules.storage.s3_uploader": S3ArtifactUploader,
    "modules.analytics.event_logger": AnalyticsEventLogger,
    "modules.analytics.db_logger": AnalyticsDbLogger,
}


MODULE_ALIASES: dict[str, str] = {
    "sim": "modules.input.camera_simulator",
    "camera-sim": "modules.input.camera_simulator",
    "usb": "modules.input.usb_camera",
    "rtsp": "modules.input.rtsp_camera",
    "motion": "modules.process.motion_detector",
    "yolo": "modules.process.yolo_detector",
    "alert-router": "modules.process.detection_event_router",
    "person": "modules.process.detection_event_router",
    "dedupe": "modules.event.deduplicator",
    "snapshot": "modules.event.snapshot_writer",
    "gif": "modules.event.gif_builder",
    "clip": "modules.event.clip_builder",
    "zoning": "modules.process.zoning_filter",
    "rate-limit": "modules.output.rate_limiter",
    "telegram": "modules.output.telegram_notifier",
    "control-api": "modules.dashboard.control_api",
    "telegram-bot": "modules.dashboard.telegram_bot",
    "websocket": "modules.dashboard.websocket_gateway",
    "ws": "modules.dashboard.websocket_gateway",
    "prom": "modules.status.prometheus_exporter",
    "resilience": "modules.status.resilience_tester",
    "storage": "modules.storage.retention",
    "s3": "modules.storage.s3_uploader",
    "analytics": "modules.analytics.event_logger",
    "db-logger": "modules.analytics.db_logger",
}


PIPELINE_PRESETS: dict[str, list[str]] = {
    "sim": [
        "modules.input.camera_simulator",
        "modules.process.motion_detector",
        "modules.process.yolo_detector",
        "modules.process.detection_event_router",
        "modules.event.deduplicator",
        "modules.process.zoning_filter",
        "modules.event.snapshot_writer",
        "modules.event.gif_builder",
        "modules.event.clip_builder",
        "modules.output.rate_limiter",
        "modules.output.telegram_notifier",
        "modules.dashboard.control_api",
        "modules.dashboard.telegram_bot",
        "modules.dashboard.websocket_gateway",
        "modules.status.prometheus_exporter",
        "modules.status.resilience_tester",
        "modules.storage.retention",
        "modules.storage.s3_uploader",
        "modules.analytics.db_logger",
    ],
    "rtsp": [
        "modules.input.rtsp_camera",
        "modules.process.motion_detector",
        "modules.process.yolo_detector",
        "modules.process.detection_event_router",
        "modules.event.deduplicator",
        "modules.process.zoning_filter",
        "modules.event.snapshot_writer",
        "modules.event.gif_builder",
        "modules.event.clip_builder",
        "modules.output.rate_limiter",
        "modules.output.telegram_notifier",
        "modules.dashboard.control_api",
        "modules.dashboard.telegram_bot",
        "modules.dashboard.websocket_gateway",
        "modules.status.prometheus_exporter",
        "modules.status.resilience_tester",
        "modules.storage.retention",
        "modules.storage.s3_uploader",
        "modules.analytics.db_logger",
    ],
    "usb": [
        "modules.input.usb_camera",
        "modules.process.motion_detector",
        "modules.process.yolo_detector",
        "modules.process.detection_event_router",
        "modules.event.deduplicator",
        "modules.process.zoning_filter",
        "modules.event.snapshot_writer",
        "modules.event.gif_builder",
        "modules.event.clip_builder",
        "modules.output.rate_limiter",
        "modules.output.telegram_notifier",
        "modules.dashboard.control_api",
        "modules.dashboard.telegram_bot",
        "modules.dashboard.websocket_gateway",
        "modules.status.prometheus_exporter",
        "modules.status.resilience_tester",
        "modules.storage.retention",
        "modules.storage.s3_uploader",
        "modules.analytics.db_logger",
    ],
}


def resolve_module_name(label: str) -> str:
    """Return the fully qualified module identifier for CLI-friendly aliases."""

    normalised = label.strip().lower()
    return MODULE_ALIASES.get(normalised, label)


def build_module_sequence(
    preset: str, extra_modules: Sequence[str] | None, skip_modules: Iterable[str] | None
) -> list[str]:
    """
    Build the ordered list of module identifiers for the requested pipeline.

    The function keeps ordering stable, deduplicates entries, and honours the optional
    --module / --skip-module CLI flags.
    """

    try:
        baseline = list(PIPELINE_PRESETS[preset])
    except KeyError as exc:  # pragma: no cover - argparse guards choices
        raise ValueError(f"Unknown preset {preset}") from exc

    resolved_extras = [resolve_module_name(name) for name in (extra_modules or [])]
    resolved_skip = {resolve_module_name(name) for name in (skip_modules or [])}
    unique: OrderedDict[str, None] = OrderedDict()
    for name in [*baseline, *resolved_extras]:
        if name in resolved_skip:
            continue
        if name not in MODULE_REGISTRY:
            raise ValueError(f"Unknown module '{name}'. Available: {sorted(MODULE_REGISTRY)}")
        unique.setdefault(name, None)
    return list(unique.keys())


async def run_pipeline(
    *,
    config_dir: Path | None,
    module_names: Sequence[str],
    enable_hot_reload: bool = True,
) -> None:
    """Instantiate modules, enable hot reload, and run until interrupted."""

    config_service = ConfigService(config_dir=config_dir)
    orchestrator = Orchestrator()
    if enable_hot_reload:
        orchestrator.enable_config_hot_reload(config_service)
    orchestrator.enable_rollback_drills()

    added = 0
    for name in module_names:
        module_cls = MODULE_REGISTRY[name]
        try:
            module_configs = config_service.module_configs_for(name)
        except KeyError as exc:
            LOGGER.warning("Skipping %s: %s", name, exc)
            continue
        if not module_configs:
            LOGGER.info("No configuration produced for %s; skipping", name)
            continue
        for module_config in module_configs:
            module = module_cls()
            await orchestrator.add_module(module, module_config)
            added += 1
            camera_id = module_config.options.get("camera_id")
            suffix = f" (camera_id={camera_id})" if camera_id else ""
            LOGGER.info("Registered module %s%s", name, suffix)

    if added == 0:
        raise RuntimeError("No modules were registered; nothing to run.")

    snapshot = config_service.snapshot
    LOGGER.info(
        "Control API configured at http://%s:%s",
        snapshot.control_api.host,
        snapshot.control_api.port,
    )
    LOGGER.info("Prometheus metrics will be exposed on 127.0.0.1:9093")

    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)

    await orchestrator.start()
    LOGGER.info("Spyoncino modular stack running with %d modules. Press Ctrl+C to stop.", added)

    try:
        await stop_event.wait()
    finally:
        await orchestrator.stop()


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def _request_shutdown(sig_name: str) -> None:
        if not stop_event.is_set():
            LOGGER.info("Received %s – beginning graceful shutdown.", sig_name)
            stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_shutdown, sig.name)
        except NotImplementedError:  # Windows Proactor loop
            signal.signal(  # type: ignore[arg-type]
                sig,
                lambda signum, _frame, sig_name=sig.name: loop.call_soon_threadsafe(
                    _request_shutdown, sig_name or str(signum)
                ),
            )


def configure_logging(level: str) -> None:
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Spyoncino modular orchestrator runner.")
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        help="Directory that contains config.yaml/secrets.yaml (default: repo config/).",
    )
    parser.add_argument(
        "--preset",
        choices=sorted(PIPELINE_PRESETS.keys()),
        default="sim",
        help="Pipeline preset to load (controls which camera module is used).",
    )
    parser.add_argument(
        "--module",
        dest="extra_modules",
        action="append",
        default=[],
        metavar="MODULE",
        help="Additional module to append (use alias like 'clip' or full name).",
    )
    parser.add_argument(
        "--skip-module",
        dest="skip_modules",
        action="append",
        default=[],
        metavar="MODULE",
        help="Module to remove from the preset (alias or full name).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Python logging level (default: INFO).",
    )
    parser.add_argument(
        "--no-hot-reload",
        action="store_true",
        help="Disable config.update hot reload listener.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)
    try:
        module_names = build_module_sequence(args.preset, args.extra_modules, args.skip_modules)
        asyncio.run(
            run_pipeline(
                config_dir=args.config_dir,
                module_names=module_names,
                enable_hot_reload=not args.no_hot_reload,
            )
        )
    except KeyboardInterrupt:
        LOGGER.info("Interrupted by user.")
        return 0
    except ConfigError as exc:
        LOGGER.error("Configuration failed: %s", exc)
        return 2
    except Exception:  # pragma: no cover - surfaced to operator
        LOGGER.exception("Spyoncino orchestrator crashed.")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["build_module_sequence", "main", "run_pipeline"]
