"""
Telegram notifier module that can deliver snapshots, GIFs, and clips.

The notifier subscribes to the configured event topics and forwards artifacts to Telegram
using python-telegram-bot by default.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol

import imageio.v3 as iio
import numpy as np

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

    async def send_message(
        self,
        *,
        chat_id: int | str,
        text: str,
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

    async def send_message(
        self,
        *,
        chat_id: int | str,
        text: str,
    ) -> None:
        try:
            result = self._bot.send_message(chat_id=chat_id, text=text)
            if inspect.isawaitable(result):
                await result
        except self._telegram_error as exc:  # pragma: no cover - network errors
            raise TelegramSendError(str(exc)) from exc

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
        self._chat_targets: dict[str, list[int | str]] = {
            "snapshot": [],
            "gif": [],
            "clip": [],
        }
        self._delivery_modes: dict[str, str] = {
            "snapshot": "photo",
            "gif": "animation",
            "clip": "video",
        }
        self._token: str | None = None
        self._read_timeout = 30.0
        self._write_timeout = 60.0
        self._inline_animation_limit_mb = 9.5
        self._transcode_large_gifs = False
        self._gif_notification_fps = 10
        self._caption_templates = {
            # Hardcoded legacy-style concise messages
            "snapshot": "👀 Motion detected on {camera_id}",
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
        default_targets = self._normalize_targets(options.get("chat_targets"))
        if not default_targets:
            default_targets = self._normalize_targets(chat_id)
        self._chat_targets["snapshot"] = default_targets
        gif_targets = self._normalize_targets(
            options.get("gif_chat_targets", options.get("gif_chat_id"))
        )
        if not gif_targets:
            gif_targets = list(default_targets)
        self._chat_targets["gif"] = gif_targets
        clip_targets = self._normalize_targets(
            options.get("clip_chat_targets", options.get("clip_chat_id"))
        )
        if not clip_targets:
            clip_targets = list(default_targets)
        self._chat_targets["clip"] = clip_targets
        self._token = options.get("token", self._token)
        self._read_timeout = float(options.get("read_timeout", self._read_timeout))
        self._write_timeout = float(options.get("write_timeout", self._write_timeout))
        if "message_template" in options:
            self._caption_templates["snapshot"] = options["message_template"]
        if "gif_message_template" in options:
            self._caption_templates["gif"] = options["gif_message_template"]
        if "clip_message_template" in options:
            self._caption_templates["clip"] = options["clip_message_template"]
        self._delivery_modes["snapshot"] = self._normalize_delivery(
            options.get("snapshot_delivery"), default=self._delivery_modes["snapshot"]
        )
        self._delivery_modes["gif"] = self._normalize_delivery(
            options.get("gif_delivery"), default=self._delivery_modes["gif"]
        )
        self._delivery_modes["clip"] = self._normalize_delivery(
            options.get("clip_delivery"), default=self._delivery_modes["clip"]
        )
        self._inline_animation_limit_mb = float(
            options.get("inline_animation_max_mb", self._inline_animation_limit_mb)
        )
        if "transcode_large_gifs" in options:
            self._transcode_large_gifs = bool(options.get("transcode_large_gifs"))
        self._gif_notification_fps = int(
            options.get("gif_notification_fps", self._gif_notification_fps)
        )

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
        mode = self._delivery_modes.get(kind, "")
        if mode in {"none", "disabled"}:
            logger.debug("Skipping %s notification because mode=%s", kind, mode)
            return
        if kind == "clip":
            if isinstance(payload, MediaArtifact):
                await self._deliver_clip(payload)
            return
        if isinstance(payload, SnapshotArtifact):
            await self._deliver_snapshot(payload, kind)

    async def _deliver_snapshot(self, artifact: SnapshotArtifact, kind: str) -> None:
        sender = self._sender
        recipients = self._recipients_for(kind)
        if sender is None or not recipients:
            logger.debug("Skipping %s notification because sender/chat is unavailable.", kind)
            return
        mode = (self._delivery_modes.get(kind, "photo") or "photo").lower()
        if mode == "text":
            await self._deliver_text(artifact, kind)
            return
        file_path = Path(artifact.artifact_path)
        if not file_path.exists():
            logger.warning("Artifact path %s does not exist; skipping.", file_path)
            return
        is_gif_artifact = (
            kind == "gif"
            or "gif" in (artifact.content_type or "").lower()
            or file_path.suffix.lower() == ".gif"
        )
        if is_gif_artifact and mode not in {"animation", "video", "text"}:
            mode = "animation"
        caption = self._compose_caption(kind, artifact.metadata, artifact.camera_id, mode)
        prepared_path = file_path
        cleanup_path: Path | None = None
        try:
            if is_gif_artifact:
                prepared_path, mode, cleanup_path = await self._prepare_gif_delivery(
                    file_path, mode
                )
                # Re-compose caption if delivery mode changed during preparation
                caption = self._compose_caption(kind, artifact.metadata, artifact.camera_id, mode)
            failures = 0
            for chat_id in recipients:
                try:
                    await self._send_media(
                        sender=sender,
                        mode=mode,
                        chat_id=chat_id,
                        file_path=prepared_path,
                        caption=caption,
                    )
                    logger.info(
                        "TelegramNotifier delivered %s %s to %s", kind, prepared_path.name, chat_id
                    )
                except TelegramSendError as exc:
                    failures += 1
                    logger.error("Telegram %s notification failed for %s: %s", kind, chat_id, exc)
            if failures and failures == len(recipients):
                logger.warning("All Telegram %s deliveries failed for %s", kind, prepared_path.name)
        finally:
            if cleanup_path is not None:
                await asyncio.to_thread(self._safe_unlink, cleanup_path)

    async def _deliver_text(self, artifact: SnapshotArtifact, kind: str) -> None:
        sender = self._sender
        recipients = self._recipients_for(kind)
        if sender is None or not recipients:
            logger.debug("Skipping %s text notification because sender/chat is unavailable.", kind)
            return
        caption = self._compose_caption(kind, artifact.metadata, artifact.camera_id, "text")
        failures = 0
        for chat_id in recipients:
            try:
                await sender.send_message(chat_id=chat_id, text=caption)
                logger.info("TelegramNotifier delivered text %s to %s", kind, chat_id)
            except TelegramSendError as exc:
                failures += 1
                logger.error("Telegram text notification failed for %s: %s", chat_id, exc)
        if failures and failures == len(recipients):
            logger.warning("All Telegram text deliveries failed for %s", artifact.camera_id)

    async def _deliver_clip(self, artifact: MediaArtifact) -> None:
        sender = self._sender
        recipients = self._recipients_for("clip")
        if sender is None or not recipients:
            logger.debug("Skipping clip notification because sender/chat is unavailable.")
            return
        file_path = Path(artifact.artifact_path)
        if not file_path.exists():
            logger.warning("Clip path %s does not exist; skipping.", file_path)
            return
        caption = self._compose_caption("clip", artifact.metadata, artifact.camera_id, "video")
        failures = 0
        for chat_id in recipients:
            try:
                await sender.send_video(chat_id=chat_id, file_path=file_path, caption=caption)
                logger.info("TelegramNotifier delivered clip %s to %s", file_path.name, chat_id)
            except TelegramSendError as exc:
                failures += 1
                logger.error("Telegram clip notification failed for %s: %s", chat_id, exc)
        if failures and failures == len(recipients):
            logger.warning("All Telegram clip deliveries failed for %s", file_path.name)

    async def _send_media(
        self,
        *,
        sender: TelegramSender,
        mode: str,
        chat_id: int | str,
        file_path: Path,
        caption: str | None,
    ) -> None:
        delivery_mode = (mode or "photo").lower()
        if delivery_mode == "video":
            await sender.send_video(chat_id=chat_id, file_path=file_path, caption=caption)
        elif delivery_mode == "animation":
            await sender.send_animation(chat_id=chat_id, file_path=file_path, caption=caption)
        else:
            await sender.send_photo(chat_id=chat_id, file_path=file_path, caption=caption)

    async def _prepare_gif_delivery(
        self, file_path: Path, mode: str
    ) -> tuple[Path, str, Path | None]:
        resolved_mode = (mode or "animation").lower()
        if file_path.suffix.lower() == ".mp4":
            return file_path, "video", None
        try:
            file_size = file_path.stat().st_size
        except OSError as exc:
            logger.warning("Unable to stat artifact %s: %s", file_path, exc)
            return file_path, resolved_mode or "animation", None

        limit_bytes = max(1, int(self._inline_animation_limit_mb * 1024 * 1024))
        needs_transcode = resolved_mode == "video"
        if not needs_transcode and self._transcode_large_gifs and file_size > limit_bytes:
            needs_transcode = True

        if not needs_transcode:
            return file_path, "animation", None

        try:
            temp_path = await asyncio.to_thread(self._transcode_gif_to_mp4, file_path)
        except Exception as exc:
            logger.warning("GIF transcode failed for %s: %s", file_path, exc)
            return file_path, "animation", None

        return temp_path, "video", temp_path

    def _transcode_gif_to_mp4(self, gif_path: Path) -> Path:
        fd, temp_name = tempfile.mkstemp(prefix="spyoncino_gif_", suffix=".mp4")
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            frames = iio.imread(gif_path, extension=".gif", index=None)
            frames_array = np.asarray(frames, dtype=np.uint8)
            if frames_array.ndim == 3:
                frames_array = np.expand_dims(frames_array, axis=0)
            if frames_array.ndim == 2:
                frames_array = np.expand_dims(frames_array, axis=0)
                frames_array = np.expand_dims(frames_array, axis=-1)
            if frames_array.shape[-1] == 1:
                frames_array = np.repeat(frames_array, 3, axis=-1)
            if frames_array.shape[-1] > 3:
                frames_array = frames_array[..., :3]
            iio.imwrite(
                temp_path,
                frames_array,
                extension=".mp4",
                fps=max(1, int(self._gif_notification_fps)),
                codec="libx264",
                macro_block_size=None,
            )
            return temp_path
        except Exception:
            with suppress(OSError):
                temp_path.unlink()
            raise

    @staticmethod
    def _safe_unlink(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.debug("Failed to cleanup temporary media %s: %s", path, exc)

    def _compose_caption(
        self,
        kind: str,
        metadata: dict[str, Any],
        camera_id: str,
        delivery_mode: str,
    ) -> str:
        """Hardcode legacy-style text with emoji chosen by delivery type.
        Event label is derived from the detection metadata, not attachment type.
        """
        label = self._event_label_from_metadata(metadata)
        mode = (delivery_mode or "").lower()
        emoji = {
            "text": "📝",
            "photo": "📸",
            "animation": "🎞️",
            "video": "🎥",
        }.get(mode, "")
        ts = self._extract_compact_timestamp(metadata)
        prefix = f"{emoji} " if emoji else ""
        return f"[{ts}] {prefix}{label} on {camera_id}"

    def _event_label_from_metadata(self, metadata: dict[str, Any]) -> str:
        """Infer Motion vs Detection from the upstream detection payload."""
        detection = metadata.get("detection") or {}
        detector_id = str(detection.get("detector_id", "")).lower()
        if detector_id == "motion":
            return "Motion"
        # Heuristics: presence of bbox/detections implies object detection
        attributes = detection.get("attributes") or {}
        if attributes.get("bbox"):
            return "Detection"
        nested = attributes.get("detections")
        if isinstance(nested, list) and any(
            isinstance(entry, dict) and entry.get("bbox") for entry in nested
        ):
            return "Detection"
        # Fallback: if confidence indicates classifier-style, prefer Detection
        try:
            conf = float(detection.get("confidence", 0.0))
            if conf > 0 and detector_id:
                return "Detection"
        except (TypeError, ValueError):
            pass
        return "Motion"

    def _extract_compact_timestamp(self, metadata: dict[str, Any]) -> str:
        """Return YYYY-MM-DD HH:MM.SS from detection.timestamp_utc (fallback to now UTC)."""
        import datetime as dt

        detection = metadata.get("detection") or {}
        raw = detection.get("timestamp_utc")
        dt_obj: dt.datetime | None = None
        if isinstance(raw, str):
            # Handle ISO strings, including trailing 'Z'
            try:
                norm = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
                dt_obj = dt.datetime.fromisoformat(norm)
            except Exception:
                dt_obj = None
        elif isinstance(raw, dt.datetime):
            dt_obj = raw
        if dt_obj is None:
            dt_obj = dt.datetime.now(tz=dt.UTC)
        # Convert to naive or keep tz-aware but format consistently
        ts = dt_obj.astimezone(dt.UTC).strftime("%d/%m/%y %H:%M.%S")
        return ts

    def _normalize_delivery(self, value: Any, *, default: str) -> str:
        if value is None:
            return default
        result = str(value).strip().lower()
        if not result:
            return default
        return result

    def _normalize_targets(self, value: Any) -> list[int | str]:
        """Return a deduplicated list of chat targets."""

        targets: list[int | str] = []
        if value is None:
            return targets
        if isinstance(value, int | str):
            targets.append(value)
            return targets
        if isinstance(value, list | tuple | set):
            for candidate in value:
                if isinstance(candidate, int | str) and candidate not in targets:
                    targets.append(candidate)
        return targets

    def _recipients_for(self, kind: str) -> list[int | str]:
        """Return configured recipients for the given notification kind."""

        specific = self._chat_targets.get(kind) or []
        if specific:
            return specific
        return self._chat_targets.get("snapshot", [])


__all__ = [
    "BotTelegramSender",
    "TelegramNotifier",
    "TelegramSendError",
    "TelegramSender",
]
