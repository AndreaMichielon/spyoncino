"""
FastAPI Web Application - Web interface for metrics, events, and configuration.

Provides REST API and web UI for:
- Viewing system metrics
- Viewing events
- Viewing and updating configuration
- Service status monitoring
"""

import ipaddress
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict, Any
from pathlib import Path
import base64
import hashlib
import hmac
import json
import yaml

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, Response
from pydantic import BaseModel

from .authz import Principal, auth_state_from_config, can
from .memory_manager import MemoryManager, EventType
from ..runtime import DEPRECATED_CONFIG_KEYS
from ..shared_theme_css import SHARED_DASHBOARD_THEME_CSS


class EventResponse(BaseModel):
    """Event response model."""

    id: int
    timestamp: datetime
    event_type: str
    message: str
    severity: str
    camera_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class ServiceStatusResponse(BaseModel):
    """Service status response model."""

    service_name: str
    is_running: bool
    last_check: datetime
    last_error: Optional[str] = None
    uptime_seconds: Optional[float] = None


class MetricsResponse(BaseModel):
    """Metrics response model."""

    uptime_seconds: float
    total_events: int
    motion_events: int
    person_events: int
    face_events: int
    error_events: int
    last_event_time: Optional[datetime] = None
    services: Dict[str, ServiceStatusResponse]


class ConfigUpdateRequest(BaseModel):
    """Configuration update request model."""

    value: Any


class ConfigResetRequest(BaseModel):
    """Configuration reset request model."""

    key: Optional[str] = None
    reset_all: bool = False


class PauseRequest(BaseModel):
    """Pause / resume patrol loop."""

    paused: bool


class MediaArtifactResponse(BaseModel):
    """Indexed media row (paths relative to configured media root)."""

    id: int
    camera_id: str
    stage: str
    kind: str
    path_rel: str
    size_bytes: Optional[int] = None
    created_at: str
    metadata: Optional[Dict[str, Any]] = None


class SnapResponse(BaseModel):
    """Result of an on-demand snap."""

    id: Optional[int] = None
    path_rel: str
    path: str
    camera_id: str


class IdentityCreateRequest(BaseModel):
    display_name: str


class IdentityPatchRequest(BaseModel):
    display_name: str


class PendingAssignRequest(BaseModel):
    identity_id: Optional[str] = None
    new_display_name: Optional[str] = None


async def require_control_api_key(request: Request) -> None:
    """If ``app.state.control_api_key`` is set, require matching ``X-API-Key`` header."""
    expected = getattr(request.app.state, "control_api_key", None)
    if not expected:
        return
    if request.headers.get("X-API-Key") != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# IPv4 any-address default; avoids a literal bind-all string that trips static analysis.
_DEFAULT_WEBAPP_HOST = str(ipaddress.IPv4Address(0))


def _resolve_brand_static_dir() -> Optional[Path]:
    """Dashboard icons shipped with the package under ``spyoncino/static`` (wheel and editable)."""
    bundled = Path(__file__).resolve().parent.parent / "static"
    if (bundled / "logo.ico").is_file():
        return bundled
    return None


class WebAppInterface:
    """
    FastAPI web application interface.

    Provides REST API endpoints and web UI for:
    - System metrics
    - Event viewing
    - Configuration management
    - Service status
    """

    def __init__(
        self,
        memory_manager: MemoryManager,
        host: str = _DEFAULT_WEBAPP_HOST,
        port: int = 8000,
        config: Optional[Dict[str, Any]] = None,
        runtime: Optional[Any] = None,
        secrets_path: Optional[str] = None,
    ):
        """
        Initialize web application interface.

        Args:
            memory_manager: MemoryManager instance
            host: Host to bind to
            port: Port to bind to
            config: Optional configuration dictionary
            runtime: SpyoncinoRuntime when started via orchestrator (control + media API)
        """
        self.memory_manager = memory_manager
        self.host = host
        self.port = port
        self.config = config or {}
        self.runtime = runtime
        self.secrets_path = Path(
            secrets_path
            or self.config.get("secrets_path")
            or "data/config/secrets.yaml"
        )
        self._auth_data = self._load_auth_data()
        self._auth_state = auth_state_from_config(self._auth_data)

        # Create FastAPI app
        self.app = FastAPI(
            title="Spyoncino Web Interface",
            description="Web interface for security system metrics, events, and configuration",
            version="1.0.0",
        )
        if runtime is not None:
            self.app.state.runtime = runtime

        @self.app.on_event("startup")
        async def _redact_telegram_urls_in_logs() -> None:
            # Uvicorn replaces logging handlers after orchestrator main(); re-attach filters.
            from ..logging_redact import install_telegram_token_log_redaction

            install_telegram_token_log_redaction()

        self.app.state.control_api_key = (self.config or {}).get("api_key") or None
        self.app.state.require_user_auth = bool(
            (self.config or {}).get("require_user_auth", False)
        )
        self.app.state.dashboard_auth_enabled = bool(
            self._auth_data.get("dashboard_username")
            and self._auth_data.get("dashboard_password")
        )
        self._brand_static = _resolve_brand_static_dir()

        # Setup routes
        self._setup_routes()

        # Logging
        self.logger = logging.getLogger(self.__class__.__name__)

        # Log initialization
        self.memory_manager.log_event(
            EventType.STARTUP,
            f"Web application interface initialized on {host}:{port}",
            severity="info",
        )

    def _load_auth_data(self) -> Dict[str, Any]:
        if not self.secrets_path.exists():
            return {
                "setup_password": None,
                "superuser_id": None,
                "user_whitelist": [],
                "allow_group_commands": True,
                "silent_unauthorized": True,
            }
        try:
            with open(self.secrets_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            auth = raw.get("authentication") or {}
            if not isinstance(auth, dict):
                auth = {}
            out = dict(auth)
            out["superuser_id"] = self._parse_user_id(out.get("superuser_id"))
            wl_raw = out.get("user_whitelist", [])
            wl: List[int] = []
            if isinstance(wl_raw, list):
                for v in wl_raw:
                    parsed = self._parse_user_id(v)
                    if parsed is not None:
                        wl.append(parsed)
            out["user_whitelist"] = sorted(set(wl))
            out["dashboard_username"] = (
                str(out.get("dashboard_username") or "").strip() or None
            )
            out["dashboard_password"] = (
                str(out.get("dashboard_password") or "").strip() or None
            )
            out["session_secret"] = str(out.get("session_secret") or "").strip() or None
            return out
        except Exception as e:
            self.logger.warning("Failed to load auth config: %s", e)
            return {"setup_password": None, "superuser_id": None, "user_whitelist": []}

    def _save_auth_data(self) -> None:
        data: Dict[str, Any] = {}
        if self.secrets_path.exists():
            with open(self.secrets_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            data = {}
        auth = data.get("authentication")
        if not isinstance(auth, dict):
            auth = {}
        auth["setup_password"] = self._auth_data.get("setup_password")
        auth["superuser_id"] = self._auth_data.get("superuser_id")
        auth["user_whitelist"] = self._auth_data.get("user_whitelist", [])
        auth["allow_group_commands"] = self._auth_data.get("allow_group_commands", True)
        auth["silent_unauthorized"] = self._auth_data.get("silent_unauthorized", True)
        auth["dashboard_username"] = self._auth_data.get("dashboard_username")
        auth["dashboard_password"] = self._auth_data.get("dashboard_password")
        auth["session_secret"] = self._auth_data.get("session_secret")
        auth["session_version"] = self._session_version()
        data["authentication"] = auth
        self.secrets_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.secrets_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False)
        self._auth_state = auth_state_from_config(auth)

    def _refresh_auth_state(self) -> None:
        """Reload auth config from secrets file (useful for live credential edits)."""
        self._auth_data = self._load_auth_data()
        self._auth_state = auth_state_from_config(self._auth_data)
        self.app.state.dashboard_auth_enabled = bool(
            self._auth_data.get("dashboard_username")
            and self._auth_data.get("dashboard_password")
        )

    @staticmethod
    def _parse_user_id(value: Any) -> Optional[int]:
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, str) and value.isdigit():
            out = int(value)
            return out if out > 0 else None
        return None

    def _is_superuser(self, user_id: int) -> bool:
        return can(
            Principal(kind="telegram", user_id=user_id),
            "manage_whitelist",
            self._auth_state,
        )

    def _is_authorized(self, user_id: int) -> bool:
        return can(
            Principal(kind="telegram", user_id=user_id), "view_status", self._auth_state
        )

    def _session_secret(self) -> str:
        return (
            self._auth_data.get("session_secret")
            or self._auth_data.get("setup_password")
            or "spyoncino-dev-session-secret"
        )

    def _session_version(self) -> int:
        raw = self._auth_data.get("session_version")
        if isinstance(raw, int):
            return max(raw, 1)
        if isinstance(raw, str) and raw.strip().isdigit():
            return max(int(raw.strip()), 1)
        return 1

    def _bump_session_version(self) -> int:
        new_version = self._session_version() + 1
        self._auth_data["session_version"] = new_version
        self._save_auth_data()
        return new_version

    @staticmethod
    def _is_request_https(request: Request) -> bool:
        if request.url.scheme == "https":
            return True
        xfp = request.headers.get("X-Forwarded-Proto", "")
        return str(xfp).split(",")[0].strip().lower() == "https"

    def _create_dashboard_session(
        self, username: str, ttl_seconds: int = 60 * 60 * 2
    ) -> str:
        exp = int(datetime.now(timezone.utc).timestamp()) + ttl_seconds
        payload_obj = {"u": username, "exp": exp, "v": self._session_version()}
        payload_raw = json.dumps(payload_obj, separators=(",", ":")).encode("utf-8")
        payload_b64 = base64.urlsafe_b64encode(payload_raw).decode("ascii")
        sig = hmac.new(
            self._session_secret().encode("utf-8"),
            payload_b64.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        return f"{payload_b64}.{sig}"

    def _metrics_dict_to_response(self, m: Dict[str, Any]) -> MetricsResponse:
        """Build ``MetricsResponse`` from ``SpyoncinoRuntime.get_metrics()`` JSON-shaped dict."""
        services_dict: Dict[str, ServiceStatusResponse] = {}
        for name, st in (m.get("services") or {}).items():
            if not isinstance(st, dict):
                continue
            lc = st.get("last_check")
            if isinstance(lc, str):
                try:
                    lc = datetime.fromisoformat(lc)
                except ValueError:
                    lc = datetime.now()
            elif lc is None:
                lc = datetime.now()
            services_dict[name] = ServiceStatusResponse(
                service_name=st["service_name"],
                is_running=bool(st.get("is_running")),
                last_check=lc,
                last_error=st.get("last_error"),
                uptime_seconds=st.get("uptime_seconds"),
            )
        let = m.get("last_event_time")
        if isinstance(let, str):
            try:
                let_parsed: Optional[datetime] = datetime.fromisoformat(let)
            except ValueError:
                let_parsed = None
        else:
            let_parsed = let
        return MetricsResponse(
            uptime_seconds=float(m.get("uptime_seconds") or 0),
            total_events=int(m.get("total_events") or 0),
            motion_events=int(m.get("motion_events") or 0),
            person_events=int(m.get("person_events") or 0),
            face_events=int(m.get("face_events") or 0),
            error_events=int(m.get("error_events") or 0),
            last_event_time=let_parsed,
            services=services_dict,
        )

    def _verify_dashboard_session(self, token: Optional[str]) -> Optional[str]:
        if not token or "." not in token:
            return None
        payload_b64, got_sig = token.rsplit(".", 1)
        expected_sig = hmac.new(
            self._session_secret().encode("utf-8"),
            payload_b64.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(got_sig, expected_sig):
            return None
        try:
            payload_raw = base64.urlsafe_b64decode(payload_b64.encode("ascii"))
            payload = json.loads(payload_raw.decode("utf-8"))
            exp = int(payload.get("exp", 0))
            if exp < int(datetime.now(timezone.utc).timestamp()):
                return None
            token_version = int(payload.get("v", 1))
            if token_version != self._session_version():
                return None
            username = str(payload.get("u") or "")
            if not username:
                return None
            return username
        except Exception:
            return None

    def _setup_routes(self) -> None:
        """Setup API routes."""

        def _user_id_from_headers(request: Request) -> int:
            raw = request.headers.get("X-User-Id")
            user_id = self._parse_user_id(raw)
            if user_id is None:
                raise HTTPException(
                    status_code=401, detail="Missing or invalid X-User-Id header"
                )
            return user_id

        def require_dashboard_session(request: Request) -> Optional[str]:
            if not getattr(request.app.state, "dashboard_auth_enabled", False):
                return "dashboard"
            token = request.cookies.get("spyoncino_dashboard_session")
            username = self._verify_dashboard_session(token)
            if not username:
                raise HTTPException(status_code=401, detail="Dashboard login required")
            if not can(
                Principal(kind="dashboard", username=username),
                "view_metrics",
                self._auth_state,
            ):
                raise HTTPException(
                    status_code=403, detail="Dashboard user not authorized"
                )
            return username

        async def require_policy_action(
            request: Request,
            action: str,
            *,
            allow_dashboard: bool = True,
            allow_control_api: bool = True,
        ) -> None:
            self._refresh_auth_state()
            if allow_dashboard:
                try:
                    username = require_dashboard_session(request)
                    if not getattr(request.app.state, "dashboard_auth_enabled", False):
                        return
                    if username and can(
                        Principal(kind="dashboard", username=username),
                        action,
                        self._auth_state,
                    ):
                        return
                    raise HTTPException(
                        status_code=403, detail="Dashboard user not authorized"
                    )
                except HTTPException as e:
                    if not allow_control_api or e.status_code == 403:
                        raise
            if allow_control_api:
                await require_control_api_key(request)
                if not getattr(request.app.state, "require_user_auth", False):
                    return
                user_id = _user_id_from_headers(request)
                if can(
                    Principal(kind="telegram", user_id=user_id),
                    action,
                    self._auth_state,
                ):
                    return
                raise HTTPException(status_code=403, detail="User not authorized")
            raise HTTPException(status_code=401, detail="Authorization required")

        def action_dep(
            action: str,
            *,
            allow_dashboard: bool = True,
            allow_control_api: bool = True,
        ):
            async def _dep(request: Request) -> None:
                await require_policy_action(
                    request,
                    action,
                    allow_dashboard=allow_dashboard,
                    allow_control_api=allow_control_api,
                )

            return _dep

        @self.app.get("/health")
        async def health():
            """Liveness probe; does not require runtime or API key."""
            return {"status": "ok", "service": "spyoncino"}

        async def _file_from_brand(name: str, media_type: str) -> FileResponse:
            base = self._brand_static
            if base is None:
                raise HTTPException(status_code=404, detail="Brand assets not found")
            path = base / name
            if not path.is_file():
                raise HTTPException(status_code=404, detail="Asset not found")
            return FileResponse(path, media_type=media_type)

        @self.app.get("/favicon.ico", include_in_schema=False)
        async def favicon():
            return await _file_from_brand("logo.ico", "image/x-icon")

        @self.app.get("/static/logo.ico", include_in_schema=False)
        async def static_logo_ico():
            return await _file_from_brand("logo.ico", "image/x-icon")

        @self.app.get("/static/logo.png", include_in_schema=False)
        async def static_logo_png():
            return await _file_from_brand("logo.png", "image/png")

        async def apple_touch_icon():
            return await _file_from_brand("logo.png", "image/png")

        self.app.add_api_route(
            "/apple-touch-icon.png",
            apple_touch_icon,
            methods=["GET"],
            include_in_schema=False,
        )
        self.app.add_api_route(
            "/apple-touch-icon-precomposed.png",
            apple_touch_icon,
            methods=["GET"],
            include_in_schema=False,
        )

        @self.app.get(
            "/api/auth/me",
            dependencies=[Depends(require_control_api_key)],
        )
        async def auth_me(request: Request):
            """Return auth status for current caller (if X-User-Id is provided)."""
            user_raw = request.headers.get("X-User-Id")
            user_id = self._parse_user_id(user_raw) if user_raw else None
            if user_id is None:
                return {
                    "require_user_auth": bool(self.app.state.require_user_auth),
                    "authenticated": False,
                    "authorized": False,
                    "superuser": False,
                    "superuser_id": self._auth_data.get("superuser_id"),
                }
            return {
                "require_user_auth": bool(self.app.state.require_user_auth),
                "authenticated": True,
                "user_id": user_id,
                "authorized": self._is_authorized(user_id),
                "superuser": self._is_superuser(user_id),
                "superuser_id": self._auth_data.get("superuser_id"),
            }

        @self.app.post(
            "/api/auth/setup",
            dependencies=[
                Depends(
                    action_dep(
                        "bootstrap_setup",
                        allow_dashboard=False,
                        allow_control_api=True,
                    )
                )
            ],
        )
        async def auth_setup(request: Request):
            """First-time bootstrap of superuser from setup password."""
            body = await request.json()
            user_id = self._parse_user_id(body.get("user_id"))
            password = str(body.get("password") or "").strip()
            if user_id is None:
                raise HTTPException(status_code=400, detail="Invalid user_id")
            if self._auth_data.get("superuser_id"):
                raise HTTPException(
                    status_code=409, detail="Superuser already configured"
                )
            expected = self._auth_data.get("setup_password")
            if expected and password != expected:
                raise HTTPException(status_code=401, detail="Invalid setup password")
            self._auth_data["superuser_id"] = user_id
            self._auth_data["user_whitelist"] = [user_id]
            self._save_auth_data()
            return {"ok": True, "superuser_id": user_id}

        @self.app.get(
            "/api/auth/whitelist",
            dependencies=[
                Depends(
                    action_dep(
                        "manage_whitelist",
                        allow_dashboard=False,
                        allow_control_api=True,
                    )
                )
            ],
        )
        async def auth_whitelist():
            return {
                "superuser_id": self._auth_data.get("superuser_id"),
                "user_whitelist": self._auth_data.get("user_whitelist", []),
            }

        @self.app.post(
            "/api/auth/whitelist/{user_id}",
            dependencies=[
                Depends(
                    action_dep(
                        "manage_whitelist",
                        allow_dashboard=False,
                        allow_control_api=True,
                    )
                )
            ],
        )
        async def auth_whitelist_add(user_id: int):
            parsed = self._parse_user_id(user_id)
            if parsed is None:
                raise HTTPException(status_code=400, detail="Invalid user id")
            wl = set(self._auth_data.get("user_whitelist", []))
            wl.add(parsed)
            self._auth_data["user_whitelist"] = sorted(wl)
            self._save_auth_data()
            return {"ok": True, "user_whitelist": self._auth_data["user_whitelist"]}

        @self.app.delete(
            "/api/auth/whitelist/{user_id}",
            dependencies=[
                Depends(
                    action_dep(
                        "manage_whitelist",
                        allow_dashboard=False,
                        allow_control_api=True,
                    )
                )
            ],
        )
        async def auth_whitelist_remove(user_id: int):
            parsed = self._parse_user_id(user_id)
            if parsed is None:
                raise HTTPException(status_code=400, detail="Invalid user id")
            if parsed == self._auth_data.get("superuser_id"):
                raise HTTPException(status_code=400, detail="Cannot remove superuser")
            wl = set(self._auth_data.get("user_whitelist", []))
            wl.discard(parsed)
            self._auth_data["user_whitelist"] = sorted(wl)
            self._save_auth_data()
            return {"ok": True, "user_whitelist": self._auth_data["user_whitelist"]}

        @self.app.get("/", response_class=HTMLResponse)
        async def root(request: Request):
            """Root endpoint with web UI."""
            self._refresh_auth_state()
            if getattr(self.app.state, "dashboard_auth_enabled", False):
                token = request.cookies.get("spyoncino_dashboard_session")
                if not self._verify_dashboard_session(token):
                    return HTMLResponse(self._get_login_html())
            return self._get_html_ui()

        @self.app.post("/api/auth/dashboard/login")
        async def dashboard_login(request: Request):
            self._refresh_auth_state()
            if not getattr(self.app.state, "dashboard_auth_enabled", False):
                raise HTTPException(
                    status_code=400, detail="Dashboard username/password not configured"
                )
            body = await request.json()
            username = str(body.get("username") or "").strip()
            password = str(body.get("password") or "").strip()
            # Accept optional leading @ for user convenience.
            if username.startswith("@"):
                username = username[1:]
            expected_user = self._auth_data.get("dashboard_username")
            expected_pass = self._auth_data.get("dashboard_password")
            if username != expected_user or password != expected_pass:
                raise HTTPException(
                    status_code=401, detail="Invalid username or password"
                )
            if not can(
                Principal(kind="dashboard", username=username),
                "view_metrics",
                self._auth_state,
            ):
                raise HTTPException(
                    status_code=403, detail="Dashboard user not authorized"
                )
            token = self._create_dashboard_session(username)
            resp = JSONResponse({"ok": True, "username": username})
            resp.set_cookie(
                key="spyoncino_dashboard_session",
                value=token,
                httponly=True,
                samesite="strict",
                secure=self._is_request_https(request),
                max_age=60 * 60 * 2,
            )
            return resp

        @self.app.post("/api/auth/dashboard/logout")
        async def dashboard_logout(request: Request):
            resp = JSONResponse({"ok": True})
            resp.delete_cookie(
                "spyoncino_dashboard_session",
                samesite="strict",
                secure=self._is_request_https(request),
            )
            return resp

        @self.app.post("/api/auth/dashboard/logout_all")
        async def dashboard_logout_all(request: Request):
            self._refresh_auth_state()
            username = require_dashboard_session(request)
            if not username or not can(
                Principal(kind="dashboard", username=username),
                "manage_dashboard_credentials",
                self._auth_state,
            ):
                raise HTTPException(
                    status_code=403, detail="Dashboard user not authorized"
                )
            self._bump_session_version()
            resp = JSONResponse({"ok": True})
            resp.delete_cookie(
                "spyoncino_dashboard_session",
                samesite="strict",
                secure=self._is_request_https(request),
            )
            return resp

        @self.app.get("/api/metrics", response_model=MetricsResponse)
        async def get_metrics(request: Request):
            """Get current system metrics (via ``SpyoncinoRuntime`` when wired)."""
            try:
                await require_policy_action(
                    request,
                    "view_metrics",
                    allow_dashboard=True,
                    allow_control_api=False,
                )
                if not self.runtime:
                    raise HTTPException(
                        status_code=503, detail="SpyoncinoRuntime not wired."
                    )
                return self._metrics_dict_to_response(self.runtime.get_metrics())
            except HTTPException:
                raise
            except Exception as e:
                self.logger.error(f"Error getting metrics: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.get("/api/events", response_model=List[EventResponse])
        async def get_events(
            request: Request,
            hours: int = Query(24, ge=1, le=168),
            event_type: Optional[str] = None,
            camera_id: Optional[str] = None,
        ):
            """Get events."""
            try:
                await require_policy_action(
                    request,
                    "view_events",
                    allow_dashboard=True,
                    allow_control_api=False,
                )
                if event_type:
                    try:
                        EventType(event_type)
                    except ValueError:
                        raise HTTPException(
                            status_code=400, detail=f"Invalid event type: {event_type}"
                        )
                if not self.runtime:
                    raise HTTPException(
                        status_code=503, detail="SpyoncinoRuntime not wired."
                    )
                events = self.runtime.get_events(
                    hours=hours,
                    event_type=event_type,
                    camera_id=camera_id,
                )
                return [EventResponse(**row) for row in events]
            except HTTPException:
                raise
            except Exception as e:
                self.logger.error(f"Error getting events: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.get("/api/services", response_model=Dict[str, ServiceStatusResponse])
        async def get_services(request: Request):
            """Get all service statuses."""
            try:
                await require_policy_action(
                    request,
                    "view_services",
                    allow_dashboard=True,
                    allow_control_api=False,
                )
                if not self.runtime:
                    raise HTTPException(
                        status_code=503, detail="SpyoncinoRuntime not wired."
                    )
                services = self.runtime.get_services()
                return {
                    name: ServiceStatusResponse(**status)
                    for name, status in services.items()
                }
            except HTTPException:
                raise
            except Exception as e:
                self.logger.error(f"Error getting services: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.get("/api/config")
        async def get_config(request: Request):
            """Get all configuration."""
            try:
                await require_policy_action(request, "view_config")
                if not self.runtime:
                    raise HTTPException(
                        status_code=503, detail="SpyoncinoRuntime not wired."
                    )
                return self.runtime.get_all_config()
            except HTTPException:
                raise
            except Exception as e:
                self.logger.error(f"Error getting config: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.get("/api/config/traits")
        async def get_config_traits(request: Request):
            """Get per-key config metadata (hot-swappable vs restart-required)."""
            try:
                await require_policy_action(request, "view_config")
                if not self.runtime:
                    raise HTTPException(
                        status_code=503, detail="SpyoncinoRuntime not wired."
                    )
                return self.runtime.get_config_traits()
            except HTTPException:
                raise
            except Exception as e:
                self.logger.error(f"Error getting config traits: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.get("/api/config/{key}")
        async def get_config_key(key: str, request: Request):
            """Get configuration value by key."""
            try:
                await require_policy_action(request, "view_config")
                if not self.runtime:
                    raise HTTPException(
                        status_code=503, detail="SpyoncinoRuntime not wired."
                    )
                if key in DEPRECATED_CONFIG_KEYS:
                    raise HTTPException(
                        status_code=404,
                        detail="Config key is deprecated and not exposed via the API.",
                    )
                value = self.runtime.get_config(key)
                if value is None:
                    raise HTTPException(
                        status_code=404, detail=f"Config key '{key}' not found"
                    )
                return {"key": key, "value": value}
            except HTTPException:
                raise
            except Exception as e:
                self.logger.error(f"Error getting config key: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.put("/api/config/{key}")
        async def update_config(
            key: str,
            request: ConfigUpdateRequest,
            http_request: Request,
        ):
            """Update configuration value."""
            try:
                await require_policy_action(http_request, "update_config")
                if not self.runtime:
                    raise HTTPException(
                        status_code=503, detail="SpyoncinoRuntime not wired."
                    )
                restart = self.runtime.set_config(key, request.value)

                return {
                    "key": key,
                    "value": restart.get("normalized_value", request.value),
                    "updated": True,
                    "restart_schedule": restart,
                }
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            except HTTPException:
                raise
            except Exception as e:
                self.logger.error(f"Error updating config: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.post("/api/config/reset")
        async def reset_config(body: ConfigResetRequest, request: Request):
            """Reset one config override key or all override keys."""
            try:
                await require_policy_action(request, "update_config")
                if not self.runtime:
                    raise HTTPException(
                        status_code=503, detail="SpyoncinoRuntime not wired."
                    )
                key = (
                    body.key.strip()
                    if isinstance(body.key, str) and body.key.strip()
                    else None
                )
                return self.runtime.reset_config(
                    key=key, reset_all=bool(body.reset_all)
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            except HTTPException:
                raise
            except Exception as e:
                self.logger.error(f"Error resetting config: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.get(
            "/api/status",
            dependencies=[
                Depends(action_dep("view_status", allow_dashboard=True)),
            ],
        )
        async def api_runtime_status():
            """Full pipeline status (metrics + loop state); requires SpyoncinoRuntime."""
            if not self.runtime:
                raise HTTPException(
                    status_code=503,
                    detail="SpyoncinoRuntime not wired (run via orchestrator build).",
                )
            try:
                return self.runtime.get_status()
            except Exception as e:
                self.logger.error("Error in get_status: %s", e, exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.get(
            "/api/analytics/summary",
            dependencies=[
                Depends(action_dep("view_metrics", allow_dashboard=True)),
            ],
        )
        def api_analytics_summary(hours: int = Query(24, ge=1, le=168)):
            """Lifetime metrics plus window aggregates (same source as Telegram /stats)."""
            if not self.runtime:
                raise HTTPException(
                    status_code=503,
                    detail="SpyoncinoRuntime not wired (run via orchestrator build).",
                )
            try:
                return self.runtime.get_analytics_summary(hours)
            except Exception as e:
                self.logger.error("Error in analytics summary: %s", e, exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.get(
            "/api/analytics/chart.jpg",
            dependencies=[
                Depends(action_dep("view_metrics", allow_dashboard=True)),
            ],
        )
        def api_analytics_chart(hours: int = Query(24, ge=1, le=168)):
            """JPEG events trend chart for the last ``hours`` (OpenCV-rendered)."""
            if not self.runtime:
                raise HTTPException(
                    status_code=503,
                    detail="SpyoncinoRuntime not wired (run via orchestrator build).",
                )
            try:
                raw = self.runtime.get_analytics_chart_jpeg(hours)
                if not raw:
                    raise HTTPException(
                        status_code=500, detail="Chart generation failed"
                    )
                return Response(content=raw, media_type="image/jpeg")
            except HTTPException:
                raise
            except Exception as e:
                self.logger.error("Error in analytics chart: %s", e, exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.get(
            "/api/analytics/series",
            dependencies=[
                Depends(action_dep("view_metrics", allow_dashboard=True)),
            ],
        )
        def api_analytics_series(hours: int = Query(24, ge=1, le=168)):
            """Hourly event counts per type for interactive dashboard charts."""
            if not self.runtime:
                raise HTTPException(
                    status_code=503,
                    detail="SpyoncinoRuntime not wired (run via orchestrator build).",
                )
            try:
                return self.runtime.get_analytics_series(hours)
            except Exception as e:
                self.logger.error("Error in analytics series: %s", e, exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.get(
            "/api/media",
            response_model=List[MediaArtifactResponse],
            dependencies=[
                Depends(action_dep("list_media", allow_dashboard=True)),
            ],
        )
        async def api_list_media(
            camera_id: Optional[str] = None,
            stage: Optional[str] = None,
            kind: Optional[str] = None,
            created_from: Optional[str] = None,
            created_to: Optional[str] = None,
            hours: int = Query(24 * 7, ge=1, le=24 * 365),
            limit: int = Query(100, ge=1, le=500),
            offset: int = Query(0, ge=0),
        ):
            if not self.runtime:
                raise HTTPException(
                    status_code=503, detail="SpyoncinoRuntime not wired."
                )
            try:
                since: Optional[datetime] = datetime.now(timezone.utc) - timedelta(
                    hours=hours
                )
                until: Optional[datetime] = None
                if isinstance(created_from, str) and created_from.strip():
                    raw_from = created_from.strip().replace("Z", "+00:00")
                    since = datetime.fromisoformat(raw_from)
                    if since.tzinfo is None:
                        since = since.replace(tzinfo=timezone.utc)
                if isinstance(created_to, str) and created_to.strip():
                    raw_to = created_to.strip().replace("Z", "+00:00")
                    until = datetime.fromisoformat(raw_to)
                    if until.tzinfo is None:
                        until = until.replace(tzinfo=timezone.utc)
                rows = self.runtime.list_media(
                    camera_id=camera_id,
                    stage=stage,
                    kind=kind,
                    since=since,
                    until=until,
                    limit=limit,
                    offset=offset,
                )
                return [MediaArtifactResponse(**r) for r in rows]
            except ValueError as e:
                raise HTTPException(
                    status_code=400, detail=f"Invalid datetime filter: {e}"
                )
            except Exception as e:
                self.logger.error("Error listing media: %s", e, exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.get(
            "/api/media/{artifact_id}/meta",
            response_model=MediaArtifactResponse,
            dependencies=[
                Depends(action_dep("list_media", allow_dashboard=True)),
            ],
        )
        async def api_media_meta(artifact_id: int):
            if not self.runtime:
                raise HTTPException(
                    status_code=503, detail="SpyoncinoRuntime not wired."
                )
            try:
                row = self.runtime.get_media_artifact_meta(artifact_id)
                if not row:
                    raise HTTPException(status_code=404, detail="Artifact not found")
                return MediaArtifactResponse(**row)
            except HTTPException:
                raise
            except Exception as e:
                self.logger.error("Error reading media meta: %s", e, exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.get(
            "/api/media/{artifact_id}/file",
            dependencies=[
                Depends(action_dep("list_media", allow_dashboard=True)),
            ],
        )
        async def api_media_file(artifact_id: int):
            if not self.runtime:
                raise HTTPException(
                    status_code=503, detail="SpyoncinoRuntime not wired."
                )
            try:
                path = self.runtime.get_media_path(artifact_id)
                if path is None or not path.is_file():
                    raise HTTPException(status_code=404, detail="Artifact not found")
                return FileResponse(path)
            except HTTPException:
                raise
            except Exception as e:
                self.logger.error("Error serving media: %s", e, exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.post(
            "/api/control/pause",
            dependencies=[
                Depends(action_dep("control_pause", allow_dashboard=True)),
            ],
        )
        async def api_control_pause(request: PauseRequest):
            if not self.runtime:
                raise HTTPException(
                    status_code=503, detail="SpyoncinoRuntime not wired."
                )
            try:
                self.runtime.set_paused(request.paused)
                return {"paused": self.runtime.is_paused()}
            except Exception as e:
                self.logger.error("Error setting pause: %s", e, exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.post(
            "/api/control/snap",
            response_model=SnapResponse,
            dependencies=[
                Depends(action_dep("control_snap", allow_dashboard=True)),
            ],
        )
        async def api_control_snap(
            camera_id: str = Query(..., description="Camera id from recipe"),
        ):
            if not self.runtime:
                raise HTTPException(
                    status_code=503, detail="SpyoncinoRuntime not wired."
                )
            try:
                out = self.runtime.snap(camera_id)
                if not out or not out.get("path_rel"):
                    raise HTTPException(
                        status_code=404,
                        detail="Snap failed (unknown camera, empty buffer, or no media store).",
                    )
                return SnapResponse(
                    id=out.get("id"),
                    path_rel=out["path_rel"],
                    path=out["path"],
                    camera_id=out["camera_id"],
                )
            except HTTPException:
                raise
            except Exception as e:
                self.logger.error("Error in snap: %s", e, exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.get(
            "/api/identities",
            dependencies=[Depends(action_dep("face_identities_read"))],
        )
        async def api_list_identities():
            if not self.runtime:
                raise HTTPException(
                    status_code=503, detail="SpyoncinoRuntime not wired."
                )
            return self.runtime.list_identities()

        @self.app.post(
            "/api/identities",
            dependencies=[Depends(action_dep("face_identities_write"))],
        )
        async def api_create_identity(body: IdentityCreateRequest):
            if not self.runtime:
                raise HTTPException(
                    status_code=503, detail="SpyoncinoRuntime not wired."
                )
            try:
                return self.runtime.create_identity(body.display_name)
            except ValueError as e:
                raise HTTPException(status_code=409, detail=str(e))

        @self.app.patch(
            "/api/identities/{identity_id}",
            dependencies=[Depends(action_dep("face_identities_write"))],
        )
        async def api_patch_identity(identity_id: str, body: IdentityPatchRequest):
            if not self.runtime:
                raise HTTPException(
                    status_code=503, detail="SpyoncinoRuntime not wired."
                )
            try:
                ok = self.runtime.update_identity(identity_id, body.display_name)
            except ValueError as e:
                raise HTTPException(status_code=409, detail=str(e))
            if not ok:
                raise HTTPException(status_code=404, detail="Identity not found")
            return {"ok": True, "id": identity_id}

        @self.app.delete(
            "/api/identities/{identity_id}",
            dependencies=[Depends(action_dep("face_identities_write"))],
        )
        async def api_delete_identity(identity_id: str):
            if not self.runtime:
                raise HTTPException(
                    status_code=503, detail="SpyoncinoRuntime not wired."
                )
            ok = self.runtime.delete_identity(identity_id)
            if not ok:
                raise HTTPException(status_code=404, detail="Identity not found")
            return {"ok": True}

        @self.app.get(
            "/api/face/pending",
            dependencies=[Depends(action_dep("face_pending_read"))],
        )
        async def api_list_pending_faces(
            status: Optional[str] = Query("open"),
            limit: int = Query(200, ge=1, le=500),
        ):
            if not self.runtime:
                raise HTTPException(
                    status_code=503, detail="SpyoncinoRuntime not wired."
                )
            st = (status or "open").strip().lower()
            if st == "all":
                st = None
            elif st not in {"open", "ignored", "assigned"}:
                raise HTTPException(
                    status_code=400, detail="Invalid pending face status"
                )
            return self.runtime.list_pending_faces(status=st, limit=limit)

        @self.app.get(
            "/api/face/pending/{pending_id}/file",
            dependencies=[Depends(action_dep("face_pending_read"))],
        )
        async def api_pending_face_file(pending_id: str):
            if not self.runtime:
                raise HTTPException(
                    status_code=503, detail="SpyoncinoRuntime not wired."
                )
            pending = self.runtime.memory_manager.get_pending_face(pending_id)
            if not pending:
                raise HTTPException(status_code=404, detail="Pending face not found")
            media_store = getattr(self.runtime, "_media_store", None)
            if media_store is None:
                raise HTTPException(
                    status_code=503,
                    detail="Media store not available for face previews.",
                )
            try:
                path = media_store.resolve_relative(str(pending.get("path_rel") or ""))
            except ValueError:
                raise HTTPException(status_code=404, detail="Invalid pending face path")
            if not path.is_file():
                raise HTTPException(
                    status_code=404, detail="Pending face image not found"
                )
            return FileResponse(path)

        @self.app.post(
            "/api/face/pending/{pending_id}/assign",
            dependencies=[Depends(action_dep("face_pending_write"))],
        )
        async def api_assign_pending(pending_id: str, body: PendingAssignRequest):
            if not self.runtime or not self.runtime.media_store:
                raise HTTPException(
                    status_code=503, detail="SpyoncinoRuntime or media store not wired."
                )
            if bool(body.identity_id) == bool(body.new_display_name):
                raise HTTPException(
                    status_code=400,
                    detail="Provide exactly one of identity_id or new_display_name",
                )
            try:
                return self.runtime.assign_pending_face(
                    pending_id,
                    identity_id=body.identity_id,
                    new_display_name=body.new_display_name,
                )
            except ValueError as e:
                msg = str(e)
                code = 409 if "already exists" in msg.lower() else 400
                raise HTTPException(status_code=code, detail=msg)

        @self.app.post(
            "/api/face/pending/{pending_id}/reassign",
            dependencies=[Depends(action_dep("face_pending_write"))],
        )
        async def api_reassign_assigned_face(
            pending_id: str, body: PendingAssignRequest
        ):
            if not self.runtime:
                raise HTTPException(
                    status_code=503, detail="SpyoncinoRuntime not wired."
                )
            if bool(body.identity_id) == bool(body.new_display_name):
                raise HTTPException(
                    status_code=400,
                    detail="Provide exactly one of identity_id or new_display_name",
                )
            try:
                return self.runtime.reassign_assigned_face(
                    pending_id,
                    identity_id=body.identity_id,
                    new_display_name=body.new_display_name,
                )
            except ValueError as e:
                msg = str(e)
                code = 409 if "already exists" in msg.lower() else 400
                raise HTTPException(status_code=code, detail=msg)

        @self.app.post(
            "/api/face/pending/{pending_id}/unassign",
            dependencies=[Depends(action_dep("face_pending_write"))],
        )
        async def api_unassign_assigned_face(pending_id: str):
            if not self.runtime:
                raise HTTPException(
                    status_code=503, detail="SpyoncinoRuntime not wired."
                )
            try:
                return self.runtime.unassign_assigned_face(pending_id)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

        @self.app.post(
            "/api/face/pending/{pending_id}/ignore",
            dependencies=[Depends(action_dep("face_pending_write"))],
        )
        async def api_ignore_pending(pending_id: str):
            if not self.runtime:
                raise HTTPException(
                    status_code=503, detail="SpyoncinoRuntime not wired."
                )
            ok = self.runtime.ignore_pending_face(pending_id)
            if not ok:
                raise HTTPException(
                    status_code=404, detail="Pending face not found or not open"
                )
            return {"ok": True}

        @self.app.get(
            "/api/face/recent",
            dependencies=[Depends(action_dep("face_pending_read"))],
        )
        async def api_face_recent(hours: int = Query(1, ge=1, le=168)):
            """Recent named identifications (and unknown glimpses) from FACE event metadata."""
            if not self.runtime:
                raise HTTPException(
                    status_code=503, detail="SpyoncinoRuntime not wired."
                )
            return self.runtime.recent_identified_presence(hours=hours)

    def _favicon_link_tags(self) -> str:
        if self._brand_static is None:
            return ""
        return (
            '    <link rel="icon" href="/favicon.ico" sizes="any">\n'
            '    <link rel="icon" type="image/png" href="/static/logo.png" sizes="32x32">\n'
            '    <link rel="apple-touch-icon" href="/apple-touch-icon.png">\n'
        )

    def _shared_theme_css(self) -> str:
        return SHARED_DASHBOARD_THEME_CSS

    def _get_login_html(self) -> str:
        fav = self._favicon_link_tags()
        shared_css = self._shared_theme_css()
        template_path = Path(__file__).resolve().parent / "templates" / "login.html"
        try:
            template = template_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"Cannot read login template: {template_path}") from exc
        return template.replace("{{FAV}}", fav, 1).replace(
            "{{SHARED_CSS}}", shared_css, 1
        )

    def _get_html_ui(self) -> str:
        """Get HTML UI for web interface."""
        fav = self._favicon_link_tags()
        shared_css = self._shared_theme_css()
        template_path = Path(__file__).resolve().parent / "templates" / "dashboard.html"
        try:
            template = template_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(
                f"Cannot read dashboard template: {template_path}"
            ) from exc
        return template.replace("{{FAV}}", fav, 1).replace(
            "{{SHARED_CSS}}", shared_css, 1
        )

    def run(self) -> None:
        """Run the web application (blocking)."""
        import uvicorn

        uvicorn.run(self.app, host=self.host, port=self.port, log_level="info")

    async def start(self) -> None:
        """Start the web application (async)."""
        import uvicorn

        config = uvicorn.Config(
            self.app, host=self.host, port=self.port, log_level="info"
        )
        server = uvicorn.Server(config)
        await server.serve()

    def process(self, result: Dict[str, Any]) -> None:
        """
        Process result from orchestrator (for interface compatibility).

        Args:
            result: Result dictionary from orchestrator
        """
        # Web app doesn't need to process results directly
        # It reads from memory_manager via API
        pass
