from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Principal:
    kind: str  # "telegram" | "dashboard"
    user_id: Optional[int] = None
    username: Optional[str] = None


@dataclass
class AuthState:
    setup_password: Optional[str]
    superuser_id: Optional[int]
    user_whitelist: List[int]
    dashboard_username: Optional[str]
    dashboard_password: Optional[str]


def _parse_user_id(value: Any) -> Optional[int]:
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit():
        out = int(value)
        return out if out > 0 else None
    return None


def auth_state_from_config(auth: Dict[str, Any]) -> AuthState:
    raw = auth if isinstance(auth, dict) else {}
    superuser_id = _parse_user_id(raw.get("superuser_id"))
    wl: List[int] = []
    raw_wl = raw.get("user_whitelist", [])
    if isinstance(raw_wl, list):
        for v in raw_wl:
            parsed = _parse_user_id(v)
            if parsed is not None:
                wl.append(parsed)
    setup_password = str(raw.get("setup_password") or "").strip() or None
    dashboard_username = str(raw.get("dashboard_username") or "").strip() or None
    dashboard_password = str(raw.get("dashboard_password") or "").strip() or None
    return AuthState(
        setup_password=setup_password,
        superuser_id=superuser_id,
        user_whitelist=sorted(set(wl)),
        dashboard_username=dashboard_username,
        dashboard_password=dashboard_password,
    )


def can(principal: Principal, action: str, state: AuthState) -> bool:
    admin_actions = {
        "manage_whitelist",
        "bootstrap_setup",
        "manage_dashboard_credentials",
    }
    control_actions = {
        "view_status",
        "list_media",
        "control_pause",
        "control_snap",
        "trigger_test",
        "view_metrics",
        "view_events",
        "view_services",
        "view_config",
        "update_config",
        "face_identities_read",
        "face_identities_write",
        "face_pending_read",
        "face_pending_write",
    }

    if principal.kind == "telegram":
        uid = principal.user_id
        if uid is None:
            return False
        is_super = state.superuser_id is not None and uid == state.superuser_id
        if action in admin_actions:
            if action == "bootstrap_setup":
                return state.superuser_id is None
            return is_super
        if is_super:
            return True
        if action in control_actions:
            if not state.user_whitelist:
                return True
            return uid in state.user_whitelist
        return False

    if principal.kind == "dashboard":
        username = principal.username or ""
        if not username:
            return False
        if action in admin_actions:
            return bool(
                state.dashboard_username and username == state.dashboard_username
            )
        if action in control_actions:
            # Current dashboard model is single local account; authenticated user can operate UI.
            return bool(
                state.dashboard_username and username == state.dashboard_username
            )
        return False

    return False
