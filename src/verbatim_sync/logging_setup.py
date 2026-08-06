"""Logging for an unattended job.

The script must log every event, and it runs from cron where nobody sees
stderr. Three things follow from that:

* every record carries the ``run_id`` of the invocation that produced it, so
  overlapping runs stay untangled in a shared log file;
* the file handler rotates, because nothing truncates it otherwise;
* credentials are redacted before they can reach a handler — the job holds an
  RSA private key, signed JWTs and presigned storage URLs whose query string
  *is* the capability to write to the bucket.
"""

from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import re
import sys
from pathlib import Path
from typing import Any

from verbatim_sync.config import LoggingConfig

LOGGER_NAME = "verbatim_sync"

#: Set once per invocation; read by RunIdFilter for every record.
run_id_var: contextvars.ContextVar[int | str] = contextvars.ContextVar(
    "run_id", default="-"
)

REDACTED = "[REDACTED]"

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Presigned URL: keep the object path, drop the signature query string.
    (re.compile(r"(https?://[^\s?\"']+)\?[^\s\"']*"), r"\1?" + REDACTED),
    # A signed JWT, whether or not it follows an Authorization/Bearer prefix.
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), REDACTED),
    # PEM private key material pasted into a message or traceback.
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        REDACTED,
    ),
    # X-Access-Token header value.
    (re.compile(r"(?i)(x-access-token\s*[:=]\s*)\S+", re.IGNORECASE), r"\1" + REDACTED),
)


def redact(text: str) -> str:
    """Strip credential material from a string bound for a log handler."""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class RunIdFilter(logging.Filter):
    """Attach the current run id to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = run_id_var.get()
        return True


class RedactionFilter(logging.Filter):
    """Redact the formatted message and any string arguments.

    Applied as a filter rather than inside a formatter so it protects every
    handler, including the ones a future caller adds.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    key: redact(value) if isinstance(value, str) else value
                    for key, value in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    redact(value) if isinstance(value, str) else value
                    for value in record.args
                )
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for ingestion by log tooling."""

    _RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
        "message",
        "asctime",
        "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "run_id": getattr(record, "run_id", "-"),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))

        # Anything passed via logger.info(..., extra={...}) rides along.
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and key not in payload:
                payload[key] = value

        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Human-readable single line, with the run id up front for grepping."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-7s [run=%(run_id)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    def formatException(self, exc_info: Any) -> str:
        return redact(super().formatException(exc_info))


def configure_logging(config: LoggingConfig, run_id: int | str | None = None) -> logging.Logger:
    """Install handlers on the package logger. Safe to call more than once."""
    if run_id is not None:
        run_id_var.set(run_id)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(config.level)
    # Handlers are owned by this package; the root logger stays untouched so an
    # embedding process keeps its own configuration.
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter: logging.Formatter = (
        JsonFormatter() if config.format == "json" else TextFormatter()
    )

    def prepare(handler: logging.Handler) -> logging.Handler:
        # Filters go on the handler, not the logger: a logger's filters are
        # only consulted for records logged through it directly, so anything
        # from a child logger such as verbatim_sync.scan would slip past
        # redaction and arrive without a run_id.
        handler.addFilter(RunIdFilter())
        handler.addFilter(RedactionFilter())
        handler.setFormatter(formatter)
        return handler

    if config.console:
        logger.addHandler(prepare(logging.StreamHandler(sys.stderr)))

    if config.file is not None:
        try:
            Path(config.file).parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                config.file,
                maxBytes=config.rotate_max_bytes,
                backupCount=config.backup_count,
                encoding="utf-8",
            )
        except OSError as exc:
            # Losing the file is not a reason to lose the run, but it must be
            # loud: without it a cron invocation leaves no trace at all.
            logger.error("Cannot open log file %s: %s", config.file, exc)
        else:
            logger.addHandler(prepare(file_handler))

    if not logger.handlers:
        logger.addHandler(logging.NullHandler())

    # httpx logs full request URLs at INFO — including the presigned upload URL
    # with its signature. Keep the library quiet and log our own lines instead.
    for noisy in ("httpx", "httpcore", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child of the package logger, e.g. ``get_logger("scan")``."""
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)
