"""
Standalone camera discovery service (separate from the main web dashboard).

Default listen port is 8001 so it does not collide with the surveillance UI (typically 8000).
Override with env SPYONCINO_DISCOVERY_PORT or --port.

Started via: ``spyoncino discover`` (see ``spyoncino.orchestrator.main``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Iterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field
import uvicorn

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

DEFAULT_PORT = 8001
ENV_PORT = "SPYONCINO_DISCOVERY_PORT"

_SENT = object()


def _next_event(it: Iterator[dict[str, object]]) -> object:
    try:
        return next(it)
    except StopIteration:
        return _SENT


def _discover_template_path() -> Path:
    return Path(__file__).resolve().parent / "templates" / "discover.html"


def _brand_static_dir() -> Path | None:
    bundled = Path(__file__).resolve().parent / "static"
    if (bundled / "logo.ico").is_file():
        return bundled
    return None


def _load_discover_page() -> str:
    path = _discover_template_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise RuntimeError(f"Cannot read discovery template: {path}") from e
    fav = ""
    static = _brand_static_dir()
    if static is not None:
        fav = (
            '    <link rel="icon" href="/favicon.ico" sizes="any">\n'
            '    <link rel="icon" type="image/png" href="/static/logo.png" sizes="32x32">\n'
        )
    return raw.replace("{{FAV}}", fav, 1).replace(
        "{{SHARED_CSS}}", SHARED_DASHBOARD_THEME_CSS, 1
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


app = FastAPI(title="Spyoncino camera discovery", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def root() -> str:
    return _load_discover_page()


_static_dir = _brand_static_dir()
if _static_dir is not None:

    @app.get("/favicon.ico")
    async def favicon() -> FileResponse:
        return FileResponse(_static_dir / "logo.ico", filename="favicon.ico")

    @app.get("/static/logo.png")
    async def logo_png() -> FileResponse:
        return FileResponse(_static_dir / "logo.png")


@app.post("/api/discover/run")
async def discover_run(body: DiscoverRequest) -> StreamingResponse:
    if not body.usb and not body.network:
        raise HTTPException(
            status_code=400, detail="Enable at least one of USB or network scanning."
        )
    manual = parse_host_list(body.hosts_text)
    if len(manual) > 128:
        raise HTTPException(status_code=400, detail="Too many manual hosts (max 128).")

    network_effective, warning_msg = plan_network_scan(
        body.usb, body.network, manual, body.scan_local_subnets
    )
    if body.network and not manual and not body.scan_local_subnets and not body.usb:
        raise HTTPException(
            status_code=400,
            detail="Add hosts, enable “scan local subnets”, or enable USB.",
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
        lan_result = LanMergeResult(merged_hosts=dedupe_hosts_preserve_order(manual))

    merged_hosts = lan_result.merged_hosts
    lan_notes = lan_result.messages

    if len(merged_hosts) > 512:
        raise HTTPException(
            status_code=400,
            detail="Too many targets after merging manual + LAN (max 512). Narrow subnets or lower max scan.",
        )

    if network_effective and not merged_hosts:
        if body.usb:
            network_effective = False
        else:
            detail = (
                "No RTSP targets. Add manual hosts, fix LAN scan, or enable USB-only. "
                + " ".join(lan_notes)
            )
            raise HTTPException(status_code=400, detail=detail.strip())

    tpl_n = len(RTSP_PATH_TEMPLATES_ALL)
    max_rtsp_probes = (
        min(3200, max(280, len(merged_hosts) * tpl_n)) if merged_hosts else 280
    )

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
        if did_lan_merge:
            yield (
                json.dumps(
                    {
                        "type": "lan_info",
                        "networks": lan_result.scanned_networks,
                        "candidate_ips": lan_result.candidate_ips,
                        "tcp_open": lan_result.tcp_open_count,
                        "truncated": lan_result.truncated,
                        "rtsp_tcp_port": body.rtsp_port,
                        "messages": lan_result.messages,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            ).encode("utf-8")
        while True:
            ev = await asyncio.to_thread(_next_event, it)
            if ev is _SENT:
                break
            line = json.dumps(ev, ensure_ascii=False) + "\n"
            yield line.encode("utf-8")

    return StreamingResponse(ndjson(), media_type="application/x-ndjson")


def _port_from_env() -> int:
    raw = os.environ.get(ENV_PORT, "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        p = int(raw)
    except ValueError:
        return DEFAULT_PORT
    return p if 1 <= p <= 65535 else DEFAULT_PORT


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="spyoncino discover",
        description="Spyoncino camera discovery web service (separate from the main dashboard).",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("SPYONCINO_DISCOVERY_HOST", "127.0.0.1"),
        help="Bind address (default 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=_port_from_env(),
        help=f"Port (default {DEFAULT_PORT}, or {ENV_PORT})",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    from .logging_redact import install_telegram_token_log_redaction

    install_telegram_token_log_redaction()
    uvicorn.run(
        "spyoncino.discovery_app:app",
        host=args.host,
        port=args.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
