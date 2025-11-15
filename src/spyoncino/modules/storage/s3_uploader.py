"""
Asynchronous uploader that mirrors local artifacts into S3-compatible storage.

The module listens to artifact topics (snapshots, GIFs, clips) and uploads the
referenced files using boto3's multipart transfer helpers. Each successful or
failed upload is reported back to the bus via `storage.s3.synced`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # pragma: no cover - optional dependency at runtime
    from boto3.s3.transfer import S3Transfer, TransferConfig
    from boto3.session import Session
except ImportError as exc:  # pragma: no cover
    Session = None  # type: ignore[assignment]
    S3Transfer = None  # type: ignore[assignment]
    TransferConfig = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None

from ...core.contracts import (
    BaseModule,
    HealthStatus,
    MediaArtifact,
    ModuleConfig,
    SnapshotArtifact,
    StorageSyncResult,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _UploadWork:
    topic: str
    artifact_path: Path
    metadata: dict[str, Any]


class S3ArtifactUploader(BaseModule):
    """Upload snapshot/GIF/clip artifacts to S3-compatible storage."""

    name = "modules.storage.s3_uploader"

    def __init__(
        self,
        *,
        client_factory: Callable[[], tuple[Any, Any]] | None = None,
    ) -> None:
        super().__init__()
        self._client_factory = client_factory or self._default_client_factory
        self._bucket: str | None = None
        self._prefix = ""
        self._region: str | None = None
        self._enabled = False
        self._upload_topics: list[str] = [
            "event.snapshot.ready",
            "event.snapshot.allowed",
            "event.gif.ready",
            "event.clip.ready",
        ]
        self._publish_topic = "storage.s3.synced"
        self._root = Path(".")
        self._queue_size = 64
        self._max_workers = 2
        self._lifecycle_tags: dict[str, str] = {}
        self._client: Any | None = None
        self._transfer: Any | None = None
        self._queue: asyncio.Queue[_UploadWork] | None = None
        self._workers: list[asyncio.Task[None]] = []
        self._subscriptions = []
        self._last_result: StorageSyncResult | None = None
        self._multipart_threshold = 8 * 1024 * 1024
        self._multipart_chunksize = 8 * 1024 * 1024
        self._endpoint_url: str | None = None
        self._aws_access_key_id: str | None = None
        self._aws_secret_access_key: str | None = None
        self._aws_session_token: str | None = None

    async def configure(self, config: ModuleConfig) -> None:
        await super().configure(config)
        options = config.options
        self._enabled = bool(options.get("enabled", self._enabled))
        self._bucket = options.get("bucket") or self._bucket
        self._region = options.get("region_name", self._region)
        self._prefix = (options.get("prefix") or "").strip("/")
        self._upload_topics = list(options.get("upload_topics", self._upload_topics))
        self._publish_topic = options.get("publish_topic", self._publish_topic)
        self._root = Path(options.get("root_dir", self._root)).resolve()
        self._queue_size = int(options.get("queue_size", self._queue_size))
        self._max_workers = max(1, int(options.get("max_concurrency", self._max_workers)))
        self._lifecycle_tags = dict(options.get("lifecycle_tags", self._lifecycle_tags))
        self._multipart_threshold = int(
            float(options.get("multipart_threshold_mb", 8.0)) * 1024 * 1024
        )
        self._multipart_chunksize = int(
            float(options.get("multipart_chunks_mb", 8.0)) * 1024 * 1024
        )
        self._endpoint_url = options.get("endpoint_url")
        self._aws_access_key_id = options.get("aws_access_key_id")
        self._aws_secret_access_key = options.get("aws_secret_access_key")
        self._aws_session_token = options.get("aws_session_token")

    async def start(self) -> None:
        if not self._enabled:
            logger.info("S3ArtifactUploader disabled; skipping startup.")
            return
        if _IMPORT_ERROR is not None:
            raise RuntimeError(
                "boto3 is required for S3ArtifactUploader. Install boto3 to enable this module."
            ) from _IMPORT_ERROR
        if not self._bucket:
            raise RuntimeError("S3ArtifactUploader requires `bucket` configuration.")
        self._client, self._transfer = self._client_factory()
        self._queue = asyncio.Queue(maxsize=self._queue_size)
        for topic in self._upload_topics:
            self._subscriptions.append(self.bus.subscribe(topic, self._enqueue_artifact))
        for _ in range(self._max_workers):
            self._workers.append(asyncio.create_task(self._worker_loop()))
        logger.info(
            "S3ArtifactUploader started with %d workers targeting bucket %s",
            self._max_workers,
            self._bucket,
        )

    async def stop(self) -> None:
        for subscription in self._subscriptions:
            self.bus.unsubscribe(subscription)
        self._subscriptions.clear()
        if self._queue:
            for _task in self._workers:
                _task.cancel()
            for task in self._workers:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._workers.clear()
        self._queue = None
        self._client = None
        self._transfer = None
        logger.info("S3ArtifactUploader stopped.")

    async def health(self) -> HealthStatus:
        status = "healthy" if self._enabled else "disabled"
        details: dict[str, Any] = {
            "bucket": self._bucket,
            "prefix": self._prefix,
            "queue_size": self._queue.qsize() if self._queue else 0,
            "last_result": self._last_result.model_dump() if self._last_result else None,
        }
        if not self._enabled:
            status = "disabled"
        elif not self._last_result or self._last_result.status == "failed":
            status = "degraded"
        return HealthStatus(status=status, details=details)

    async def _enqueue_artifact(
        self, topic: str, payload: SnapshotArtifact | MediaArtifact
    ) -> None:
        if not isinstance(payload, SnapshotArtifact | MediaArtifact):
            return
        if self._queue is None:
            logger.debug("S3 queue not ready; dropping artifact from %s", topic)
            return
        path = Path(payload.artifact_path)
        work = _UploadWork(
            topic=topic,
            artifact_path=path,
            metadata=getattr(payload, "metadata", {}),
        )
        try:
            self._queue.put_nowait(work)
        except asyncio.QueueFull:
            logger.warning("S3 upload queue full; dropping artifact %s", path)

    async def _worker_loop(self) -> None:
        assert self._queue is not None
        while True:
            work = await self._queue.get()
            try:
                await self._upload_artifact(work)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - logged for operators
                logger.exception("S3 upload failed for %s", work.artifact_path)
            finally:
                self._queue.task_done()

    async def _upload_artifact(self, work: _UploadWork) -> None:
        if self._client is None or self._transfer is None:
            raise RuntimeError("S3 client not initialised.")
        path = work.artifact_path
        if not path.exists():
            logger.warning("Artifact path %s no longer exists; skipping upload.", path)
            return
        local_path = path if path.is_absolute() else (self._root / path)
        key = self._build_object_key(local_path)
        start = time.perf_counter()
        result: StorageSyncResult
        try:
            await asyncio.to_thread(
                self._transfer.upload_file,
                str(local_path),
                self._bucket,
                key,
            )
            if self._lifecycle_tags:
                tag_set = [{"Key": k, "Value": v} for k, v in self._lifecycle_tags.items()]
                await asyncio.to_thread(
                    self._client.put_object_tagging,
                    Bucket=self._bucket,
                    Key=key,
                    Tagging={"TagSet": tag_set},
                )
            head = await asyncio.to_thread(self._client.head_object, Bucket=self._bucket, Key=key)
            duration_ms = (time.perf_counter() - start) * 1000
            size = os.path.getsize(local_path)
            result = StorageSyncResult(
                artifact_path=str(local_path),
                bucket=self._bucket or "",
                object_key=key,
                size_bytes=size,
                etag=head.get("ETag"),
                version_id=head.get("VersionId"),
                duration_ms=round(duration_ms, 3),
                lifecycle_tags=self._lifecycle_tags,
                metadata={"topic": work.topic, **work.metadata},
                status="synced",
            )
        except Exception as exc:
            duration_ms = (time.perf_counter() - start) * 1000
            result = StorageSyncResult(
                artifact_path=str(local_path),
                bucket=self._bucket or "",
                object_key=key,
                size_bytes=local_path.stat().st_size if local_path.exists() else 0,
                etag=None,
                version_id=None,
                duration_ms=round(duration_ms, 3),
                lifecycle_tags=self._lifecycle_tags,
                metadata={"topic": work.topic},
                status="failed",
                error=str(exc),
            )
        self._last_result = result
        await self.bus.publish(self._publish_topic, result)

    def _build_object_key(self, path: Path) -> str:
        try:
            rel = path.relative_to(self._root)
        except ValueError:
            rel = path.name
        prefix = f"{self._prefix}/" if self._prefix else ""
        return f"{prefix}{rel}".replace("\\", "/")

    def _default_client_factory(self) -> tuple[Any, Any]:
        if Session is None or TransferConfig is None or S3Transfer is None:  # pragma: no cover
            raise RuntimeError("boto3 is not available in this environment.")
        session = Session(
            aws_access_key_id=self._aws_access_key_id,
            aws_secret_access_key=self._aws_secret_access_key,
            aws_session_token=self._aws_session_token,
            region_name=self._region,
        )
        client = session.client("s3", endpoint_url=self._endpoint_url, region_name=self._region)
        transfer_config = TransferConfig(
            multipart_threshold=self._multipart_threshold,
            multipart_chunksize=self._multipart_chunksize,
            max_concurrency=self._max_workers,
        )
        transfer = S3Transfer(client, config=transfer_config)
        return client, transfer


__all__ = ["S3ArtifactUploader"]
