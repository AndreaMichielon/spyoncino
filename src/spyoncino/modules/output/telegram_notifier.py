"""
Telegram notifier module that can deliver snapshots, GIFs, and clips.

The notifier subscribes to the configured event topics and forwards artifacts to Telegram
using python-telegram-bot by default.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Protocol

from ...core.bus import Subscription
from ...core.contracts import BaseModule, MediaArtifact, ModuleConfig, SnapshotArtifact

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

    async def send_animation(
        self,
        *,
        chat_id: int | str,
        file_path: Path,
        caption: str | None = None,
    ) -> None: ...

    async def send_video(
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
        await self._send(chat_id, file_path, caption, method="photo")

    async def send_animation(
        self,
        *,
        chat_id: int | str,
        file_path: Path,
        caption: str | None = None,
    ) -> None:
        await self._send(chat_id, file_path, caption, method="animation")

    async def send_video(
        self,
        *,
        chat_id: int | str,
        file_path: Path,
        caption: str | None = None,
    ) -> None:
        await self._send(chat_id, file_path, caption, method="video")

    async def _send(
        self,
        chat_id: int | str,
        file_path: Path,
        caption: str | None,
        *,
        method: str,
    ) -> None:
        try:
            data = await asyncio.to_thread(file_path.read_bytes)
        except OSError as exc:
            raise TelegramSendError(f"Failed to read artifact {file_path}") from exc

        input_file = self._InputFile(data, filename=file_path.name)

        try:
            if method == "photo":
                await self._bot.send_photo(chat_id=chat_id, photo=input_file, caption=caption)
            elif method == "animation":
                await self._bot.send_animation(
                    chat_id=chat_id, animation=input_file, caption=caption
                )
            elif method == "video":
                await self._bot.send_video(chat_id=chat_id, video=input_file, caption=caption)
            else:  # pragma: no cover - defensive branch
                raise TelegramSendError(f"Unsupported Telegram method {method}")
        except self._telegram_error as exc:  # pragma: no cover - network errors
            raise TelegramSendError(str(exc)) from exc


class TelegramNotifier(BaseModule):
    """Output module that posts artifacts to Telegram chats."""

    name = "modules.output.telegram_notifier"

    def __init__(self, *, sender: TelegramSender | None = None) -> None:
        super().__init__()
        self._sender = sender
        self._subscriptions: list[Subscription] = []
        self._topics: dict[str, str | None] = {
            "snapshot": "event.snapshot.ready",
            "gif": "event.gif.ready",
            "clip": "event.clip.ready",
        }
        self._chat_targets: dict[str, int | str | None] = {
            "snapshot": None,
            "gif": None,
            "clip": None,
        }
        self._token: str | None = None
        self._read_timeout = 30.0
        self._write_timeout = 60.0
        self._caption_templates = {
            "snapshot": (
                "Motion detected on {camera_id} "
                "(detector={detector_id}, confidence={confidence:.2f})"
            ),
            "gif": "🚨 Person detected on {camera_id}",
            "clip": "🎥 Clip recorded on {camera_id}",
        }

    async def configure(self, config: ModuleConfig) -> None:
        await super().configure(config)
        options = config.options
        self._topics["snapshot"] = options.get("topic", self._topics["snapshot"])
        self._topics["gif"] = options.get("gif_topic", self._topics["gif"])
        self._topics["clip"] = options.get("clip_topic", self._topics["clip"])
        chat_id = options.get("chat_id")
        if chat_id is not None:
            self._chat_targets["snapshot"] = chat_id
        self._chat_targets["gif"] = options.get("gif_chat_id", self._chat_targets["gif"] or chat_id)
        self._chat_targets["clip"] = options.get(
            "clip_chat_id", self._chat_targets["clip"] or chat_id
        )
        self._token = options.get("token", self._token)
        self._read_timeout = float(options.get("read_timeout", self._read_timeout))
        self._write_timeout = float(options.get("write_timeout", self._write_timeout))
        if "message_template" in options:
            self._caption_templates["snapshot"] = options["message_template"]
        if "gif_message_template" in options:
            self._caption_templates["gif"] = options["gif_message_template"]
        if "clip_message_template" in options:
            self._caption_templates["clip"] = options["clip_message_template"]

    async def start(self) -> None:
        if self._sender is None and self._token:
            self._sender = BotTelegramSender(
                self._token,
                read_timeout=self._read_timeout,
                write_timeout=self._write_timeout,
            )
        if self._sender is None:
            logger.warning(
                "TelegramNotifier has no sender configured; notifications will be dropped."
            )
        for kind, topic in self._topics.items():
            if not topic:
                continue

            async def handler(event_topic: str, payload: Any, *, _kind=kind) -> None:
                await self._handle_payload(_kind, event_topic, payload)

            self._subscriptions.append(self.bus.subscribe(topic, handler))
        logger.info(
            "TelegramNotifier listening on %s",
            {k: v for k, v in self._topics.items() if v},
        )

    async def stop(self) -> None:
        for subscription in self._subscriptions:
            self.bus.unsubscribe(subscription)
        self._subscriptions.clear()

    async def _handle_payload(self, kind: str, topic: str, payload: Any) -> None:
        if kind == "clip":
            if isinstance(payload, MediaArtifact):
                await self._deliver_clip(payload)
            return
        if isinstance(payload, SnapshotArtifact):
            await self._deliver_snapshot(payload, kind)

    async def _deliver_snapshot(self, artifact: SnapshotArtifact, kind: str) -> None:
        sender = self._sender
        chat_id = self._chat_targets.get(kind) or self._chat_targets.get("snapshot")
        if sender is None or chat_id is None:
            logger.debug("Skipping %s notification because sender/chat is unavailable.", kind)
            return
        file_path = Path(artifact.artifact_path)
        if not file_path.exists():
            logger.warning("Artifact path %s does not exist; skipping.", file_path)
            return
        caption = self._render_caption(kind, artifact.metadata, artifact.camera_id)
        try:
            if kind == "gif" or "gif" in (artifact.content_type or ""):
                await sender.send_animation(chat_id=chat_id, file_path=file_path, caption=caption)
            else:
                await sender.send_photo(chat_id=chat_id, file_path=file_path, caption=caption)
            logger.info("TelegramNotifier delivered %s %s", kind, file_path.name)
        except TelegramSendError as exc:
            logger.error("Telegram %s notification failed: %s", kind, exc)

    async def _deliver_clip(self, artifact: MediaArtifact) -> None:
        sender = self._sender
        chat_id = self._chat_targets.get("clip") or self._chat_targets.get("snapshot")
        if sender is None or chat_id is None:
            logger.debug("Skipping clip notification because sender/chat is unavailable.")
            return
        file_path = Path(artifact.artifact_path)
        if not file_path.exists():
            logger.warning("Clip path %s does not exist; skipping.", file_path)
            return
        caption = self._render_caption("clip", artifact.metadata, artifact.camera_id)
        try:
            await sender.send_video(chat_id=chat_id, file_path=file_path, caption=caption)
            logger.info("TelegramNotifier delivered clip %s", file_path.name)
        except TelegramSendError as exc:
            logger.error("Telegram clip notification failed: %s", exc)

    def _render_caption(self, kind: str, metadata: dict[str, Any], camera_id: str) -> str:
        detection = metadata.get("detection") or {}
        template = self._caption_templates.get(kind) or self._caption_templates["snapshot"]
        template_vars = {
            "camera_id": camera_id,
            "detector_id": detection.get("detector_id", "unknown"),
            "confidence": detection.get("confidence", 0.0),
            "timestamp": detection.get("timestamp_utc", ""),
        }
        try:
            return template.format(**template_vars)
        except KeyError:
            return f"Event detected on {camera_id}"


__all__ = [
    "BotTelegramSender",
    "TelegramNotifier",
    "TelegramSendError",
    "TelegramSender",
]
