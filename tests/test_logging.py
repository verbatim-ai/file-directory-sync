from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from verbatim_sync.config import LoggingConfig
from verbatim_sync.logging_setup import (
    LOGGER_NAME,
    REDACTED,
    configure_logging,
    get_logger,
    redact,
)

PRESIGNED = (
    "https://bucket.s3.amazonaws.com/org/doc.pdf"
    "?X-Amz-Signature=deadbeefcafe&X-Amz-Expires=900"
)
TOKEN = "eyJhbGciOiJSUzUxMiIsImtpZCI6ImsifQ.eyJzdWIiOiJvIn0.c2lnbmF0dXJl"


@pytest.fixture(autouse=True)
def reset_logger():
    yield
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


class TestRedact:
    def test_strips_a_presigned_signature(self):
        result = redact(f"PUT {PRESIGNED}")
        assert "deadbeefcafe" not in result
        # The object path survives, so the log still identifies the target.
        assert "bucket.s3.amazonaws.com/org/doc.pdf" in result

    def test_strips_a_jwt(self):
        assert TOKEN not in redact(f"Authorization: Bearer {TOKEN}")
        assert REDACTED in redact(f"Authorization: Bearer {TOKEN}")

    def test_strips_a_private_key(self):
        pem = "-----BEGIN PRIVATE KEY-----\nSUPERSECRET\n-----END PRIVATE KEY-----"
        assert "SUPERSECRET" not in redact(f"key was {pem}")

    def test_strips_an_access_token_header(self):
        assert "abc123" not in redact("X-Access-Token: abc123")

    def test_leaves_ordinary_text_alone(self):
        assert redact("Accepted reports/q3.pdf (application/pdf, 51 bytes)") == (
            "Accepted reports/q3.pdf (application/pdf, 51 bytes)"
        )

    def test_is_idempotent(self):
        once = redact(f"PUT {PRESIGNED}")
        assert redact(once) == once


class TestConfigureLogging:
    def test_writes_to_the_configured_file(self, tmp_path: Path):
        log_file = tmp_path / "logs" / "sync.log"
        configure_logging(
            LoggingConfig(file=log_file, console=False), run_id=42
        )
        get_logger("scan").info("Accepted a.pdf")
        logging.shutdown()

        content = log_file.read_text()
        assert "Accepted a.pdf" in content
        assert "[run=42]" in content
        assert "verbatim_sync.scan" in content

    def test_redacts_records_from_child_loggers(self, tmp_path: Path):
        """Filters live on handlers precisely so child loggers cannot bypass them."""
        log_file = tmp_path / "sync.log"
        configure_logging(LoggingConfig(file=log_file, console=False), run_id=1)
        get_logger("api").info("PUT %s", PRESIGNED)
        logging.shutdown()

        content = log_file.read_text()
        assert "deadbeefcafe" not in content
        assert REDACTED in content

    def test_json_format(self, tmp_path: Path):
        log_file = tmp_path / "sync.log"
        configure_logging(
            LoggingConfig(file=log_file, console=False, format="json"), run_id=7
        )
        get_logger("scan").info("Accepted %s", "a.pdf", extra={"rel_path": "a.pdf"})
        logging.shutdown()

        payload = json.loads(log_file.read_text().strip())
        assert payload["msg"] == "Accepted a.pdf"
        assert payload["run_id"] == 7
        assert payload["level"] == "INFO"
        assert payload["rel_path"] == "a.pdf"

    def test_json_format_redacts_exceptions(self, tmp_path: Path):
        log_file = tmp_path / "sync.log"
        configure_logging(
            LoggingConfig(file=log_file, console=False, format="json"), run_id=1
        )
        try:
            raise RuntimeError(f"upload to {PRESIGNED} failed")
        except RuntimeError:
            get_logger("api").exception("upload failed")
        logging.shutdown()

        content = log_file.read_text()
        assert "deadbeefcafe" not in content

    def test_level_is_honoured(self, tmp_path: Path):
        log_file = tmp_path / "sync.log"
        configure_logging(
            LoggingConfig(level="WARNING", file=log_file, console=False), run_id=1
        )
        get_logger().info("invisible")
        get_logger().warning("visible")
        logging.shutdown()

        content = log_file.read_text()
        assert "invisible" not in content
        assert "visible" in content

    def test_repeated_calls_do_not_duplicate_handlers(self, tmp_path: Path):
        config = LoggingConfig(file=tmp_path / "sync.log", console=False)
        configure_logging(config, run_id=1)
        configure_logging(config, run_id=2)

        assert len(logging.getLogger(LOGGER_NAME).handlers) == 1

    def test_rotation(self, tmp_path: Path):
        log_file = tmp_path / "sync.log"
        configure_logging(
            LoggingConfig(
                file=log_file, console=False, rotate_max_bytes=512, backup_count=2
            ),
            run_id=1,
        )
        for index in range(200):
            get_logger("scan").info("Accepted file-%03d.pdf", index)
        logging.shutdown()

        assert log_file.exists()
        assert (tmp_path / "sync.log.1").exists()

    def test_httpx_is_quietened(self, tmp_path: Path):
        """httpx logs full request URLs at INFO, including presigned ones."""
        configure_logging(LoggingConfig(file=tmp_path / "sync.log", console=False))
        assert logging.getLogger("httpx").level == logging.WARNING


class TestDegradedLogging:
    def test_an_unopenable_log_file_does_not_lose_the_run(self, tmp_path: Path, capsys):
        """Wrong permissions on a cron host must be loud, not fatal."""
        blocker = tmp_path / "afile"
        blocker.write_text("x")

        configure_logging(LoggingConfig(file=blocker / "nested" / "sync.log"), run_id=1)
        get_logger("scan").info("still working")

        err = capsys.readouterr().err
        assert "Cannot open log file" in err
        assert "still working" in err  # the console handler carried on

    def test_falls_back_to_a_null_handler(self, tmp_path: Path):
        """With no console and an unopenable file there must still be a handler,
        or logging raises 'no handlers could be found' on every call."""
        blocker = tmp_path / "afile"
        blocker.write_text("x")

        configure_logging(
            LoggingConfig(file=blocker / "nested" / "sync.log", console=False), run_id=1
        )
        logger = logging.getLogger(LOGGER_NAME)

        assert logger.handlers
        assert isinstance(logger.handlers[-1], logging.NullHandler)
        get_logger("scan").info("swallowed, but not an error")
