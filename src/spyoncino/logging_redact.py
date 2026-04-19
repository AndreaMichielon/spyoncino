"""
Sanitize log records so Telegram Bot API tokens never appear in log output.

URLs look like ``https://api.telegram.org/bot<token>/method`` — httpx logs the full URL.
"""

from __future__ import annotations

import logging
import re
from typing import Any

# ``123456789:AA...`` in ``/bot.../`` paths (Telegram bot token format).
_TELEGRAM_BOT_TOKEN_IN_PATH = re.compile(r"bot\d+:[A-Za-z0-9_-]+")


def redact_telegram_bot_token(text: str) -> str:
    """Replace embedded ``bot<digits>:<secret>`` segments (e.g. in API URLs)."""
    if not text or "bot" not in text:
        return text
    return _TELEGRAM_BOT_TOKEN_IN_PATH.sub("bot***REDACTED***", text)


def _might_contain_telegram_token(text: str) -> bool:
    return (
        "telegram.org" in text or _TELEGRAM_BOT_TOKEN_IN_PATH.search(text) is not None
    )


def _redact_log_arg(arg: Any) -> Any:
    """
    Redact a single log format argument.

    ``httpx`` passes :class:`httpx.URL` objects (not strings); the token only appears after
    ``str(arg)`` / ``%`` formatting, so we stringify and redact when it looks like Telegram.
    """
    if isinstance(arg, str):
        return redact_telegram_bot_token(arg)
    if isinstance(arg, (bytes, bytearray)):
        try:
            text = bytes(arg).decode("utf-8", errors="replace")
        except Exception:
            return arg
        if not _might_contain_telegram_token(text):
            return arg
        out = redact_telegram_bot_token(text)
        enc = out.encode("utf-8")
        if isinstance(arg, bytes):
            return enc
        return bytearray(enc)
    try:
        s = str(arg)
    except Exception:
        return arg
    if _might_contain_telegram_token(s):
        return redact_telegram_bot_token(s)
    return arg


class RedactTelegramTokenFilter(logging.Filter):
    """``logging.Filter`` that redacts Telegram bot tokens from message and %-format args."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_telegram_bot_token(record.msg)
        if record.args:
            record.args = tuple(_redact_log_arg(a) for a in record.args)
        return True


def _handler_has_redact_filter(handler: logging.Handler) -> bool:
    return any(
        isinstance(f, RedactTelegramTokenFilter)
        for f in getattr(handler, "filters", [])
    )


def install_telegram_token_log_redaction() -> None:
    """
    Attach :class:`RedactTelegramTokenFilter` so Telegram URLs never reach handlers intact.

    Filters on a parent logger (e.g. ``httpx``) do **not** run for child loggers such as
    ``httpx._client``. Attaching to **handlers** (especially on the root logger) catches
    propagated records.

    **Uvicorn** (and similar) may **replace** root handlers *after* :func:`logging.basicConfig`.
    This function walks **every** :class:`logging.Handler` on **every** registered logger so
    new handlers still get the filter. Safe to call again after the web server starts.

    Also attaches to a few named loggers for trees that set ``propagate=False``.

    Safe to call multiple times (idempotent).
    """
    filt = RedactTelegramTokenFilter()
    seen_handler_ids: set[int] = set()

    def _attach_to_handler(handler: logging.Handler) -> None:
        hid = id(handler)
        if hid in seen_handler_ids:
            return
        seen_handler_ids.add(hid)
        if not _handler_has_redact_filter(handler):
            handler.addFilter(filt)

    root = logging.getLogger()
    for handler in root.handlers:
        _attach_to_handler(handler)

    for name in list(logging.root.manager.loggerDict.keys()):
        obj = logging.root.manager.loggerDict[name]
        if not isinstance(obj, logging.Logger):
            continue
        for handler in obj.handlers:
            _attach_to_handler(handler)

    for name in (
        "httpx",
        "httpcore",
        "telegram",
        "telegram.ext",
    ):
        log = logging.getLogger(name)
        if not any(isinstance(f, RedactTelegramTokenFilter) for f in log.filters):
            log.addFilter(filt)
