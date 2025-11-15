"""Fail pre-commit if secrets example placeholders are removed."""

from __future__ import annotations

import sys
from pathlib import Path

REQUIRED_MARKERS = [
    "your_bot_token_here",
    "Bearer replace-me",
    "replace-me-now",
    "AKIA....",
]


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    example = root / "config" / "secrets.yaml.example"
    content = example.read_text(encoding="utf-8")
    missing = [marker for marker in REQUIRED_MARKERS if marker not in content]
    if missing:
        marker_list = ", ".join(missing)
        print(
            f"[check-secrets-placeholders] Missing placeholder(s): {marker_list}. "
            "Never commit real credentials to config/secrets.yaml.example.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
