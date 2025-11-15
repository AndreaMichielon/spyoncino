"""
Realtime streaming gateway that surfaces bus topics over WebSocket/HTTP.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from ...core.contracts import BaseModule, BasePayload, HealthStatus, ModuleConfig

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _RealtimeClient:
    queue: asyncio.Queue[dict[str, Any]]


class WebsocketGateway(BaseModule):
    """Expose bus events to dashboards via WebSockets with polling fallback."""

    name = "modules.dashboard.websocket_gateway"

    def __init__(
        self,
        *,
        config_factory: Callable[..., uvicorn.Config] | None = None,
        server_factory: Callable[[uvicorn.Config], uvicorn.Server] | None = None,
    ) -> None:
        super().__init__()
        self._host = "127.0.0.1"
        self._port = 8081
        self._serve_http = True
        self._topics: list[str] = [
            "status.health.summary",
            "status.bus",
            "notify.telegram.sent",
            "analytics.persistence.cursor",
        ]
        self._buffer_size = 256
        self._idle_timeout = 30.0
        self._buffer: deque[dict[str, Any]] = deque(maxlen=self._buffer_size)
        self._clients: set[_RealtimeClient] = set()
        self._subscriptions = []
        self._app: FastAPI | None = None
        self._config_factory = config_factory or uvicorn.Config
        self._server_factory = server_factory or uvicorn.Server
        self._server: uvicorn.Server | None = None
        self._server_task: asyncio.Task[None] | None = None

    async def configure(self, config: ModuleConfig) -> None:
        await super().configure(config)
        options = config.options
        self._host = options.get("host", self._host)
        self._port = int(options.get("port", self._port))
        self._serve_http = bool(options.get("serve_http", self._serve_http))
        topics = options.get("topics")
        if topics:
            self._topics = list(dict.fromkeys(topics))
        self._buffer_size = int(options.get("buffer_size", self._buffer_size))
        self._buffer = deque(self._buffer, maxlen=self._buffer_size)
        self._idle_timeout = float(options.get("idle_timeout_seconds", self._idle_timeout))

    async def start(self) -> None:
        self._app = self._build_app()
        for topic in self._topics:
            self._subscriptions.append(self.bus.subscribe(topic, self._handle_payload))
        if not self._serve_http:
            logger.info("WebsocketGateway running in embedded mode (no HTTP server).")
            return
        config = self._config_factory(
            app=self._app,
            host=self._host,
            port=self._port,
            loop="asyncio",
            lifespan="on",
            log_level="info",
        )
        self._server = self._server_factory(config)
        self._server_task = asyncio.create_task(self._server.serve())
        logger.info("WebsocketGateway listening on ws://%s:%s/ws", self._host, self._port)

    async def stop(self) -> None:
        for subscription in self._subscriptions:
            self.bus.unsubscribe(subscription)
        self._subscriptions.clear()
        if self._server_task:
            self._server.should_exit = True  # type: ignore[union-attr]
            await asyncio.wait([self._server_task], timeout=1)
            self._server_task = None
        self._server = None
        self._app = None
        self._clients.clear()

    @property
    def app(self) -> FastAPI:
        if self._app is None:
            raise RuntimeError("WebsocketGateway has not been started.")
        return self._app

    async def health(self) -> HealthStatus:
        return HealthStatus(
            status="healthy",
            details={
                "topics": self._topics,
                "buffer_size": len(self._buffer),
                "clients": len(self._clients),
            },
        )

    async def _handle_payload(self, topic: str, payload: BasePayload) -> None:
        event = {
            "topic": topic,
            "payload": payload.model_dump(mode="json"),
        }
        self._buffer.append(event)
        await self._broadcast(event)

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="Spyoncino Realtime Gateway", version="0.1.0")

        @app.get("/health")
        async def health() -> dict[str, Any]:
            return {
                "status": "ok",
                "buffer_size": len(self._buffer),
                "topics": self._topics,
            }

        @app.get("/events")
        async def latest_events(limit: int = 50) -> dict[str, Any]:
            limit = max(1, min(limit, self._buffer_size))
            events = list(self._buffer)[-limit:]
            return {"events": events}

        @app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            client = _RealtimeClient(queue=asyncio.Queue())
            self._clients.add(client)
            try:
                for event in self._buffer:
                    await websocket.send_json(event)
                while True:
                    try:
                        event = await asyncio.wait_for(
                            client.queue.get(), timeout=self._idle_timeout
                        )
                    except TimeoutError:
                        await websocket.send_json({"type": "keepalive"})
                        continue
                    await websocket.send_json(event)
            except WebSocketDisconnect:
                logger.info("Websocket client disconnected.")
            finally:
                self._clients.discard(client)

        return app

    async def _broadcast(self, event: dict[str, Any]) -> None:
        if not self._clients:
            return
        for client in list(self._clients):
            try:
                client.queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("Dropping realtime event due to slow consumer.")


__all__ = ["WebsocketGateway"]
