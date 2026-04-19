"""
HTTP client for Spyoncino FastAPI control/status routes.

Telegram and other out-of-process callers use this instead of importing SpyoncinoRuntime.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import httpx

_ANALYTICS_CHART_TIMEOUT = 45.0
_MEDIA_FILE_TIMEOUT = 120.0


class SpyoncinoHttpClient:
    """Thin async wrapper around ``/api/status``, media, and control endpoints."""

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        timeout: float = 15.0,
    ):
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._headers: Dict[str, str] = {}
        if api_key:
            self._headers["X-API-Key"] = api_key

    def _client(self, user_id: Optional[int] = None) -> httpx.AsyncClient:
        headers = dict(self._headers)
        if user_id is not None:
            headers["X-User-Id"] = str(user_id)
        return httpx.AsyncClient(
            base_url=self.base_url, timeout=self._timeout, headers=headers
        )

    async def get_status(self, *, user_id: Optional[int] = None) -> Dict[str, Any]:
        async with self._client(user_id=user_id) as client:
            r = await client.get("/api/status")
            r.raise_for_status()
            return r.json()

    async def list_media(
        self,
        *,
        camera_id: Optional[str] = None,
        stage: Optional[str] = None,
        hours: int = 24 * 7,
        limit: int = 20,
        offset: int = 0,
        user_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"hours": hours, "limit": limit, "offset": offset}
        if camera_id is not None:
            params["camera_id"] = camera_id
        if stage is not None:
            params["stage"] = stage
        async with self._client(user_id=user_id) as client:
            r = await client.get("/api/media", params=params)
            r.raise_for_status()
            return r.json()

    async def get_media_meta(
        self,
        artifact_id: int,
        *,
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        async with self._client(user_id=user_id) as client:
            r = await client.get(f"/api/media/{int(artifact_id)}/meta")
            r.raise_for_status()
            return r.json()

    async def get_media_file_bytes(
        self,
        artifact_id: int,
        *,
        user_id: Optional[int] = None,
    ) -> Tuple[bytes, str]:
        """Download ``/api/media/{id}/file`` body and ``Content-Type`` header."""
        headers = dict(self._headers)
        if user_id is not None:
            headers["X-User-Id"] = str(user_id)
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=_MEDIA_FILE_TIMEOUT,
            headers=headers,
        ) as client:
            r = await client.get(f"/api/media/{int(artifact_id)}/file")
            r.raise_for_status()
            return r.content, (r.headers.get("content-type") or "").split(";")[
                0
            ].strip()

    async def set_paused(
        self, paused: bool, *, user_id: Optional[int] = None
    ) -> Dict[str, Any]:
        async with self._client(user_id=user_id) as client:
            r = await client.post("/api/control/pause", json={"paused": paused})
            r.raise_for_status()
            return r.json()

    async def snap(
        self, camera_id: str, *, user_id: Optional[int] = None
    ) -> Dict[str, Any]:
        async with self._client(user_id=user_id) as client:
            r = await client.post(
                "/api/control/snap",
                params={"camera_id": camera_id},
            )
            r.raise_for_status()
            return r.json()

    async def get_analytics_summary(
        self,
        hours: int = 24,
        *,
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        h = max(1, min(168, int(hours)))
        async with self._client(user_id=user_id) as client:
            r = await client.get("/api/analytics/summary", params={"hours": h})
            r.raise_for_status()
            return r.json()

    async def get_analytics_chart_jpeg(
        self,
        hours: int = 24,
        *,
        user_id: Optional[int] = None,
    ) -> bytes:
        h = max(1, min(168, int(hours)))
        headers = dict(self._headers)
        if user_id is not None:
            headers["X-User-Id"] = str(user_id)
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=_ANALYTICS_CHART_TIMEOUT,
            headers=headers,
        ) as client:
            r = await client.get("/api/analytics/chart.jpg", params={"hours": h})
            r.raise_for_status()
            return r.content

    async def get_all_config(self, *, user_id: Optional[int] = None) -> Dict[str, Any]:
        async with self._client(user_id=user_id) as client:
            r = await client.get("/api/config")
            r.raise_for_status()
            return r.json()

    async def get_config_traits(
        self, *, user_id: Optional[int] = None
    ) -> Dict[str, Dict[str, Any]]:
        async with self._client(user_id=user_id) as client:
            r = await client.get("/api/config/traits")
            r.raise_for_status()
            return r.json()

    async def set_config_value(
        self,
        key: str,
        value: Any,
        *,
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        async with self._client(user_id=user_id) as client:
            r = await client.put(f"/api/config/{key}", json={"value": value})
            r.raise_for_status()
            return r.json()

    async def reset_config(
        self,
        *,
        key: Optional[str] = None,
        reset_all: bool = False,
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"reset_all": bool(reset_all)}
        if key is not None:
            body["key"] = key
        async with self._client(user_id=user_id) as client:
            r = await client.post("/api/config/reset", json=body)
            r.raise_for_status()
            return r.json()

    async def list_identities(
        self, *, user_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        async with self._client(user_id=user_id) as client:
            r = await client.get("/api/identities")
            r.raise_for_status()
            return r.json()

    async def create_identity(
        self,
        display_name: str,
        *,
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        async with self._client(user_id=user_id) as client:
            r = await client.post(
                "/api/identities", json={"display_name": display_name}
            )
            r.raise_for_status()
            return r.json()

    async def list_pending_faces(
        self, *, user_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        async with self._client(user_id=user_id) as client:
            r = await client.get("/api/face/pending")
            r.raise_for_status()
            return r.json()

    async def get_recent_face_presence(
        self,
        *,
        hours: int = 1,
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        h = max(1, min(168, int(hours)))
        async with self._client(user_id=user_id) as client:
            r = await client.get("/api/face/recent", params={"hours": h})
            r.raise_for_status()
            return r.json()

    async def assign_pending_face(
        self,
        pending_id: str,
        *,
        identity_id: Optional[str] = None,
        new_display_name: Optional[str] = None,
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        if identity_id:
            body["identity_id"] = identity_id
        if new_display_name:
            body["new_display_name"] = new_display_name
        async with self._client(user_id=user_id) as client:
            r = await client.post(f"/api/face/pending/{pending_id}/assign", json=body)
            r.raise_for_status()
            return r.json()

    async def ignore_pending_face(
        self,
        pending_id: str,
        *,
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        async with self._client(user_id=user_id) as client:
            r = await client.post(f"/api/face/pending/{pending_id}/ignore")
            r.raise_for_status()
            return r.json()

    async def unassign_assigned_face(
        self,
        pending_id: str,
        *,
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        async with self._client(user_id=user_id) as client:
            r = await client.post(f"/api/face/pending/{pending_id}/unassign")
            r.raise_for_status()
            return r.json()
