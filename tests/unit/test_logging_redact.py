"""Telegram bot token redaction in logs."""

import logging

from spyoncino.logging_redact import (
    RedactTelegramTokenFilter,
    redact_telegram_bot_token,
)


def test_redact_telegram_url():
    raw = (
        "HTTP Request: POST https://api.telegram.org/bot123456789:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxx/getUpdates "
        '"HTTP/1.1 200 OK"'
    )
    out = redact_telegram_bot_token(raw)
    assert "AAHxxxx" not in out
    assert "bot***REDACTED***" in out
    assert "getUpdates" in out


def test_redact_non_string_url_arg_like_httpx():
    """httpx logs ``HTTP Request: ...`` with a URL object, not a str — token must still redact."""

    class _FakeUrl:
        def __str__(self) -> str:
            return "https://api.telegram.org/bot123456789:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxx/getMe"

    filt = RedactTelegramTokenFilter()
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='HTTP Request: %s "%s %d %s"',
        args=(
            _FakeUrl(),
            "HTTP",
            200,
            "OK",
        ),
        exc_info=None,
    )
    assert filt.filter(record)
    line = record.getMessage()
    assert "AAHxxxx" not in line
    assert "bot***REDACTED***" in line
