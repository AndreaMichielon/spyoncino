"""
Event trend chart rendering (used by SpyoncinoRuntime and HTTP layer).
"""

from __future__ import annotations

import io
from typing import Dict, List, Optional

import cv2
import numpy as np

# BGR — dark dashboard-like theme for Telegram JPEG
_CHART_BG = (19, 24, 16)
_CHART_PANEL = (25, 32, 22)
_CHART_GRID = (38, 48, 42)
_CHART_TEXT_DIM = (120, 140, 128)
_CHART_TEXT = (210, 220, 215)


def render_events_trend_jpeg(
    hours: int, series: Dict[str, List[int]], quality: int = 88
) -> Optional[bytes]:
    """
    Draw a multi-line trend chart as JPEG bytes.

    ``series`` keys: motion, person, face, error (counts), system (0-100 patrol uptime).
    """
    if hours < 1:
        return None
    for key in ("motion", "person", "face", "error", "system"):
        seq = series.get(key) or []
        if len(seq) != hours:
            return None

    width, height = 1000, 560
    img = np.full((height, width, 3), _CHART_BG, dtype=np.uint8)
    left, top, right, bottom = 72, 56, 44, 72
    plot_w = width - left - right
    plot_h = height - top - bottom

    title = f"Spyoncino - events ({hours}h)"
    cv2.putText(
        img,
        title,
        (left, 38),
        cv2.FONT_HERSHEY_DUPLEX,
        0.85,
        _CHART_TEXT,
        1,
        lineType=cv2.LINE_AA,
    )
    cv2.rectangle(
        img,
        (left, top),
        (left + plot_w, top + plot_h),
        _CHART_PANEL,
        -1,
        lineType=cv2.LINE_AA,
    )
    cv2.rectangle(
        img,
        (left, top),
        (left + plot_w, top + plot_h),
        _CHART_GRID,
        1,
        lineType=cv2.LINE_AA,
    )

    count_series = {k: series[k] for k in ("motion", "person", "face", "error")}
    max_v = max([1] + [max(v) for v in count_series.values()])
    for i in range(6):
        y = top + int(plot_h * i / 5)
        cv2.line(
            img,
            (left, y),
            (left + plot_w, y),
            _CHART_GRID,
            1,
            lineType=cv2.LINE_AA,
        )
        label = str(int(max_v * (5 - i) / 5))
        cv2.putText(
            img,
            label,
            (14, y + 5),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.42,
            _CHART_TEXT_DIM,
            1,
            lineType=cv2.LINE_AA,
        )
        cv2.line(img, (left - 5, y), (left, y), (90, 110, 98), 1, lineType=cv2.LINE_AA)

    # Right axis: system % (0-100)
    rx = left + plot_w + 8
    for i in range(6):
        y = top + int(plot_h * i / 5)
        pct = int(100 * (5 - i) / 5)
        cv2.putText(
            img,
            f"{pct}%",
            (rx, y + 5),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.38,
            (140, 175, 155),
            1,
            lineType=cv2.LINE_AA,
        )

    tick_count = min(8, max(2, hours))
    for i in range(tick_count):
        frac = i / max(1, tick_count - 1)
        x = left + int(plot_w * frac)
        cv2.line(
            img,
            (x, top + plot_h),
            (x, top + plot_h + 5),
            (90, 110, 98),
            1,
            lineType=cv2.LINE_AA,
        )
        hour_ago = int(round((1.0 - frac) * (hours - 1)))
        label = f"-{hour_ago}h"
        cv2.putText(
            img,
            label,
            (x - 16, top + plot_h + 20),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.38,
            _CHART_TEXT_DIM,
            1,
            lineType=cv2.LINE_AA,
        )

    colors = {
        "motion": (80, 170, 235),
        "person": (0, 140, 255),
        "face": (95, 200, 88),
        "error": (210, 90, 180),
    }
    for name, vals in count_series.items():
        pts = []
        for i, v in enumerate(vals):
            x = left + int(plot_w * i / max(1, hours - 1))
            y = top + plot_h - int((v / max_v) * plot_h)
            pts.append((x, y))
        for i in range(1, len(pts)):
            cv2.line(
                img,
                pts[i - 1],
                pts[i],
                colors[name],
                2,
                lineType=cv2.LINE_AA,
            )
        if pts:
            cv2.circle(img, pts[-1], 4, colors[name], -1, lineType=cv2.LINE_AA)

    sys_vals = series["system"]
    spts = []
    for i, v in enumerate(sys_vals):
        x = left + int(plot_w * i / max(1, hours - 1))
        sv = max(0, min(100, int(v)))
        y = top + plot_h - int((sv / 100.0) * plot_h)
        spts.append((x, y))
    for i in range(1, len(spts)):
        cv2.line(
            img,
            spts[i - 1],
            spts[i],
            (200, 200, 200),
            2,
            lineType=cv2.LINE_AA,
        )
    if spts:
        cv2.circle(img, spts[-1], 4, (220, 220, 220), -1, lineType=cv2.LINE_AA)

    legend_y = height - 34
    x = left
    for name in ("motion", "person", "face", "error"):
        cv2.circle(
            img, (x + 8, legend_y - 4), 5, colors[name], -1, lineType=cv2.LINE_AA
        )
        cv2.putText(
            img,
            name,
            (x + 18, legend_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            _CHART_TEXT,
            1,
            lineType=cv2.LINE_AA,
        )
        x += 118
    cv2.circle(img, (x + 8, legend_y - 4), 5, (220, 220, 220), -1, lineType=cv2.LINE_AA)
    cv2.putText(
        img,
        "system %",
        (x + 18, legend_y),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.5,
        (180, 200, 190),
        1,
        lineType=cv2.LINE_AA,
    )

    ok, encoded = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        return None
    return encoded.tobytes()


def jpeg_to_bytesio(data: bytes) -> io.BytesIO:
    bio = io.BytesIO(data)
    bio.seek(0)
    return bio
