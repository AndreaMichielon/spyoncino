"""
Email notifier module that sends artifacts via SMTP.

The notifier subscribes to configured bus topics, renders subject/body templates,
and sends snapshots, GIFs, or clips as email attachments using a configurable
SMTP backend.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Protocol

from ...core.bus import Subscription
from ...core.contracts import BaseModule, MediaArtifact, ModuleConfig, SnapshotArtifact

logger = logging.getLogger(__name__)


class EmailSendError(RuntimeError):
    """Raised when sending an email notification fails."""


@dataclass(slots=True)
class EmailAttachment:
    """Attachment descriptor used by EmailSender implementations."""

    file_path: Path
    content_type: str | None = None


class EmailSender(Protocol):
    """Protocol implemented by concrete SMTP/email senders."""

    async def send_email(
        self,
        *,
        subject: str,
        body: str,
        sender: str,
        recipients: Sequence[str],
        attachments: Sequence[EmailAttachment] = (),
        headers: dict[str, str] | None = None,
    ) -> None: ...


class SMTPLibEmailSender:
    """SMTP client backed by smtplib with optional TLS/SSL support."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str | None = None,
        password: str | None = None,
        use_tls: bool = True,
        use_ssl: bool = False,
        timeout: float = 30.0,
    ) -> None:
        if not host:
            raise EmailSendError("SMTP host is required.")
        if use_tls and use_ssl:
            raise EmailSendError("use_tls and use_ssl are mutually exclusive.")
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._use_tls = use_tls
        self._use_ssl = use_ssl
        self._timeout = timeout

    async def send_email(
        self,
        *,
        subject: str,
        body: str,
        sender: str,
        recipients: Sequence[str],
        attachments: Sequence[EmailAttachment] = (),
        headers: dict[str, str] | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._send_blocking, subject, body, sender, list(recipients), list(attachments), headers
        )

    def _send_blocking(
        self,
        subject: str,
        body: str,
        sender: str,
        recipients: list[str],
        attachments: list[EmailAttachment],
        headers: dict[str, str] | None,
    ) -> None:
        if not recipients:
            raise EmailSendError("At least one recipient is required.")

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = sender
        message["To"] = ", ".join(recipients)
        if headers:
            for key, value in headers.items():
                if key and value:
                    message[key] = value
        message.set_content(body)

        for attachment in attachments:
            file_path = attachment.file_path
            try:
                data = file_path.read_bytes()
            except OSError as exc:
                raise EmailSendError(f"Failed to read attachment {file_path}") from exc
            content_type = attachment.content_type or mimetypes.guess_type(file_path)[0]
            maintype, subtype = (content_type or "application/octet-stream").split("/", 1)
            message.add_attachment(
                data,
                maintype=maintype,
                subtype=subtype,
                filename=file_path.name,
            )

        try:
            import smtplib
            import ssl
        except ModuleNotFoundError as exc:  # pragma: no cover - stdlib guard
            raise EmailSendError("smtplib/ssl modules are unavailable") from exc

        context = ssl.create_default_context()
        smtp_cls = smtplib.SMTP_SSL if self._use_ssl else smtplib.SMTP
        try:
            with smtp_cls(self._host, self._port, timeout=self._timeout) as client:
                if self._use_tls and not self._use_ssl:
                    client.starttls(context=context)
                if self._username and self._password:
                    client.login(self._username, self._password)
                client.send_message(message)
        except smtplib.SMTPException as exc:  # pragma: no cover - network errors
            raise EmailSendError(str(exc)) from exc


def _ensure_list(value: Any) -> list[str]:
    """Normalize recipient config values into a simple list."""
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, Iterable):
        normalized = []
        for item in value:
            if item is None:
                continue
            text = str(item).strip()
            if text:
                normalized.append(text)
        return normalized
    raise ValueError(f"Unsupported recipient value {value!r}")


class EmailNotifier(BaseModule):
    """Output module that delivers artifacts via email."""

    name = "modules.output.email_notifier"

    def __init__(self, *, sender: EmailSender | None = None) -> None:
        super().__init__()
        self._sender = sender
        self._subscriptions: list[Subscription] = []
        self._topics: dict[str, str | None] = {
            "snapshot": "event.snapshot.ready",
            "gif": "event.gif.ready",
            "clip": "event.clip.ready",
        }
        self._recipients: dict[str, list[str]] = {
            "snapshot": [],
            "gif": [],
            "clip": [],
        }
        self._sender_address = "noreply@spyoncino.local"
        self._subject_templates = {
            "snapshot": "Spyoncino alert on {camera_id}",
            "gif": "Spyoncino GIF alert on {camera_id}",
            "clip": "Spyoncino clip alert on {camera_id}",
        }
        self._body_templates = {
            "snapshot": (
                "Motion detected on camera {camera_id} by detector {detector_id} "
                "with confidence {confidence:.2f} at {timestamp}"
            ),
            "gif": "GIF available for camera {camera_id} (detector={detector_id}).",
            "clip": "Clip recorded on camera {camera_id}.",
        }
        self._smtp_options: dict[str, Any] = {}
        self._custom_headers: dict[str, str] = {}

    async def configure(self, config: ModuleConfig) -> None:
        await super().configure(config)
        options = config.options
        self._topics["snapshot"] = options.get("topic", self._topics["snapshot"])
        self._topics["gif"] = options.get("gif_topic", self._topics["gif"])
        self._topics["clip"] = options.get("clip_topic", self._topics["clip"])

        snapshot_recipients = _ensure_list(options.get("recipients") or options.get("to"))
        self._recipients["snapshot"] = snapshot_recipients
        self._recipients["gif"] = _ensure_list(options.get("gif_recipients")) or snapshot_recipients
        self._recipients["clip"] = (
            _ensure_list(options.get("clip_recipients")) or snapshot_recipients
        )

        self._sender_address = options.get("from_address", self._sender_address)
        if "subject_template" in options:
            self._subject_templates["snapshot"] = options["subject_template"]
        if "gif_subject_template" in options:
            self._subject_templates["gif"] = options["gif_subject_template"]
        if "clip_subject_template" in options:
            self._subject_templates["clip"] = options["clip_subject_template"]
        if "body_template" in options:
            self._body_templates["snapshot"] = options["body_template"]
        if "gif_body_template" in options:
            self._body_templates["gif"] = options["gif_body_template"]
        if "clip_body_template" in options:
            self._body_templates["clip"] = options["clip_body_template"]

        headers = options.get("headers")
        if isinstance(headers, dict):
            self._custom_headers = {str(k): str(v) for k, v in headers.items()}

        smtp_options = {
            "host": options.get("smtp_host"),
            "port": int(options.get("smtp_port", 587)),
            "username": options.get("smtp_username"),
            "password": options.get("smtp_password"),
            "use_tls": bool(options.get("smtp_use_tls", True)),
            "use_ssl": bool(options.get("smtp_use_ssl", False)),
            "timeout": float(options.get("smtp_timeout", 30.0)),
        }
        # Filter out None host so we can tell later if configuration is complete.
        self._smtp_options = smtp_options if smtp_options["host"] else {}

    async def start(self) -> None:
        if self._sender is None and self._smtp_options:
            self._sender = SMTPLibEmailSender(**self._smtp_options)
        if self._sender is None:
            logger.warning("EmailNotifier has no sender configured; notifications will be dropped.")
        for kind, topic in self._topics.items():
            if not topic:
                continue

            async def handler(event_topic: str, payload: Any, *, _kind=kind) -> None:
                await self._handle_payload(_kind, event_topic, payload)

            self._subscriptions.append(self.bus.subscribe(topic, handler))
        logger.info(
            "EmailNotifier listening on %s",
            {k: v for k, v in self._topics.items() if v},
        )

    async def stop(self) -> None:
        for subscription in self._subscriptions:
            self.bus.unsubscribe(subscription)
        self._subscriptions.clear()

    async def _handle_payload(self, kind: str, topic: str, payload: Any) -> None:
        if isinstance(payload, SnapshotArtifact):
            await self._deliver_snapshot(payload, kind)
        elif isinstance(payload, MediaArtifact) and kind == "clip":
            await self._deliver_clip(payload)

    async def _deliver_snapshot(self, artifact: SnapshotArtifact, kind: str) -> None:
        sender = self._sender
        recipients = self._recipients.get(kind) or self._recipients.get("snapshot", [])
        if sender is None or not recipients:
            logger.debug("Skipping %s email because sender/recipients unavailable.", kind)
            return
        file_path = Path(artifact.artifact_path)
        if not file_path.exists():
            logger.warning("Artifact path %s does not exist; skipping.", file_path)
            return
        subject = self._render_subject(kind, artifact.metadata, artifact.camera_id)
        body = self._render_body(kind, artifact.metadata, artifact.camera_id)
        attachment = EmailAttachment(file_path=file_path, content_type=artifact.content_type)
        try:
            await sender.send_email(
                subject=subject,
                body=body,
                sender=self._sender_address,
                recipients=recipients,
                attachments=[attachment],
                headers=self._custom_headers,
            )
            logger.info("EmailNotifier delivered %s %s", kind, file_path.name)
        except EmailSendError as exc:
            logger.error("Email %s notification failed: %s", kind, exc)

    async def _deliver_clip(self, artifact: MediaArtifact) -> None:
        sender = self._sender
        recipients = self._recipients.get("clip") or self._recipients.get("snapshot", [])
        if sender is None or not recipients:
            logger.debug("Skipping clip email because sender/recipients unavailable.")
            return
        file_path = Path(artifact.artifact_path)
        if not file_path.exists():
            logger.warning("Clip path %s does not exist; skipping.", file_path)
            return
        subject = self._render_subject("clip", artifact.metadata, artifact.camera_id)
        body = self._render_body("clip", artifact.metadata, artifact.camera_id)
        attachment = EmailAttachment(file_path=file_path, content_type=artifact.content_type)
        try:
            await sender.send_email(
                subject=subject,
                body=body,
                sender=self._sender_address,
                recipients=recipients,
                attachments=[attachment],
                headers=self._custom_headers,
            )
            logger.info("EmailNotifier delivered clip %s", file_path.name)
        except EmailSendError as exc:
            logger.error("Email clip notification failed: %s", exc)

    def _render_subject(self, kind: str, metadata: dict[str, Any], camera_id: str) -> str:
        template = self._subject_templates.get(kind) or self._subject_templates["snapshot"]
        return self._render_template(template, metadata, camera_id)

    def _render_body(self, kind: str, metadata: dict[str, Any], camera_id: str) -> str:
        template = self._body_templates.get(kind) or self._body_templates["snapshot"]
        return self._render_template(template, metadata, camera_id)

    def _render_template(self, template: str, metadata: dict[str, Any], camera_id: str) -> str:
        detection = metadata.get("detection") or {}
        template_vars = {
            "camera_id": camera_id,
            "detector_id": detection.get("detector_id", "unknown"),
            "confidence": detection.get("confidence", 0.0),
            "timestamp": detection.get("timestamp_utc", ""),
        }
        try:
            return template.format(**template_vars)
        except KeyError:
            return f"Spyoncino alert on {camera_id}"


__all__ = [
    "EmailAttachment",
    "EmailNotifier",
    "EmailSendError",
    "EmailSender",
    "SMTPLibEmailSender",
]
