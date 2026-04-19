"""
Resolve filesystem paths from a recipe using a ``data_root`` anchor.

If the recipe **omits** ``data_root``, it defaults to ``"data"`` so SQLite, media store,
weights, and gallery paths resolve under ``./data/`` (not the repo root).

Set ``data_root: null`` in YAML to restore the legacy layout (paths relative to cwd only).
Use ``data_root: "."`` to anchor at cwd without a ``data`` subfolder.

If a relative path starts with the same name as ``data_root`` (e.g. ``data/face_gallery``
with ``data_root: data``), the duplicate segment is stripped so existing recipes keep
working without ``data/data/...`` duplication.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional


def resolve_data_root(
    recipe: Dict[str, Any], cwd: Optional[Path] = None
) -> Optional[Path]:
    """
    Return absolute ``data_root``, or None for legacy cwd-only resolution.

    - Key **absent**: default ``"data"`` (runtime state under ``./data/``).
    - ``data_root: null`` or ``""``: legacy — no anchor; paths relative to cwd.
    """
    base = cwd if cwd is not None else Path.cwd()
    if "data_root" not in recipe:
        raw: Any = "data"
    else:
        raw = recipe["data_root"]
        if raw is None:
            return None
        if isinstance(raw, str) and not raw.strip():
            return None
    s = str(raw).strip()
    p = Path(s)
    if p.is_absolute():
        return p.resolve()
    return (base / p).resolve()


def _strip_redundant_prefix(data_root: Path, relative: Path) -> Path:
    """If ``relative`` starts with the basename of ``data_root``, drop that first segment."""
    parts = relative.parts
    if not parts:
        return relative
    if parts[0] == data_root.name:
        return Path(*parts[1:]) if len(parts) > 1 else Path(".")
    return relative


def resolve_path_for_recipe(
    recipe: Dict[str, Any],
    path_str: str,
    *,
    cwd: Optional[Path] = None,
) -> Path:
    """
    Resolve a path string from the recipe.

    - Absolute paths: returned resolved.
    - If ``data_root`` resolves (including the default ``data/``): relative paths join under it
      (with duplicate-prefix strip).
    - If ``data_root`` is explicitly disabled (``null``): relative to ``cwd``.
    """
    base = cwd if cwd is not None else Path.cwd()
    p = Path(str(path_str).strip())
    if not p.parts:
        return base.resolve()
    if p.is_absolute():
        return p.resolve()
    dr = resolve_data_root(recipe, cwd=base)
    if dr is None:
        return (base / p).resolve()
    inner = _strip_redundant_prefix(dr, p)
    return (dr / inner).resolve()


def sqlite_path_from_recipe(recipe: Dict[str, Any], cwd: Optional[Path] = None) -> Path:
    """SQLite file path: ``sqlite_path`` recipe key, default ``spyoncino.db``."""
    raw = recipe.get("sqlite_path", "spyoncino.db")
    if raw is None:
        raw = "spyoncino.db"
    return resolve_path_for_recipe(recipe, str(raw), cwd=cwd)


def gallery_path_from_recipe(
    recipe: Dict[str, Any], cwd: Optional[Path] = None
) -> Path:
    """
    Face gallery directory from the ``face_identification`` postproc step, or the same
    defaults as :class:`~spyoncino.postproc.face_identification.FaceIdentification`.
    """
    from .recipe_classes import resolve_recipe_class

    default = "data/face_gallery"
    for step in recipe.get("postproc") or []:
        if not isinstance(step, dict):
            continue
        try:
            resolved = resolve_recipe_class(str(step.get("class") or ""))
        except ValueError:
            continue
        if not resolved.endswith(".FaceIdentification"):
            continue
        params = step.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        raw = params.get("gallery_path", default)
        return resolve_path_for_recipe(recipe, str(raw), cwd=cwd)
    return resolve_path_for_recipe(recipe, default, cwd=cwd)


def resolve_inference_weights(
    recipe: Dict[str, Any],
    weights: str,
    *,
    cwd: Optional[Path] = None,
) -> str:
    """Return a string path for YOLO weights (Ultralytics accepts str)."""
    p = resolve_path_for_recipe(recipe, weights, cwd=cwd)
    return str(p)


def resolve_secrets_path(
    recipe: Dict[str, Any],
    *,
    cwd: Optional[Path] = None,
) -> Optional[str]:
    """
    Path to ``secrets.yaml``. Relative paths use **cwd** only (not ``data_root``), so
    ``data/config/secrets.yaml`` (or any cwd-relative path) stays repo-relative — not joined with ``data_root``.
    """
    raw = recipe.get("secrets_path")
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    base = cwd if cwd is not None else Path.cwd()
    p = Path(s)
    if p.is_absolute():
        return str(p.resolve())
    return str((base / p).resolve())
