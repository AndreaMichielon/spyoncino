import numpy as np
import cv2
from typing import List


class MotionDetection:
    def __init__(self, threshold: int = 10):
        self.state = {}
        self.threshold = threshold

    def _calculate_motion_percent(self, fg_mask: np.ndarray):
        motion_pixels = cv2.countNonZero(fg_mask)
        total_pixels = fg_mask.shape[0] * fg_mask.shape[1]
        return int((motion_pixels / total_pixels) * 100), fg_mask

    def peak(self, camera_id: str, frame: np.ndarray):
        if camera_id not in self.state:
            self.state[camera_id] = cv2.createBackgroundSubtractorMOG2(
                detectShadows=True
            )

        fg_mask = self.state[camera_id].apply(frame)
        motion_percent, fg_mask = self._calculate_motion_percent(fg_mask)
        return motion_percent > self.threshold, motion_percent, fg_mask

    def detect(self, camera_id: str, frames: List[np.ndarray]) -> List[np.ndarray]:
        frames_with_motion = []
        motion_detected = False
        for frame in frames:
            is_motion, motion_percent, fg_mask = self.peak(camera_id, frame)
            motion_detected = motion_detected or is_motion
            frames_with_motion.append(
                {
                    "overlay": self._create_overlay(
                        frame, fg_mask, motion_percent, self.threshold
                    ),
                    "score": motion_percent,
                }
            )
        return frames_with_motion, motion_detected

    def _smooth_mask(self, fg_mask: np.ndarray) -> np.ndarray:
        """Reduce speckle while keeping moving regions readable."""
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        m = cv2.morphologyEx(fg_mask, cv2.MORPH_OPEN, k)
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
        k2 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        m = cv2.dilate(m, k2, iterations=1)
        return m

    def _create_overlay(
        self,
        frame: np.ndarray,
        fg_mask: np.ndarray,
        motion_percent: int,
        threshold: int,
    ) -> np.ndarray:
        h, w = frame.shape[:2]
        overlay = np.zeros((h, w, 3), dtype=np.uint8)

        scale = min(w / 1920.0, h / 1080.0)
        font_scale = max(0.42, 0.52 * scale)

        color_normal = (110, 210, 120)
        color_alarmed = (68, 92, 255)
        mask_color = color_alarmed if motion_percent >= threshold else color_normal

        cleaned = self._smooth_mask(fg_mask)
        fg_mask_3ch = cv2.cvtColor(cleaned, cv2.COLOR_GRAY2BGR)
        colored_mask = np.where(fg_mask_3ch > 0, mask_color, (0, 0, 0))
        overlay = np.maximum(overlay, colored_mask.astype(np.uint8))

        text = f"Motion {int(motion_percent)}%  (threshold {int(threshold)}%)"
        font = cv2.FONT_HERSHEY_DUPLEX
        thickness = max(1, int(round(scale)))
        (tw, th), baseline = cv2.getTextSize(text, font, font_scale, thickness)
        pad_x = max(10, int(12 * scale))
        pad_y = max(6, int(8 * scale))
        px1 = int(10 * scale)
        py1 = int(8 * scale)
        px2 = min(w - 1, px1 + tw + pad_x * 2)
        py2 = min(h - 1, py1 + th + pad_y * 2)
        bg = (22, 36, 30)
        cv2.rectangle(overlay, (px1, py1), (px2, py2), bg, -1, lineType=cv2.LINE_AA)
        cv2.rectangle(
            overlay, (px1, py1), (px2, py2), mask_color, 1, lineType=cv2.LINE_AA
        )
        tx = px1 + pad_x
        ty = py1 + th + pad_y - max(0, baseline // 2)
        cv2.putText(
            overlay,
            text,
            (tx, ty),
            font,
            font_scale,
            (248, 250, 248),
            thickness,
            lineType=cv2.LINE_AA,
        )

        return overlay
