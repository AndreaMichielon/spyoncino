"""
Webhook notifier module that forwards artifacts to HTTP endpoints.

The notifier listens to snapshot/GIF/clip topics and POSTs JSON payloads to
configurable URLs, optionally embedding the binary artifact as base64 data.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
from typing import Any, Protocol

from ...core.bus import Subscription
from ...core.contracts import BaseModule, MediaArtifact, ModuleConfig, SnapshotArtifact

logger = logging.getLogger(__name__)


class WebhookSendError(RuntimeError):
    """Raised when dispatching a webhook fails."""


class WebhookClient(Protocol):
    """Protocol implemented by concrete HTTP clients."""

    async def send_json(
        self,
        *,
        url: str,
        payload: dict[str, Any],
        method: str = "POST",
        headers: dict[str, str] | None = None,
    ) -> None: ...


class HttpxWebhookClient:
    """Webhook client implemented with httpx."""

    def __init__(self, *, timeout: float = 15.0, verify: bool = True) -> None:
        try:
            import httpx
        except ModuleNotFoundError as exc:  # pragma: no cover - import guard
            raise WebhookSendError("httpx is not installed") from exc
        self._httpx = httpx
        self._timeout = timeout
        self._verify = verify

    async def send_json(
        self,
        *,
        url: str,
        payload: dict[str, Any],
        method: str = "POST",
        headers: dict[str, str] | None = None,
    ) -> None:
        if not url:
            raise WebhookSendError("Webhook URL is required.")
        async with self._httpx.AsyncClient(timeout=self._timeout, verify=self._verify) as client:
            try:
                response = await client.request(method, url, json=payload, headers=headers)
                response.raise_for_status()
            except self._httpx.HTTPError as exc:  # pragma: no cover - network errors
                raise WebhookSendError(str(exc)) from exc


class WebhookNotifier(BaseModule):
    """Output module that invokes HTTP webhooks for alerts."""

    name = "modules.output.webhook_notifier"

    def __init__(self, *, client: WebhookClient | None = None) -> None:
        super().__init__()
        self._client = client
        self._subscriptions: list[Subscription] = []
        self._topics: dict[str, str | None] = {
            "snapshot": "event.snapshot.ready",
            "gif": "event.gif.ready",
            "clip": "event.clip.ready",
        }
        self._urls: dict[str, str | None] = {
            "snapshot": None,
            "gif": None,
            "clip": None,
        }
        self._method = "POST"
        self._headers: dict[str, str] = {}
        self._timeout = 15.0
        self._verify_ssl = True
        self._include_binary = False
        self._max_binary_bytes = 2 * 1024 * 1024  # 2 MiB
        self._require_https = True

    async def configure(self, config: ModuleConfig) -> None:
        await super().configure(config)
        options = config.options
        self._topics["snapshot"] = options.get("topic", self._topics["snapshot"])
        self._topics["gif"] = options.get("gif_topic", self._topics["gif"])
        self._topics["clip"] = options.get("clip_topic", self._topics["clip"])

        snapshot_url = options.get("url")
        self._require_https = bool(options.get("require_https", self._require_https))
        self._urls["snapshot"] = self._validate_url(snapshot_url, "snapshot")
        self._urls["gif"] = self._validate_url(options.get("gif_url", snapshot_url), "gif")
        self._urls["clip"] = self._validate_url(options.get("clip_url", snapshot_url), "clip")

        self._method = options.get("method", self._method).upper()
        headers = options.get("headers")
        if isinstance(headers, dict):
            self._headers = {str(k): str(v) for k, v in headers.items()}
        self._timeout = float(options.get("timeout", self._timeout))
        self._verify_ssl = bool(options.get("verify_ssl", self._verify_ssl))
        self._include_binary = bool(options.get("include_binary", self._include_binary))
        self._max_binary_bytes = int(options.get("max_binary_bytes", self._max_binary_bytes))

    async def start(self) -> None:
        if self._client is None:
            try:
                self._client = HttpxWebhookClient(timeout=self._timeout, verify=self._verify_ssl)
            except WebhookSendError as exc:
                logger.warning("WebhookNotifier cannot initialize HTTP client: %s", exc)
        if self._client is None:
            logger.warning(
                "WebhookNotifier has no HTTP client configured; notifications will be dropped."
            )
        for kind, topic in self._topics.items():
            if not topic:
                continue

            async def handler(event_topic: str, payload: Any, *, _kind=kind) -> None:
                await self._handle_payload(_kind, event_topic, payload)

            self._subscriptions.append(self.bus.subscribe(topic, handler))
        logger.info(
            "WebhookNotifier listening on %s",
            {k: v for k, v in self._topics.items() if v},
        )

    async def stop(self) -> None:
        for subscription in self._subscriptions:
            self.bus.unsubscribe(subscription)
        self._subscriptions.clear()

    async def _handle_payload(self, kind: str, topic: str, payload: Any) -> None:
        if isinstance(payload, SnapshotArtifact) or (
            isinstance(payload, MediaArtifact) and kind == "clip"
        ):
            await self._dispatch_payload(kind, payload.camera_id, payload)

    async def _dispatch_payload(
        self,
        kind: str,
        camera_id: str,
        artifact: SnapshotArtifact | MediaArtifact,
    ) -> None:
        client = self._client
        url = self._urls.get(kind) or self._urls.get("snapshot")
        if client is None or not url:
            logger.debug("Skipping %s webhook because client/url unavailable.", kind)
            return
        payload = await self._build_payload(kind, camera_id, artifact)
        try:
            await client.send_json(
                url=url,
                payload=payload,
                method=self._method,
                headers=self._headers,
            )
            logger.info("WebhookNotifier delivered %s to %s", kind, url)
        except WebhookSendError as exc:
            logger.error("Webhook %s notification failed: %s", kind, exc)

    async def _build_payload(
        self,
        kind: str,
        camera_id: str,
        artifact: SnapshotArtifact | MediaArtifact,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "event_kind": kind,
            "camera_id": camera_id,
            "artifact_path": artifact.artifact_path,
            "content_type": getattr(artifact, "content_type", None),
            "metadata": artifact.metadata,
        }
        if isinstance(artifact, MediaArtifact):
            payload["media_kind"] = artifact.media_kind
        if self._include_binary:
            file_path = Path(artifact.artifact_path)
            payload["artifact_base64"] = await self._read_base64(file_path)
        return payload

    async def _read_base64(self, file_path: Path) -> str | None:
        if not file_path.exists():
            logger.warning("Artifact path %s does not exist; skipping binary embed.", file_path)
            return None
        try:
            data = await asyncio.to_thread(file_path.read_bytes)
        except OSError as exc:
            logger.error("Failed to read artifact %s: %s", file_path, exc)
            return None
        if len(data) > self._max_binary_bytes:
            logger.debug(
                "Skipping binary embed for %s because payload exceeds %s bytes.",
                file_path,
                self._max_binary_bytes,
            )
            return None
        return base64.b64encode(data).decode("ascii")

    def _validate_url(self, url: str | None, kind: str) -> str | None:
        if url is None:
            return None
        if self._require_https and not url.lower().startswith("https://"):
            raise ValueError(f"{kind} webhook URL must use https:// when require_https is enabled.")
        return url


__all__ = [
    "HttpxWebhookClient",
    "WebhookClient",
    "WebhookNotifier",
    "WebhookSendError",
]
