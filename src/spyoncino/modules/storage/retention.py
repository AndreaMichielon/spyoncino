"""
Filesystem retention manager that mirrors the legacy SecurityEventManager cleanup loop.

The module periodically scans recording directories, enforces retention windows,
publishes `StorageStats` telemetry, and emits warnings when disk space drops below
the configured threshold.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...core.bus import Subscription
from ...core.contracts import (
    BaseModule,
    HealthStatus,
    ModuleConfig,
    StorageDiscrepancy,
    StorageStats,
    StorageSyncResult,
)

logger = logging.getLogger(__name__)


class StorageRetention(BaseModule):
    """Background task that enforces disk retention policies."""

    name = "modules.storage.retention"

    def __init__(self, *, clock: Callable[[], dt.datetime] | None = None) -> None:
        super().__init__()
        self._root = Path("recordings")
        self._retention_hours = 24.0
        self._aggressive_hours = 12.0
        self._low_space_threshold_gb = 1.0
        self._cleanup_interval = 600.0
        self._artifact_globs: list[str] = [
            "frames/**/*.png",
            "frames/**/*.jpg",
            "frames/**/*.jpeg",
            "snapshots/*.png",
            "snapshots/*.jpg",
            "gifs/*.gif",
            "clips/*.mp4",
        ]
        self._stats_topic = "storage.stats"
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._last_stats: StorageStats | None = None
        self._clock = clock or (lambda: dt.datetime.now(tz=dt.UTC))
        self._remote_tracking = False
        self._remote_topic = "storage.s3.synced"
        self._discrepancy_topic = "storage.discrepancy"
        self._remote_window_seconds = 3600.0
        self._remote_index: dict[str, dt.datetime] = {}
        self._remote_subscription: Subscription | None = None

    async def configure(self, config: ModuleConfig) -> None:
        await super().configure(config)
        options = config.options
        self._root = Path(options.get("root_dir", self._root))
        self._retention_hours = float(options.get("retention_hours", self._retention_hours))
        self._aggressive_hours = float(options.get("aggressive_hours", self._aggressive_hours))
        self._low_space_threshold_gb = float(
            options.get("low_space_threshold_gb", self._low_space_threshold_gb)
        )
        self._cleanup_interval = float(
            options.get("cleanup_interval_seconds", self._cleanup_interval)
        )
        globs = options.get("artifact_globs")
        if globs:
            self._artifact_globs = list(globs)
        self._stats_topic = options.get("stats_topic", self._stats_topic)
        self._remote_tracking = bool(options.get("remote_tracking", self._remote_tracking))
        self._remote_topic = options.get("remote_topic", self._remote_topic)
        self._discrepancy_topic = options.get("discrepancy_topic", self._discrepancy_topic)
        self._remote_window_seconds = float(
            options.get("remote_window_seconds", self._remote_window_seconds)
        )

    async def start(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="storage-retention")
        if self._remote_tracking:
            self._remote_subscription = self.bus.subscribe(
                self._remote_topic, self._record_remote_sync
            )
        logger.info("StorageRetention started; monitoring %s", self._root)

    async def stop(self) -> None:
        if self._remote_subscription:
            self.bus.unsubscribe(self._remote_subscription)
            self._remote_subscription = None
        if self._task is None:
            return
        self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
        logger.info("StorageRetention stopped.")

    async def health(self) -> HealthStatus:
        status = "healthy" if self._last_stats else "degraded"
        details: dict[str, Any] = {"root": str(self._root)}
        if self._last_stats:
            data = self._last_stats
            details.update(
                {
                    "free_gb": data.free_gb,
                    "used_gb": data.used_gb,
                    "warning": data.warning,
                }
            )
        return HealthStatus(status=status, details=details)

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                stats, local_paths = await asyncio.to_thread(self._run_cleanup_cycle)
                self._last_stats = stats
                await self.bus.publish(self._stats_topic, stats)
                if self._remote_tracking:
                    await self._publish_discrepancies(local_paths)
            except Exception:  # pragma: no cover - defensive logging
                logger.exception("Storage retention cycle failed.")
            await self._wait_with_cancel(self._cleanup_interval)

    async def _wait_with_cancel(self, interval: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
        except TimeoutError:
            return

    def _run_cleanup_cycle(self) -> tuple[StorageStats, set[str]]:
        self._root.mkdir(parents=True, exist_ok=True)
        disk = shutil.disk_usage(self._root)
        total_gb = disk.total / (1024**3)
        used_gb = (disk.total - disk.free) / (1024**3)
        free_gb = disk.free / (1024**3)
        usage_percent = (used_gb / total_gb * 100.0) if total_gb else 0.0
        aggressive = free_gb < self._low_space_threshold_gb
        retention_hours = self._aggressive_hours if aggressive else self._retention_hours
        cutoff = self._clock() - dt.timedelta(hours=retention_hours)

        files_deleted = 0
        artifacts_after: dict[str, int] = {}
        local_paths: set[str] = set()
        for pattern in self._artifact_globs:
            remaining = 0
            for path in self._root.glob(pattern):
                if not path.is_file():
                    continue
                if self._delete_if_expired(path, cutoff):
                    files_deleted += 1
                    continue
                remaining += 1
                relative = self._relative_path(path)
                if relative:
                    local_paths.add(relative)
            artifacts_after[pattern] = remaining

        warning = aggressive or free_gb < self._low_space_threshold_gb
        stats = StorageStats(
            root=str(self._root),
            total_gb=round(total_gb, 3),
            used_gb=round(used_gb, 3),
            free_gb=round(free_gb, 3),
            usage_percent=round(usage_percent, 2),
            files_deleted=files_deleted,
            aggressive=aggressive,
            warning=warning,
            artifacts=artifacts_after,
        )
        logger.debug("Storage retention stats: %s", stats.model_dump())
        return stats, local_paths

    def _delete_if_expired(self, path: Path, cutoff: dt.datetime) -> bool:
        try:
            mtime = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.UTC)
        except OSError:
            return False
        if mtime >= cutoff:
            return False
        try:
            path.unlink()
            logger.info("Pruned expired artifact %s", path)
            return True
        except OSError as exc:
            logger.warning("Failed to delete %s: %s", path, exc)
            return False

    async def _record_remote_sync(self, topic: str, payload: StorageSyncResult) -> None:
        if not isinstance(payload, StorageSyncResult):
            return
        relative = self._relative_path(Path(payload.artifact_path))
        if not relative:
            return
        self._remote_index[relative] = self._clock()

    async def _publish_discrepancies(self, local_paths: set[str]) -> None:
        now = self._clock()
        horizon = dt.timedelta(seconds=self._remote_window_seconds)
        self._remote_index = {
            path: timestamp
            for path, timestamp in self._remote_index.items()
            if now - timestamp <= horizon
        }
        remote_paths = set(self._remote_index.keys())
        missing_remote = sorted(local_paths - remote_paths)
        orphaned_remote = sorted(remote_paths - local_paths)
        if not missing_remote and not orphaned_remote:
            return
        payload = StorageDiscrepancy(
            root=str(self._root),
            missing_remote=missing_remote,
            orphaned_remote=orphaned_remote,
            total_local=len(local_paths),
            total_remote=len(remote_paths),
        )
        await self.bus.publish(self._discrepancy_topic, payload)

    def _relative_path(self, path: Path) -> str | None:
        try:
            rel = path.resolve().relative_to(self._root.resolve())
        except ValueError:
            return None
        return str(rel).replace("\\", "/")


__all__ = ["StorageRetention"]
