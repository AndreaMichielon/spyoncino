"""
Recipe class aliases: short names in YAML map to concrete classes.

Fully qualified dotted paths (containing '.') are still resolved via import.
"""

from typing import Dict, Set, Union

# Keys are lowercase aliases; values are import paths under the ``spyoncino`` package.
RECIPE_CLASS_ALIASES: Dict[str, str] = {
    "camera": "spyoncino.input.cam_grabber.CamGrabber",
    "motion": "spyoncino.preproc.motion_detection.MotionDetection",
    "detector": "spyoncino.inference.object_detection.ObjectDetection",
    "telegram": "spyoncino.interface.telegram_bot.TelegramBotInterface",
    "webapp": "spyoncino.interface.webapp.WebAppInterface",
    "face_identification": "spyoncino.postproc.face_identification.FaceIdentification",
}


def resolve_recipe_class(class_ref: str) -> str:
    """Return dotted import path for a recipe ``class`` entry."""
    if not class_ref or not isinstance(class_ref, str):
        raise ValueError("Recipe class must be a non-empty string")
    stripped = class_ref.strip()
    if "." in stripped:
        return stripped
    key = stripped.lower()
    if key not in RECIPE_CLASS_ALIASES:
        raise ValueError(
            f"Unknown recipe class alias {class_ref!r}. "
            f"Known aliases: {', '.join(sorted(RECIPE_CLASS_ALIASES))}"
        )
    return RECIPE_CLASS_ALIASES[key]


def normalize_notify_modes(value: Union[None, bool, str, list, tuple]) -> Set[str]:
    """
    Normalize notify_* recipe values to a set of modes: text, gif, video.

    Accepts a single string, a list/tuple of strings, or falsy for "none".
    """
    if value is None or value is False:
        return set()
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("", "none", "off", "false", "no"):
            return set()
        if s in ("text", "gif", "video"):
            return {s}
        raise ValueError(
            f"Invalid notify mode {value!r}; use text, gif, video, or a list of those."
        )
    if isinstance(value, (list, tuple)):
        out: Set[str] = set()
        for item in value:
            if not isinstance(item, str):
                continue
            s = item.strip().lower()
            if s in ("text", "gif", "video"):
                out.add(s)
            elif s in ("", "none", "off"):
                continue
            else:
                raise ValueError(
                    f"Invalid notify mode {item!r}; use text, gif, or video."
                )
        return out
    raise ValueError(
        f"notify modes must be str, list, or falsy; got {type(value).__name__}"
    )
