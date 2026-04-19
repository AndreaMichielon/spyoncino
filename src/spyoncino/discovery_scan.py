"""
Camera discovery for USB indices and common RTSP URL patterns (local network tooling).

Produces recipe-shaped snippets for ``data/config/recipe.yaml`` inputs.

**Scope vs vendor tools (e.g. Hikvision SADP / Dahua ConfigTool):** Those apps use
vendor-specific **UDP discovery** (often broadcast/multicast on fixed ports) and sometimes
talk to devices that have **wrong IP/subnet** because discovery is layer-2 / broadcast
before RTSP. This tool is **RTSP-first**: it needs a routable IP (manual, LAN TCP scan,
or future ONVIF). It does **not** send proprietary “magic” packets.

**RTSP paths:** Base + extended vendor URL patterns are **always** tried (see
``RTSP_PATH_TEMPLATES_ALL``). Failed RTSP probes are **always** listed for debugging
(wrong path vs login). **ONVIF WS-Discovery** (UDP 3702) is a possible future add-on.
**Per-vendor SDKs** (Hikvision ISAPI, etc.) are out of scope here.
"""

from __future__ import annotations

import logging
import re
import time
from contextlib import suppress
from typing import Any, Iterator, Optional
from urllib.parse import quote

import cv2
import yaml

_logger = logging.getLogger(__name__)

# Cap total RTSP probes per request to avoid long runs / accidental wide scans.
_MAX_RTSP_PROBES_DEFAULT = 280

# (label, path) — common vendor / generic RTSP paths (port usually 554).
RTSP_PATH_TEMPLATES: list[tuple[str, str]] = [
    ("generic_stream1", "/stream1"),
    ("hikvision_main", "/Streaming/Channels/101"),
    ("hikvision_sub", "/Streaming/Channels/102"),
    ("hikvision_h264", "/h264/ch1/main/av_stream"),
    ("dahua_main", "/cam/realmonitor?channel=1&subtype=0"),
    ("dahua_sub", "/cam/realmonitor?channel=1&subtype=1"),
    ("live", "/live"),
    ("live_main", "/live/main"),
    ("unicast", "/unicast/c1/s0/live"),
    ("onvif", "/onvif1"),
    ("axis", "/axis-media/media.amp"),
    ("tp_link", "/stream1"),
    ("foscam", "/videoMain"),
    ("mpeg4", "/mpeg4"),
]

# Additional vendor / device patterns (always merged into ``RTSP_PATH_TEMPLATES_ALL``).
RTSP_PATH_TEMPLATES_EXTENDED: list[tuple[str, str]] = [
    ("hikvision_ch1", "/Streaming/Channels/1"),
    ("hikvision_201", "/Streaming/Channels/201"),
    ("reolink_main", "/h264Preview_01_main"),
    ("reolink_sub", "/h264Preview_01_sub"),
    ("ubiquiti_s0", "/s0"),
    ("ubiquiti_s1", "/s1"),
    ("generic_ch0", "/ch0"),
    ("generic_ch1", "/ch1"),
    ("amcrest_like", "/cam/realmonitor?channel=1&subtype=0&unicast=false"),
    ("tplink_s2", "/stream2"),
    ("vivotek", "/live1s1.sdp"),
    ("axis_h264", "/axis-media/media.amp?videocodec=h264"),
    ("sony", "/media/video1"),
    ("sv3c", "/11"),
    ("grandstream", "/cam/realmonitor?channel=0&subtype=0"),
    ("panasonic", "/MediaInput/h264"),
    ("generic_mp4", "/mp4"),
]

RTSP_PATH_TEMPLATES_ALL: list[tuple[str, str]] = (
    RTSP_PATH_TEMPLATES + RTSP_PATH_TEMPLATES_EXTENDED
)


def _recipe_usb(cam_id: str, index: int, memory_seconds: int = 3) -> str:
    block = {
        "name": cam_id,
        "class": "camera",
        "params": {
            "cam_id": cam_id,
            "type": "usb",
            "source": index,
            "memory_seconds": memory_seconds,
        },
    }
    return yaml.dump([block], sort_keys=False, default_flow_style=False).rstrip()


def _recipe_rtsp(cam_id: str, source_url: str, memory_seconds: int = 3) -> str:
    block = {
        "name": cam_id,
        "class": "camera",
        "params": {
            "cam_id": cam_id,
            "type": "rtsp",
            "source": source_url,
            "memory_seconds": memory_seconds,
        },
    }
    return yaml.dump([block], sort_keys=False, default_flow_style=False).rstrip()


def _slug(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_]+", "_", s.strip()).strip("_")
    return s[:48] if s else "camera"


NETWORK_SKIPPED_NO_HOSTS_MSG = (
    "Network scan skipped — no hosts parsed. Add one IP/hostname per line "
    "under RTSP hosts, or uncheck Network (RTSP)."
)


def plan_network_scan(
    usb: bool,
    network_requested: bool,
    hosts: list[str],
    scan_local_subnets: bool = False,
) -> tuple[bool, str | None]:
    """
    Decide whether to run RTSP probes. If Network is on but the host list is empty
    and USB is also enabled, skip network and return a warning instead of erroring.
    If ``scan_local_subnets`` is set, hosts may be filled later from a LAN scan.
    """
    if not network_requested:
        return False, None
    if hosts:
        return True, None
    if scan_local_subnets:
        return True, None
    if usb:
        return False, NETWORK_SKIPPED_NO_HOSTS_MSG
    return False, None


def parse_host_list(text: str) -> list[str]:
    """Split comma/newline-separated host IPs or hostnames; strip and drop empties."""
    out: list[str] = []
    for part in re.split(r"[\s,;]+", text or ""):
        p = part.strip()
        if p:
            out.append(p)
    return out


def build_rtsp_url(
    host: str,
    port: int,
    path: str,
    user: str = "",
    auth_plaintext: str = "",
) -> str:
    path = path if path.startswith("/") else f"/{path}"
    if user:
        u = quote(str(user), safe="")
        p = quote(str(auth_plaintext or ""), safe="")
        return f"rtsp://{u}:{p}@{host}:{port}{path}"
    return f"rtsp://{host}:{port}{path}"


def mask_rtsp_url_for_display(url: str) -> str:
    """Hide password in rtsp://user:pass@host for UI/logging."""
    return re.sub(r"(rtsp://[^:]+:)([^@]+)(@)", r"\1***\3", url, count=1)


def rtsp_failure_hint(error: str, had_credentials: bool) -> str:
    """
    Human-readable hint. OpenCV/FFmpeg do not expose RTSP 401 distinctly; auth failures
    often look like not_opened or no_frame.
    """
    err = (error or "").strip()
    if err == "not_opened":
        msg = (
            "Stream did not open — host/port unreachable, wrong path, firewall, "
            "or RTSP rejected (including bad username/password)."
        )
    elif err == "no_frame":
        msg = (
            "Session opened but no video frame — timeout, codec, or auth/session issue "
            "(wrong credentials can look like this too)."
        )
    else:
        msg = f"Probe error: {err}"

    if had_credentials:
        msg += (
            " If login should work, confirm user/password in the camera app or VLC; "
            "this tool cannot distinguish “wrong password” from “wrong URL path.”"
        )
    return msg


def probe_usb_index(index: int, timeout_sec: float = 2.5) -> Optional[dict[str, Any]]:
    """
    Try to open a USB capture index and read one frame.
    Returns a result dict or None if not a usable camera.
    """
    cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        with suppress(Exception):
            cap.release()
        return None

    t0 = time.monotonic()
    ok, frame = False, None
    while time.monotonic() - t0 < timeout_sec:
        ok, frame = cap.read()
        if ok and frame is not None and getattr(frame, "size", 0) > 0:
            break
        time.sleep(0.04)

    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps_r = cap.get(cv2.CAP_PROP_FPS)
    fps: Optional[int] = int(fps_r) if fps_r and fps_r > 1 else None

    try:
        backend = cap.getBackendName()
    except Exception:
        backend = None

    cap.release()

    if not ok or frame is None or frame.size == 0:
        return None
    if w <= 0 or h <= 0:
        return None

    cam_id = f"usb_{index}"
    return {
        "source": "usb",
        "kind": "camera",
        "success": True,
        "label": f"USB index {index}",
        "usb_index": index,
        "width": w,
        "height": h,
        "fps": fps,
        "backend": backend,
        "recipe_cam_id": cam_id,
        "recipe_yaml": _recipe_usb(cam_id, index),
        "display_url": f"device index {index}",
    }


def probe_rtsp_url(url: str, timeout_sec: float = 4.0) -> dict[str, Any]:
    """
    Try to read one frame from an RTSP URL. Returns ok + metrics or error.
    """
    t_start = time.monotonic()
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        with suppress(Exception):
            cap.release()
        cap = cv2.VideoCapture(url)
    if not cap.isOpened():
        with suppress(Exception):
            cap.release()
        return {
            "ok": False,
            "error": "not_opened",
            "elapsed_ms": int((time.monotonic() - t_start) * 1000),
        }

    ok, frame = False, None
    while time.monotonic() - t_start < timeout_sec:
        ok, frame = cap.read()
        if ok and frame is not None and frame.size > 0:
            break
        time.sleep(0.06)

    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps_r = cap.get(cv2.CAP_PROP_FPS)
    fps: Optional[int] = int(fps_r) if fps_r and fps_r > 1 else None

    with suppress(Exception):
        cap.release()

    elapsed_ms = int((time.monotonic() - t_start) * 1000)
    success = bool(ok and frame is not None and frame.size > 0 and w > 0 and h > 0)

    if not success:
        return {"ok": False, "error": "no_frame", "elapsed_ms": elapsed_ms}

    return {
        "ok": True,
        "width": w,
        "height": h,
        "fps": fps,
        "elapsed_ms": elapsed_ms,
    }


def iter_discovery_events(
    *,
    usb: bool,
    usb_max_index: int,
    network: bool,
    hosts: list[str],
    rtsp_port: int,
    rtsp_user: str,
    rtsp_password: str,
    timeout_sec: float,
    memory_seconds: int,
    max_rtsp_probes: int = _MAX_RTSP_PROBES_DEFAULT,
) -> Iterator[dict[str, Any]]:
    """
    Yield progress / result / done events as dicts (serialized to NDJSON by the caller).

    Each result item includes recipe-ready YAML for successful probes.
    """
    yield {
        "type": "start",
        "usb": usb,
        "network": network,
        "host_count": len(hosts) if network else 0,
    }

    if usb:
        yield {
            "type": "phase",
            "phase": "usb",
            "message": "Scanning local USB indices…",
        }
        total = max(0, min(usb_max_index, 32))
        for idx in range(total):
            yield {
                "type": "progress",
                "phase": "usb",
                "current": idx + 1,
                "total": total,
                "label": f"USB {idx}",
            }
            try:
                item = probe_usb_index(idx, timeout_sec=timeout_sec)
            except Exception as e:
                _logger.debug("USB probe %s failed: %s", idx, e)
                item = None
            if item:
                item["memory_seconds"] = memory_seconds
                yield {"type": "result", "item": item}

    had_rtsp_credentials = bool(
        (rtsp_user or "").strip() or (rtsp_password or "").strip()
    )

    if network and hosts:
        templates = RTSP_PATH_TEMPLATES_ALL
        planned: list[tuple[str, str, str, str]] = []
        for host in hosts:
            for label, path in templates:
                url = build_rtsp_url(host, rtsp_port, path, rtsp_user, rtsp_password)
                planned.append((host, label, path, url))
                if len(planned) >= max_rtsp_probes:
                    break
            if len(planned) >= max_rtsp_probes:
                break

        yield {
            "type": "phase",
            "phase": "network",
            "message": f"Trying RTSP paths on {len(hosts)} host(s), {len(planned)} URL(s)…",
        }
        total = len(planned)
        for i, (host, tmpl_label, path, url) in enumerate(planned):
            yield {
                "type": "progress",
                "phase": "network",
                "current": i + 1,
                "total": total,
                "label": f"{host} · {tmpl_label}",
            }
            try:
                pr = probe_rtsp_url(url, timeout_sec=timeout_sec)
            except Exception as e:
                _logger.debug("RTSP probe failed: %s", e)
                pr = {"ok": False, "error": str(e)}

            if pr.get("ok"):
                safe_id = _slug(f"rtsp_{host}_{tmpl_label}")
                cam_id = safe_id
                item = {
                    "source": "network",
                    "kind": "camera",
                    "success": True,
                    "label": f"{host} ({tmpl_label})",
                    "host": host,
                    "rtsp_template": tmpl_label,
                    "path": path,
                    "width": pr.get("width"),
                    "height": pr.get("height"),
                    "fps": pr.get("fps"),
                    "rtsp_url": url,
                    "display_url": mask_rtsp_url_for_display(url),
                    "recipe_cam_id": cam_id,
                    "recipe_yaml": _recipe_rtsp(cam_id, url, memory_seconds),
                    "memory_seconds": memory_seconds,
                    "probe_ms": pr.get("elapsed_ms"),
                }
                yield {"type": "result", "item": item}
            else:
                err = str(pr.get("error") or "unknown")
                item = {
                    "source": "network",
                    "kind": "probe_failed",
                    "success": False,
                    "label": f"{host} · {tmpl_label}",
                    "host": host,
                    "rtsp_template": tmpl_label,
                    "path": path,
                    "display_url": mask_rtsp_url_for_display(url),
                    "failure_reason": err,
                    "failure_hint": rtsp_failure_hint(err, had_rtsp_credentials),
                    "recipe_yaml": "",
                    "width": None,
                    "height": None,
                    "fps": None,
                    "probe_ms": pr.get("elapsed_ms"),
                }
                yield {"type": "result", "item": item}

    yield {"type": "done"}
