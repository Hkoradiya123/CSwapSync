"""Logging with a secret-redaction filter.

Primary rule: callers never pass CSwap output or credential material to the
logger. The filter is defence in depth for the day someone forgets.
"""
from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from settings import secure_chmod

_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]+"),                 # Anthropic OAuth / API keys
    re.compile(r"AGE-SECRET-KEY-[A-Z0-9]+"),               # age private keys
    re.compile(r"ya29\.[A-Za-z0-9_\-\.]+"),                 # Google access tokens
    re.compile(r"1//[A-Za-z0-9_\-]{20,}"),                 # Google refresh tokens
    re.compile(r"GOCSPX-[A-Za-z0-9_\-]+"),                 # Google client secrets
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r'("(?:access|refresh)_?[Tt]oken"\s*:\s*")[^"]+(")'),
    re.compile(r'("client_secret"\s*:\s*")[^"]+(")'),
]
REDACTED = "[REDACTED]"


def redact(text: str) -> str:
    for pattern in _PATTERNS:
        if pattern.groups == 2:
            text = pattern.sub(lambda m: m.group(1) + REDACTED + m.group(2), text)
        else:
            text = pattern.sub(REDACTED, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        return True


def setup_logging(logs_dir: Path, verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger("cswapsync")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "cswapsync.log"

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=5,
                                       encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)
    file_handler.addFilter(RedactingFilter())

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.addFilter(RedactingFilter())

    logger.addHandler(file_handler)
    logger.addHandler(console)
    secure_chmod(log_path, 0o600)
    return logger


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"cswapsync.{name}")
