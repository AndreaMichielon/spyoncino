"""
Telegram notifier module that publishes snapshots to end users.

The module subscribes to `event.snapshot.ready`, loads the referenced file, and
invokes a pluggable sender (defaulting to python-telegram-bot) to deliver the
media.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Protocol

from ...core.bus import Subscription
from ...core.contracts import BaseModule, ModuleConfig, SnapshotArtifact

logger = logging.getLogger(__name__)


class TelegramSendError(RuntimeError):
    """Raised when sending a Telegram notification fails."""


class TelegramSender(Protocol):
    """Protocol implemented by concrete Telegram senders."""

    async def send_photo(
        self,
        *,
        chat_id: int | str,
        file_path: Path,
        caption: str | None = None,
    ) -> None: ...


class BotTelegramSender:
    """Adapter that uses python-telegram-bot to send media."""

    def __init__(self, token: str, *, read_timeout: float, write_timeout: float) -> None:
        if not token:
            raise TelegramSendError("Telegram token is required.")
        try:
            from telegram import Bot, InputFile
            from telegram.error import TelegramError
            from telegram.request import HTTPXRequest
        except ModuleNotFoundError as exc:  # pragma: no cover - import guard
            raise TelegramSendError("python-telegram-bot is not installed") from exc

        request = HTTPXRequest(read_timeout=read_timeout, write_timeout=write_timeout)
        self._bot = Bot(token=token, request=request)
        self._InputFile = InputFile
        self._telegram_error = TelegramError

    async def send_photo(
        self,
        *,
        chat_id: int | str,
        file_path: Path,
        caption: str | None = None,
    ) -> None:
        try:
            data = await asyncio.to_thread(file_path.read_bytes)
        except OSError as exc:
            raise TelegramSendError(f"Failed to read snapshot {file_path}") from exc

        input_file = self._InputFile(data, filename=file_path.name)

        try:
            await self._bot.send_photo(chat_id=chat_id, photo=input_file, caption=caption)
        except self._telegram_error as exc:  # pragma: no cover - network errors
            raise TelegramSendError(str(exc)) from exc


class TelegramNotifier(BaseModule):
    """Output module that posts snapshot artifacts to Telegram."""

    name = "modules.output.telegram_notifier"

    def __init__(self, *, sender: TelegramSender | None = None) -> None:
        super().__init__()
        self._sender = sender
        self._subscription: Subscription | None = None
        self._topic = "event.snapshot.ready"
        self._chat_id: int | str | None = None
        self._token: str | None = None
        self._read_timeout = 30.0
        self._write_timeout = 60.0
        self._send_typing_action = True
        self._message_template = (
            "Motion detected on {camera_id} "
            "(detector={detector_id}, confidence={confidence:.2f})"
        )

    async def configure(self, config: ModuleConfig) -> None:
        await super().configure(config)
        options = config.options
        self._topic = options.get("topic", self._topic)
        self._chat_id = options.get("chat_id", self._chat_id)
        self._token = options.get("token", self._token)
        self._read_timeout = float(options.get("read_timeout", self._read_timeout))
        self._write_timeout = float(options.get("write_timeout", self._write_timeout))
        self._send_typing_action = bool(options.get("send_typing_action", self._send_typing_action))
        self._message_template = options.get("message_template", self._message_template)

    async def start(self) -> None:
        if self._sender is None and self._token:
            self._sender = BotTelegramSender(
                self._token,
                read_timeout=self._read_timeout,
                write_timeout=self._write_timeout,
            )
        self._subscription = self.bus.subscribe(self._topic, self._handle_snapshot)
        if not self._chat_id:
            logger.warning(
                "TelegramNotifier has no chat_id configured; notifications will be dropped."
            )
        if self._sender is None:
            logger.warning(
                "TelegramNotifier has no sender configured; notifications will be dropped."
            )

    async def stop(self) -> None:
        if self._subscription:
            self.bus.unsubscribe(self._subscription)
            self._subscription = None

    async def _handle_snapshot(self, topic: str, payload: SnapshotArtifact) -> None:
        if not isinstance(payload, SnapshotArtifact):
            return
        if not self._chat_id:
            logger.debug("Skipping Telegram notification because chat_id is missing.")
            return
        if self._sender is None:
            logger.debug("Skipping Telegram notification because sender is unavailable.")
            return
        file_path = Path(payload.artifact_path)
        if not file_path.exists():
            logger.warning("Snapshot path %s does not exist; skipping.", file_path)
            return
        caption = self._render_caption(payload)
        try:
            await self._sender.send_photo(
                chat_id=self._chat_id,
                file_path=file_path,
                caption=caption,
            )
            logger.info("TelegramNotifier delivered snapshot %s", file_path.name)
        except TelegramSendError as exc:
            logger.error("Telegram notification failed: %s", exc)

    def _render_caption(self, artifact: SnapshotArtifact) -> str:
        detection = artifact.metadata.get("detection") or {}
        template_vars = {
            "camera_id": artifact.camera_id,
            "detector_id": detection.get("detector_id", "unknown"),
            "confidence": detection.get("confidence", 0.0),
            "timestamp": detection.get("timestamp_utc", ""),
        }
        try:
            return self._message_template.format(**template_vars)
        except KeyError:
            return f"Motion detected on {artifact.camera_id}"


__all__ = [
    "BotTelegramSender",
    "TelegramNotifier",
    "TelegramSendError",
    "TelegramSender",
]
