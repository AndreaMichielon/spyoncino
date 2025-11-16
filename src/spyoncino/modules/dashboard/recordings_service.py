"""
Dashboard-oriented recordings service.

This module listens for dashboard control commands related to recordings and
produces structured results that can be consumed by dashboards such as the
Telegram control bot.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from ...core.bus import Subscription
from ...core.contracts import (
    BaseModule,
    ControlCommand,
    ModuleConfig,
    RecordingGetResult,
    RecordingListItem,
    RecordingsListResult,
)

logger = logging.getLogger(__name__)


class RecordingsService(BaseModule):
    """Expose filesystem-backed recordings via dashboard control commands."""

    name = "modules.dashboard.recordings_service"

    def __init__(self) -> None:
        super().__init__()
        self._events_root = Path("events")
        self._command_topic = "dashboard.control.command"
        self._list_result_topic = "dashboard.recordings.list.result"
        self._get_result_topic = "dashboard.recordings.get.result"
        self._subscription: Subscription | None = None
        self._default_limit = 20

    async def configure(self, config: ModuleConfig) -> None:
        await super().configure(config)
        options = config.options
        root = options.get("events_root")
        if root:
            self._events_root = Path(root)
        self._command_topic = options.get("command_topic", self._command_topic)
        self._list_result_topic = options.get("list_result_topic", self._list_result_topic)
        self._get_result_topic = options.get("get_result_topic", self._get_result_topic)
        self._default_limit = int(options.get("default_limit", self._default_limit))

    async def start(self) -> None:
        if self._subscription is not None:
            return
        self._subscription = self.bus.subscribe(self._command_topic, self._handle_command)
        logger.info(
            "RecordingsService listening for commands on %s (root=%s)",
            self._command_topic,
            self._events_root,
        )

    async def stop(self) -> None:
        if self._subscription:
            self.bus.unsubscribe(self._subscription)
            self._subscription = None

    async def _handle_command(self, topic: str, payload: Any) -> None:
        if not isinstance(payload, ControlCommand):
            return
        if payload.command == "recordings.list":
            await self._handle_list_command(payload)
        elif payload.command == "recordings.get":
            await self._handle_get_command(payload)

    async def _handle_list_command(self, cmd: ControlCommand) -> None:
        args = cmd.arguments or {}
        request_id = str(args.get("request_id") or f"auto-{int(datetime.utcnow().timestamp())}")
        limit = int(args.get("limit", self._default_limit))
        if limit <= 0:
            limit = self._default_limit

        items: list[RecordingListItem] = []
        if not self._events_root.exists():
            logger.debug("Events root %s does not exist; returning empty list.", self._events_root)
        else:
            recordings: list[tuple[datetime, Path]] = []
            for file_path in self._events_root.glob("*.gif"):
                ts = self._get_timestamp(file_path)
                recordings.append((ts, file_path))
            recordings.sort(key=lambda x: x[0], reverse=True)
            for idx, (ts, path) in enumerate(recordings[:limit]):
                stem = path.stem
                # Use stem as stable id so dashboards can request the same file later.
                item_id = stem
                label = self._format_label(stem=stem, index=idx, ts=ts)
                items.append(
                    RecordingListItem(
                        id=item_id,
                        label=label,
                        path=str(path),
                        event_name=stem,
                        camera_id=cmd.camera_id,
                        timestamp_utc=ts,
                    )
                )

        result = RecordingsListResult(request_id=request_id, camera_id=cmd.camera_id, items=items)
        await self.bus.publish(self._list_result_topic, result)

    async def _handle_get_command(self, cmd: ControlCommand) -> None:
        args = cmd.arguments or {}
        request_id = str(args.get("request_id") or f"get-{int(datetime.utcnow().timestamp())}")
        mode = args.get("mode") or ""

        # Mode: latest recording for a specific camera_id
        if mode == "latest_for_camera" and cmd.camera_id:
            if not self._events_root.exists():
                return
            candidates: list[tuple[datetime, Path]] = []
            for file_path in self._events_root.glob("*.gif"):
                stem = file_path.stem.lower()
                if str(cmd.camera_id).lower() in stem:
                    ts = self._get_timestamp(file_path)
                    candidates.append((ts, file_path))
            if not candidates:
                logger.debug(
                    "No recordings found matching camera_id=%s under %s",
                    cmd.camera_id,
                    self._events_root,
                )
                return
            candidates.sort(key=lambda x: x[0], reverse=True)
            _, best_path = candidates[0]
            key = best_path.stem
            result = RecordingGetResult(
                request_id=request_id,
                item_id=key,
                camera_id=str(cmd.camera_id),
                path=str(best_path),
                content_type="image/gif",
            )
            await self.bus.publish(self._get_result_topic, result)
            return

        # Default mode: interpret key/item_id as filename stem
        key = args.get("item_id") or args.get("key")
        if not isinstance(key, str) or not key:
            logger.debug("RecordingsService received get command without key/item_id.")
            return

        candidate = self._events_root / f"{key}.gif"
        if not candidate.exists():
            logger.debug("Recording %s not found under %s", key, self._events_root)
            return

        result = RecordingGetResult(
            request_id=request_id,
            item_id=key,
            camera_id=cmd.camera_id,
            path=str(candidate),
            content_type="image/gif",
        )
        await self.bus.publish(self._get_result_topic, result)

    @staticmethod
    def _get_timestamp(path: Path) -> datetime:
        """Parse timestamp from filename or fall back to mtime."""
        try:
            parts = path.stem.split("_")
            # Support both event_type_YYYYMMDD_HHMMSS.gif and
            # camera_eventtype_YYYYMMDD_HHMMSS.gif by always using the last
            # two segments as date/time when they look like digits.
            if len(parts) >= 3:
                date_part = parts[-2]
                time_part = parts[-1]
                timestamp_str = date_part + time_part
                return datetime.strptime(timestamp_str, "%Y%m%d%H%M%S")
        except Exception as exc:
            logger.debug("Failed to parse timestamp from %s: %s", path, exc)
        try:
            return datetime.fromtimestamp(path.stat().st_mtime)
        except Exception:
            return datetime.utcnow()

    @staticmethod
    def _format_label(*, stem: str, index: int, ts: datetime) -> str:
        """Create a compact, human friendly label for inline buttons."""
        try:
            time_str = ts.strftime("%H:%M")
        except Exception:
            time_str = "?"
        # Best-effort event type icon from filename
        lower = stem.lower()
        if "person" in lower:
            icon = "🚨"
        elif "motion" in lower:
            icon = "👀"
        else:
            icon = "📹"
        return f"{icon} {time_str}"


__all__ = ["RecordingsService"]
