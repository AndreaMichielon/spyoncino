"""
Zone-aware filter that annotates detections with configured regions of interest.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from ...core.bus import Subscription
from ...core.contracts import BaseModule, DetectionEvent, ModuleConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ZoneRule:
    """Immutable zone rule compiled from configuration."""

    camera_id: str
    zone_id: str
    name: str | None
    bounds: tuple[float, float, float, float]
    labels: frozenset[str]
    action: str
    frame_width: int
    frame_height: int

    def contains(self, x: float, y: float) -> bool:
        x1, y1, x2, y2 = self.bounds
        return x1 <= x <= x2 and y1 <= y <= y2

    def matches_label(self, label: str | None) -> bool:
        return not self.labels or (label is not None and label in self.labels)


class ZoningFilter(BaseModule):
    """Annotate detections with zone metadata and optionally drop those outside include zones."""

    name = "modules.process.zoning_filter"

    def __init__(self) -> None:
        super().__init__()
        self._enabled = False
        self._input_topic = "process.motion.unique"
        self._output_topic = "process.motion.zoned"
        self._unmatched_topic: str | None = None
        self._drop_outside = False
        self._frame_width = 640
        self._frame_height = 480
        self._zones: list[ZoneRule] = []
        self._subscriptions: list[Subscription] = []
        self._camera_dimensions: dict[str, tuple[int, int]] = {}

    async def configure(self, config: ModuleConfig) -> None:
        await super().configure(config)
        options = config.options
        self._enabled = bool(options.get("enabled", self._enabled))
        self._input_topic = options.get("input_topic", self._input_topic)
        self._output_topic = options.get("output_topic", self._output_topic)
        self._unmatched_topic = options.get("unmatched_topic", self._unmatched_topic)
        self._drop_outside = bool(options.get("drop_outside", self._drop_outside))
        self._frame_width = int(options.get("frame_width", self._frame_width))
        self._frame_height = int(options.get("frame_height", self._frame_height))
        self._camera_dimensions = self._parse_camera_dimensions(options.get("camera_dimensions"))
        raw_zones: Sequence[dict[str, object]] = options.get("zones", []) or []
        self._zones = [self._compile_zone(entry) for entry in raw_zones]

    async def start(self) -> None:
        if not self._enabled:
            logger.info("ZoningFilter disabled; skipping subscriptions.")
            return
        self._subscriptions.append(self.bus.subscribe(self._input_topic, self._handle_detection))
        logger.info(
            "ZoningFilter ready on %s with %d zones defined.", self._input_topic, len(self._zones)
        )

    async def stop(self) -> None:
        for subscription in self._subscriptions:
            self.bus.unsubscribe(subscription)
        self._subscriptions.clear()

    async def _handle_detection(self, topic: str, payload: DetectionEvent) -> None:
        if not isinstance(payload, DetectionEvent):
            logger.debug("ZoningFilter ignoring payload type %s on %s", type(payload), topic)
            return
        if not self._zones:
            await self.bus.publish(self._output_topic, payload)
            return
        matches, blocked = self._match_zones(payload)
        if blocked:
            logger.debug(
                "Detection %s blocked by exclusion zone for camera %s",
                payload.detector_id,
                payload.camera_id,
            )
            if self._unmatched_topic:
                await self.bus.publish(self._unmatched_topic, payload)
            return
        if matches:
            annotated = self._annotate(payload, matches)
            await self.bus.publish(self._output_topic, annotated)
            return
        if self._drop_outside:
            logger.debug(
                "Dropping detection outside include zones for camera %s", payload.camera_id
            )
            if self._unmatched_topic:
                await self.bus.publish(self._unmatched_topic, payload)
            return
        await self.bus.publish(self._output_topic, payload)

    def _match_zones(self, payload: DetectionEvent) -> tuple[list[ZoneRule], bool]:
        relevant = [zone for zone in self._zones if zone.camera_id == payload.camera_id]
        if not relevant:
            return [], False
        bbox = self._extract_bbox(payload)
        if bbox is None:
            return [], False
        label = payload.attributes.get("label")
        cx, cy = bbox
        matches: list[ZoneRule] = []
        for zone in relevant:
            if not zone.matches_label(label):
                continue
            if zone.contains(cx, cy):
                if zone.action == "exclude":
                    return [], True
                matches.append(zone)
        return matches, False

    def _extract_bbox(self, payload: DetectionEvent) -> tuple[float, float] | None:
        raw_bbox = payload.attributes.get("bbox")
        if not raw_bbox or not isinstance(raw_bbox, Sequence) or len(raw_bbox) != 4:
            return None
        x1, y1, x2, y2 = (float(value) for value in raw_bbox)
        frame_meta = payload.attributes.get("frame") or {}
        width = self._resolve_dimension(frame_meta.get("width"), payload.camera_id, axis="width")
        height = self._resolve_dimension(frame_meta.get("height"), payload.camera_id, axis="height")
        if width is None or height is None or width <= 0 or height <= 0:
            return None
        cx = ((x1 + x2) / 2) / width
        cy = ((y1 + y2) / 2) / height
        return cx, cy

    def _annotate(self, payload: DetectionEvent, matches: Iterable[ZoneRule]) -> DetectionEvent:
        attributes = dict(payload.attributes)
        attributes["zone_matches"] = [
            {"zone_id": zone.zone_id, "name": zone.name} for zone in matches
        ]
        return payload.model_copy(update={"attributes": attributes})

    def _compile_zone(self, entry: dict[str, object]) -> ZoneRule:
        bounds = entry.get("bounds") or entry.get("box") or entry.get("polygon")
        if not bounds or not isinstance(bounds, Sequence) or len(bounds) != 4:
            raise ValueError("Zone definition must include 'bounds' with four float values.")
        bx = tuple(float(value) for value in bounds)  # type: ignore[arg-type]
        camera_id = str(entry.get("camera_id") or "default")
        zone_id = str(entry.get("zone_id") or entry.get("id") or "zone")
        name = entry.get("name")
        labels = entry.get("labels") or entry.get("classes") or []
        action = str(entry.get("action") or "include")
        frame_width = int(entry.get("frame_width") or self._frame_width)
        frame_height = int(entry.get("frame_height") or self._frame_height)
        return ZoneRule(
            camera_id=camera_id,
            zone_id=zone_id,
            name=str(name) if name else None,
            bounds=(
                float(bx[0]),
                float(bx[1]),
                float(bx[2]),
                float(bx[3]),
            ),
            labels=frozenset(str(label) for label in labels) if labels else frozenset(),
            action=action,
            frame_width=frame_width,
            frame_height=frame_height,
        )

    def _parse_camera_dimensions(self, raw: object) -> dict[str, tuple[int, int]]:
        if not isinstance(raw, dict):
            return {}
        result: dict[str, tuple[int, int]] = {}
        for camera_id, dims in raw.items():
            if not isinstance(dims, dict):
                continue
            width = dims.get("width")
            height = dims.get("height")
            try:
                w = int(width)
                h = int(height)
            except (TypeError, ValueError):
                continue
            if w > 0 and h > 0:
                result[str(camera_id)] = (w, h)
        return result

    def _resolve_dimension(self, value: object, camera_id: str, *, axis: str) -> float | None:
        try:
            resolved = float(value) if value is not None else None
        except (TypeError, ValueError):
            resolved = None
        if resolved and resolved > 0:
            return resolved
        camera_dims = self._camera_dimensions.get(camera_id)
        if camera_dims:
            index = 0 if axis == "width" else 1
            fallback = camera_dims[index]
            if fallback > 0:
                return float(fallback)
        fallback = self._frame_width if axis == "width" else self._frame_height
        if fallback > 0:
            return float(fallback)
        return None


__all__ = ["ZoningFilter"]
