from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from .discovery_lan import (
    LanMergeResult,
    dedupe_hosts_preserve_order,
    merge_manual_and_lan_hosts,
)
from .discovery_scan import (
    RTSP_PATH_TEMPLATES_ALL,
    iter_discovery_events,
    parse_host_list,
    plan_network_scan,
)
from .shared_theme_css import SHARED_DASHBOARD_THEME_CSS

_logger = logging.getLogger(__name__)
_SENT = object()


def _next_event(it: Iterator[dict[str, object]]) -> object:
    try:
        return next(it)
    except StopIteration:
        return _SENT


def _template_path() -> Path:
    return Path(__file__).resolve().parent / "templates" / "recipe_builder.html"


def _brand_static_dir() -> Optional[Path]:
    bundled = Path(__file__).resolve().parent / "static"
    if (bundled / "logo.ico").is_file():
        return bundled
    return None


def _load_page(*, show_bootstrap_start: bool = False) -> str:
    path = _template_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Cannot read recipe builder template: {path}") from exc
    fav = ""
    static = _brand_static_dir()
    if static is not None:
        fav = (
            '    <link rel="icon" href="/favicon.ico" sizes="any">\n'
            '    <link rel="icon" type="image/png" href="/static/logo.png" sizes="32x32">\n'
            '    <link rel="apple-touch-icon" href="/apple-touch-icon.png">\n'
        )
    if show_bootstrap_start:
        body_class = "mode-bootstrap"
        intro_sub = "Set <strong>Web app port</strong> to this page&rsquo;s port, then <strong>Save &amp; run</strong>."
        section4_heading = "Finish"
        primary_actions = (
            '            <div class="row actions-row">\n'
            '                <button class="btn btn-secondary" id="btnValidate" type="button">Validate</button>\n'
            '                <button class="btn btn-primary" id="btnSaveAndStart" type="button">Save &amp; run</button>\n'
            "            </div>\n"
        )
        bootstrap_script = "<script>window.__SPYONCINO_BOOTSTRAP__=true</script>"
    else:
        body_class = ""
        intro_sub = "Output goes under <code>data/config/</code>."
        section4_heading = "Validate &amp; save"
        primary_actions = (
            '            <div class="row actions-row">\n'
            '                <button class="btn btn-secondary" id="btnValidate" type="button">Validate</button>\n'
            '                <button class="btn btn-primary" id="btnSave" type="button">Save recipe</button>\n'
            "            </div>\n"
        )
        bootstrap_script = "<script>window.__SPYONCINO_BOOTSTRAP__=false</script>"
    return (
        raw.replace("{{FAV}}", fav, 1)
        .replace("{{SHARED_CSS}}", SHARED_DASHBOARD_THEME_CSS, 1)
        .replace("{{BOOTSTRAP_SCRIPT}}", bootstrap_script, 1)
        .replace("{{BODY_CLASS}}", body_class, 1)
        .replace("{{INTRO_SUB}}", intro_sub, 1)
        .replace("{{SECTION4_HEADING}}", section4_heading, 1)
        .replace("{{PRIMARY_ACTIONS}}", primary_actions, 1)
    )


class DiscoverRequest(BaseModel):
    usb: bool = True
    usb_max_index: int = Field(10, ge=1, le=32)
    network: bool = False
    hosts_text: str = Field("", max_length=8000)
    scan_local_subnets: bool = False
    extra_subnets_text: str = Field("", max_length=2000)
    max_scan_hosts: int = Field(254, ge=8, le=1024)
    tcp_probe_timeout: float = Field(0.35, ge=0.1, le=2.0)
    rtsp_port: int = Field(554, ge=1, le=65535)
    rtsp_user: str = Field("", max_length=256)
    rtsp_password: str = Field("", max_length=256)
    timeout_sec: float = Field(3.5, ge=1.0, le=30.0)
    memory_seconds: int = Field(3, ge=1, le=120)


class RecipeInput(BaseModel):
    enabled: bool = True
    cam_id: str = Field(..., min_length=1, max_length=80)
    source_type: str = Field(..., pattern=r"^(usb|rtsp|ip)$")
    source_value: str = Field(..., min_length=1, max_length=1024)
    memory_seconds: int = Field(3, ge=1, le=120)


class RecipeSaveRequest(BaseModel):
    recipe_name: str = Field(..., min_length=1, max_length=80)
    set_default: bool = True
    patrol_time: float = Field(15.0, ge=0.2, le=3600.0)
    data_root: str = Field("data", min_length=1, max_length=255)
    sqlite_path: str = Field("spyoncino.db", min_length=1, max_length=255)
    secrets_path: str = Field("data/config/secrets.yaml", min_length=1, max_length=255)
    input_items: List[RecipeInput] = Field(default_factory=list)
    media_root: str = Field("media", min_length=1, max_length=255)
    media_retention_days: int = Field(14, ge=0, le=3650)
    media_max_total_mb: float = Field(2048, ge=0, le=1024 * 1024)
    media_max_files_per_camera: int = Field(500, ge=0, le=100000)
    media_retention_every_n_cycles: int = Field(120, ge=0, le=100000)
    event_log_retention_days: int = Field(3, ge=0, le=3650)
    event_log_retention_every_n_cycles: int = Field(120, ge=0, le=100000)
    use_motion: bool = True
    motion_threshold: int = Field(10, ge=1, le=100)
    use_detector: bool = True
    detector_weights: str = Field("weights/yolov8n.pt", min_length=1, max_length=512)
    conf_threshold: float = Field(0.25, ge=0.01, le=1.0)
    iou_threshold: float = Field(0.6, ge=0.01, le=1.0)
    batch_size: int = Field(16, ge=1, le=128)
    use_face_identification: bool = False
    face_gallery_path: str = Field("data/face_gallery", min_length=1, max_length=255)
    face_detector_backend: str = Field("ssd", min_length=1, max_length=64)
    face_model_name: str = Field("Facenet", min_length=1, max_length=64)
    face_align: bool = True
    face_distance_metric: str = Field("cosine", min_length=1, max_length=32)
    face_match_threshold: float = Field(0.35, ge=0.0, le=2.0)
    face_champion_frame_policy: str = Field("area", min_length=1, max_length=32)
    face_recognition_cooldown_seconds_per_identity: int = Field(600, ge=0, le=86400)
    face_unknown_prompt_cooldown_seconds: int = Field(120, ge=0, le=86400)
    face_pending_ttl_days: int = Field(14, ge=0, le=3650)
    face_max_exemplars_per_identity: int = Field(30, ge=1, le=1000)
    web_host: str = Field("127.0.0.1", min_length=1, max_length=255)
    web_port: int = Field(8000, ge=1, le=65535)
    enable_telegram: bool = False
    notify_on_preproc: List[str] = Field(default_factory=lambda: ["text"])
    notify_on_detection: List[str] = Field(default_factory=lambda: ["gif"])
    telegram_notification_rate_limit: int = Field(3, ge=1, le=120)
    telegram_outbound_strategy: str = Field("normal", min_length=1, max_length=16)
    telegram_gif_fps: int = Field(10, ge=1, le=60)
    telegram_gif_duration: int = Field(3, ge=1, le=60)
    telegram_video_fps: int = Field(10, ge=1, le=60)
    telegram_video_duration: int = Field(3, ge=1, le=60)
    telegram_video_format: str = Field("mp4", min_length=1, max_length=8)
    telegram_max_file_size_mb: float = Field(50.0, ge=1.0, le=2000.0)
    telegram_api_base_url: str = Field(
        "http://127.0.0.1:8000", min_length=1, max_length=300
    )


def _slug_name(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", name.strip())
    slug = slug.strip("._-").lower()
    return slug[:64]


def _valid_notify_modes(modes: List[str]) -> List[str]:
    allowed = {"text", "gif", "video"}
    cleaned: List[str] = []
    for raw in modes:
        s = str(raw).strip().lower()
        if not s:
            continue
        if s not in allowed:
            raise ValueError(f"Invalid notify mode {raw!r}. Allowed: text, gif, video")
        if s not in cleaned:
            cleaned.append(s)
    return cleaned


def _build_recipe(body: RecipeSaveRequest) -> Dict[str, Any]:
    active_inputs = [i for i in body.input_items if i.enabled]
    if not active_inputs:
        raise ValueError("Select at least one camera input before saving.")
    recipe: Dict[str, Any] = {
        "patrol_time": float(body.patrol_time),
        "data_root": body.data_root.strip(),
        "sqlite_path": body.sqlite_path.strip(),
        "secrets_path": body.secrets_path.strip(),
        "media": {
            "root": body.media_root.strip(),
            "retention_days": int(body.media_retention_days),
            "max_total_mb": float(body.media_max_total_mb),
            "max_files_per_camera": int(body.media_max_files_per_camera),
            "retention_every_n_cycles": int(body.media_retention_every_n_cycles),
        },
        "event_log": {
            "retention_days": int(body.event_log_retention_days),
            "retention_every_n_cycles": int(body.event_log_retention_every_n_cycles),
        },
        "inputs": [],
        "preproc": [],
        "inference": [],
        "postproc": [],
        "interfaces": [
            {
                "name": "web",
                "class": "webapp",
                "params": {
                    "memory_manager": None,
                    "host": body.web_host.strip(),
                    "port": int(body.web_port),
                    "config": {},
                },
            }
        ],
    }

    for item in active_inputs:
        source: Any = item.source_value.strip()
        if item.source_type == "usb":
            try:
                source = int(source)
            except ValueError as exc:
                raise ValueError(
                    f"USB source for {item.cam_id} must be numeric."
                ) from exc
        recipe["inputs"].append(
            {
                "name": item.cam_id,
                "class": "camera",
                "params": {
                    "cam_id": item.cam_id.strip(),
                    "type": item.source_type,
                    "source": source,
                    "memory_seconds": int(item.memory_seconds),
                },
            }
        )

    if body.use_motion:
        recipe["preproc"].append(
            {
                "name": "motion_detector",
                "class": "motion",
                "params": {"threshold": int(body.motion_threshold)},
            }
        )
    if body.use_detector:
        recipe["inference"].append(
            {
                "name": "person_detector",
                "class": "detector",
                "params": {
                    "weights": body.detector_weights.strip(),
                    "conf_threshold": float(body.conf_threshold),
                    "iou_threshold": float(body.iou_threshold),
                    "batch_size": int(body.batch_size),
                    "alarmed_classes": ["person"],
                },
            }
        )
    if body.use_face_identification:
        recipe["postproc"].append(
            {
                "name": "face_identification",
                "class": "face_identification",
                "params": {
                    "enabled": True,
                    "gallery_path": body.face_gallery_path.strip(),
                    "detector_backend": body.face_detector_backend.strip(),
                    "model_name": body.face_model_name.strip(),
                    "align": bool(body.face_align),
                    "distance_metric": body.face_distance_metric.strip(),
                    "match_threshold": float(body.face_match_threshold),
                    "champion_frame_policy": body.face_champion_frame_policy.strip(),
                    "recognition_cooldown_seconds_per_identity": int(
                        body.face_recognition_cooldown_seconds_per_identity
                    ),
                    "unknown_prompt_cooldown_seconds": int(
                        body.face_unknown_prompt_cooldown_seconds
                    ),
                    "pending_ttl_days": int(body.face_pending_ttl_days),
                    "max_exemplars_per_identity": int(
                        body.face_max_exemplars_per_identity
                    ),
                },
            }
        )
    if body.enable_telegram:
        recipe["interfaces"].append(
            {
                "name": "telegram_bot",
                "class": "telegram",
                "params": {
                    "memory_manager": None,
                    "config": {
                        "api_base_url": body.telegram_api_base_url.strip(),
                        "notify_on_preproc": _valid_notify_modes(
                            body.notify_on_preproc
                        ),
                        "notify_on_detection": _valid_notify_modes(
                            body.notify_on_detection
                        ),
                        "gif": {
                            "fps": int(body.telegram_gif_fps),
                            "duration": int(body.telegram_gif_duration),
                        },
                        "video": {
                            "fps": int(body.telegram_video_fps),
                            "duration": int(body.telegram_video_duration),
                            "format": body.telegram_video_format.strip(),
                        },
                        "max_file_size_mb": float(body.telegram_max_file_size_mb),
                        "notification_rate_limit": int(
                            body.telegram_notification_rate_limit
                        ),
                        "outbound_strategy": body.telegram_outbound_strategy.strip(),
                    },
                },
            }
        )
    return recipe


def _persist_recipe_body(
    body: RecipeSaveRequest, *, force_default: bool = False
) -> Dict[str, Any]:
    """
    Write recipe YAML under data/config/. When ``force_default``, always write recipe.yaml.
    """
    slug = _slug_name(body.recipe_name)
    if not slug:
        raise ValueError("Recipe name must include letters or numbers.")
    recipe = _build_recipe(body)
    folder = (Path.cwd() / "data" / "config").resolve()
    folder.mkdir(parents=True, exist_ok=True)
    named_path = folder / f"{slug}.yaml"
    with open(named_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(recipe, f, sort_keys=False, allow_unicode=False)
    default_path: Optional[Path] = None
    if force_default or body.set_default:
        default_path = folder / "recipe.yaml"
        with open(default_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(recipe, f, sort_keys=False, allow_unicode=False)
    return {
        "ok": True,
        "saved_path": str(named_path),
        "default_path": str(default_path) if default_path else None,
        "run_command": "spyoncino data/config/recipe.yaml",
    }


def create_app(*, bootstrap_launch: bool = False) -> FastAPI:
    app = FastAPI(title="Spyoncino recipe builder", version="0.1.0")

    @app.get("/health")
    async def health() -> Dict[str, str]:
        # Distinct from main dashboard so the bootstrap page can detect handoff.
        return {"status": "ok", "service": "recipe-builder"}

    @app.get("/", response_class=HTMLResponse)
    async def root() -> str:
        return _load_page(show_bootstrap_start=bootstrap_launch)

    static_dir = _brand_static_dir()
    if static_dir is not None:

        @app.get("/favicon.ico")
        async def favicon() -> FileResponse:
            return FileResponse(static_dir / "logo.ico", filename="favicon.ico")

        @app.get("/static/logo.png")
        async def logo_png() -> FileResponse:
            return FileResponse(static_dir / "logo.png")

        @app.get("/apple-touch-icon.png")
        async def apple_touch_icon() -> FileResponse:
            return FileResponse(static_dir / "logo.png")

    @app.post("/api/discover/run")
    async def discover_run(body: DiscoverRequest) -> StreamingResponse:
        if not body.usb and not body.network:
            raise HTTPException(
                status_code=400,
                detail="Enable at least one of USB or network scanning.",
            )
        manual = parse_host_list(body.hosts_text)
        network_effective, warning_msg = plan_network_scan(
            body.usb, body.network, manual, body.scan_local_subnets
        )
        did_lan_merge = bool(network_effective and body.scan_local_subnets)
        if did_lan_merge:
            lan_result: LanMergeResult = await asyncio.to_thread(
                merge_manual_and_lan_hosts,
                manual,
                True,
                body.extra_subnets_text,
                body.rtsp_port,
                body.tcp_probe_timeout,
                body.max_scan_hosts,
            )
        else:
            lan_result = LanMergeResult(
                merged_hosts=dedupe_hosts_preserve_order(manual)
            )
        merged_hosts = lan_result.merged_hosts
        tpl_n = len(RTSP_PATH_TEMPLATES_ALL)
        max_rtsp_probes = min(3200, max(280, len(merged_hosts) * tpl_n))
        it = iter_discovery_events(
            usb=body.usb,
            usb_max_index=body.usb_max_index,
            network=network_effective,
            hosts=merged_hosts,
            rtsp_port=body.rtsp_port,
            rtsp_user=body.rtsp_user,
            rtsp_password=body.rtsp_password,
            timeout_sec=body.timeout_sec,
            memory_seconds=body.memory_seconds,
            max_rtsp_probes=max_rtsp_probes,
        )

        async def ndjson():
            if warning_msg:
                yield (
                    json.dumps(
                        {"type": "warning", "message": warning_msg}, ensure_ascii=False
                    )
                    + "\n"
                ).encode("utf-8")
            while True:
                ev = await asyncio.to_thread(_next_event, it)
                if ev is _SENT:
                    break
                yield (json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8")

        return StreamingResponse(ndjson(), media_type="application/x-ndjson")

    @app.post("/api/recipe/validate")
    async def validate_recipe(body: RecipeSaveRequest) -> Dict[str, Any]:
        errors: List[str] = []
        slug = _slug_name(body.recipe_name)
        if not slug:
            errors.append("Recipe name must include letters or numbers.")
        try:
            _build_recipe(body)
        except ValueError as exc:
            errors.append(str(exc))
        return {"ok": len(errors) == 0, "errors": errors, "recipe_slug": slug}

    @app.post("/api/recipe/save")
    async def save_recipe(body: RecipeSaveRequest) -> Dict[str, Any]:
        try:
            return _persist_recipe_body(body, force_default=False)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if bootstrap_launch:

        @app.post("/api/recipe/save-and-launch")
        async def save_and_launch(
            body: RecipeSaveRequest, request: Request
        ) -> Dict[str, Any]:
            try:
                result = _persist_recipe_body(body, force_default=True)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            request.app.state.bootstrap_launch_requested = True
            srv = getattr(request.app.state, "bootstrap_server", None)
            if srv is not None:
                srv.should_exit = True
            result["message"] = (
                "Spyoncino is starting in this process; reload the dashboard URL shortly."
            )
            return result

    return app


app = create_app()


async def run_bootstrap_until_launch(host: str, port: int) -> bool:
    """
    Serve the recipe builder until the user clicks **Save & start Spyoncino** or the server stops.

    Returns:
        ``True`` if ``data/config/recipe.yaml`` was written and launch was requested (run orchestrator next).
        ``False`` if the server exited without launch (e.g. Ctrl+C).
    """
    app_local = create_app(bootstrap_launch=True)
    config = uvicorn.Config(app_local, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    app_local.state.bootstrap_server = server
    app_local.state.bootstrap_launch_requested = False
    await server.serve()
    return bool(getattr(app_local.state, "bootstrap_launch_requested", False))


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="spyoncino recipe-builder",
        description="Recipe builder web UI (default port 8002). Saves under data/config/.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address")
    parser.add_argument("--port", type=int, default=8002, help="Port for builder UI")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    _logger.info("Recipe builder at http://%s:%s", args.host, args.port)
    uvicorn.run(
        create_app(bootstrap_launch=False),
        host=args.host,
        port=args.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
