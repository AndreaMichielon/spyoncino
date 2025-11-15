"""
FastAPI-powered control surface for camera toggles and zoning updates.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from ...core.config import ZoneDefinition
from ...core.contracts import BaseModule, ConfigUpdate, ControlCommand, ModuleConfig

logger = logging.getLogger(__name__)


class CameraStateRequest(BaseModel):
    """Request body for enabling/disabling a camera."""

    enabled: bool = Field(description="Whether the camera should be active.")


class ZoneUpdateRequest(BaseModel):
    """Request for updating zoning definitions."""

    camera_id: str
    zones: list[ZoneDefinition]


class ControlApi(BaseModule):
    """Expose HTTP endpoints that publish control/config events to the bus."""

    name = "modules.dashboard.control_api"

    def __init__(
        self,
        *,
        config_factory: Callable[..., uvicorn.Config] | None = None,
        server_factory: Callable[[uvicorn.Config], uvicorn.Server] | None = None,
    ) -> None:
        super().__init__()
        self._host = "127.0.0.1"
        self._port = 8080
        self._serve_api = True
        self._command_topic = "dashboard.control.command"
        self._config_topic = "config.update"
        self._app: FastAPI | None = None
        self._server: uvicorn.Server | None = None
        self._server_task: asyncio.Task[None] | None = None
        self._config_factory = config_factory or uvicorn.Config
        self._server_factory = server_factory or uvicorn.Server

    async def configure(self, config: ModuleConfig) -> None:
        await super().configure(config)
        options = config.options
        self._host = options.get("host", self._host)
        self._port = int(options.get("port", self._port))
        self._serve_api = bool(options.get("serve_api", self._serve_api))
        self._command_topic = options.get("command_topic", self._command_topic)
        self._config_topic = options.get("config_topic", self._config_topic)

    async def start(self) -> None:
        self._app = self._build_app()
        if not self._serve_api:
            logger.info("ControlApi running in embedded-only mode (no HTTP server).")
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
        logger.info("ControlApi listening on http://%s:%s", self._host, self._port)

    async def stop(self) -> None:
        if self._server_task:
            self._server.should_exit = True  # type: ignore[union-attr]
            await asyncio.wait(
                [self._server_task],
                timeout=1,
            )
            self._server_task = None
        self._server = None

    @property
    def app(self) -> FastAPI:
        if self._app is None:
            raise RuntimeError("ControlApi has not been started or configured yet.")
        return self._app

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="Spyoncino Control API", version="0.1.0")

        @app.get("/health")
        async def health() -> dict[str, str]:
            return {"status": "ok"}

        @app.post("/cameras/{camera_id}/state", status_code=202)
        async def camera_state(camera_id: str, request: CameraStateRequest) -> dict[str, str]:
            await self._publish_command(
                ControlCommand(
                    command="camera.state", camera_id=camera_id, arguments=request.model_dump()
                )
            )
            return {"status": "accepted"}

        @app.post("/config/zones", status_code=202)
        async def update_zones(request: ZoneUpdateRequest) -> dict[str, str]:
            if not request.zones:
                raise HTTPException(status_code=400, detail="At least one zone must be provided.")
            zones = []
            for zone in request.zones:
                payload = zone.model_copy(update={"camera_id": request.camera_id})
                zones.append(payload.model_dump(mode="python"))
            update = ConfigUpdate(
                source="control_api",
                changes={
                    "zoning": {
                        "zones": zones,
                        "enabled": True,
                    }
                },
            )
            await self.bus.publish(self._config_topic, update)
            return {"status": "accepted"}

        return app

    async def _publish_command(self, command: ControlCommand) -> None:
        await self.bus.publish(self._command_topic, command)


__all__ = ["ControlApi"]
