from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from verbatim_sync.cli import EXIT_CONFIG_ERROR, EXIT_FAILURE, EXIT_OK, main
from verbatim_sync.logging_setup import LOGGER_NAME

CORPUS = "550e8400-e29b-41d4-a716-446655440001"


@pytest.fixture(autouse=True)
def reset_logger():
    yield
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def read_files(db_path: Path) -> dict[str, sqlite3.Row]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        return {row["rel_path"]: row for row in connection.execute("SELECT * FROM file")}
    finally:
        connection.close()


class TestInitDb:
    def test_creates_and_is_idempotent(self, write_config, tmp_path: Path):
        config_path = write_config()
        assert main(["--config", str(config_path), "--init-db"]) == EXIT_OK

        db_path = tmp_path / "state" / "sync.db"
        assert db_path.exists()
        assert main(["--config", str(config_path), "--init-db"]) == EXIT_OK


class TestConfigErrors:
    def test_missing_config_exits_2(self, tmp_path: Path, capsys):
        assert main(["--config", str(tmp_path / "nope.toml")]) == EXIT_CONFIG_ERROR
        assert "configuration error" in capsys.readouterr().err


class TestScanOnly:
    def test_records_accepted_files_and_skips_the_rest(
        self, write_config, tmp_path: Path
    ):
        config_path = write_config()
        assert main(["--config", str(config_path), "--scan-only"]) == EXIT_OK

        files = read_files(tmp_path / "state" / "sync.db")
        assert set(files) == {"a.pdf", "nested/b.pdf"}

        record = files["a.pdf"]
        assert record["content_type"] == "application/pdf"
        assert record["sync_state"] == "NEW"
        assert record["document_id"] is None
        assert len(record["content_hash"]) == 64

    def test_logs_a_reason_for_every_skip(self, write_config, tmp_path: Path):
        config_path = write_config()
        main(["--config", str(config_path), "--scan-only"])

        log = (tmp_path / "logs" / "sync.log").read_text()
        assert "Skipped reports/big.pdf: file is larger than" in log
        assert "Skipped notes.txt: content type is not in" in log
        assert "Skipped archive.zzz: content type could not be determined" in log
        assert "Skipped empty.pdf: file is smaller than" in log
        # .hidden.pdf is dropped by the exclude glob before filtering.
        assert ".hidden.pdf" not in log

    def test_second_run_reuses_the_hash_of_unchanged_files(
        self, write_config, tmp_path: Path
    ):
        config_path = write_config()
        main(["--config", str(config_path), "--scan-only"])
        (tmp_path / "logs" / "sync.log").unlink()
        main(["--config", str(config_path), "--scan-only"])

        log = (tmp_path / "logs" / "sync.log").read_text()
        assert "unchanged, hash reused" in log

    def test_detects_a_changed_file(self, write_config, tmp_path: Path, source_tree: Path):
        config_path = write_config()
        main(["--config", str(config_path), "--scan-only"])

        (source_tree / "a.pdf").write_bytes(b"%PDF-1.4 modified content")
        (tmp_path / "logs" / "sync.log").unlink()
        main(["--config", str(config_path), "--scan-only"])

        assert "changed" in (tmp_path / "logs" / "sync.log").read_text()

    def test_detects_a_file_that_disappeared(
        self, write_config, tmp_path: Path, source_tree: Path
    ):
        config_path = write_config()
        main(["--config", str(config_path), "--scan-only"])

        (source_tree / "nested" / "b.pdf").unlink()
        (tmp_path / "logs" / "sync.log").unlink()
        main(["--config", str(config_path), "--scan-only"])

        log = (tmp_path / "logs" / "sync.log").read_text()
        assert "Missing locally: nested/b.pdf" in log
        # The row survives: it still holds the mapping needed to delete remotely.
        assert "nested/b.pdf" in read_files(tmp_path / "state" / "sync.db")

    def test_records_a_successful_run(self, write_config, tmp_path: Path):
        config_path = write_config()
        main(["--config", str(config_path), "--scan-only"])

        connection = sqlite3.connect(tmp_path / "state" / "sync.db")
        connection.row_factory = sqlite3.Row
        run = connection.execute("SELECT * FROM sync_run").fetchone()
        events = connection.execute("SELECT * FROM event").fetchall()
        connection.close()

        assert run["status"] == "SUCCESS"
        assert run["mode"] == "scan-only"
        assert run["files_scanned"] == 6  # .hidden.pdf excluded before filtering
        assert run["files_skipped"] == 4
        assert {event["event_type"] for event in events} >= {
            "run.started",
            "file.discovered",
            "file.skipped",
        }

    def test_verbose_flag_lowers_the_level(self, write_config, tmp_path: Path):
        config_path = write_config()
        main(["--config", str(config_path), "--scan-only", "-v"])
        assert "Excluded by pattern" in (tmp_path / "logs" / "sync.log").read_text()


@pytest.fixture
def offline(monkeypatch, backend):
    """Wire the CLI to the in-memory backend so no test touches the network."""

    class NullClient:
        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "verbatim_sync.cli._build_documents_api", lambda config: (NullClient(), backend)
    )
    return backend


class TestDryRun:
    def test_reports_the_plan_without_touching_anything(
        self, write_config, tmp_path: Path, offline, capsys
    ):
        config_path = write_config()
        assert main(["--config", str(config_path), "--dry-run"]) == EXIT_OK

        # Nothing sent, nothing recorded.
        assert offline.documents == {}
        assert read_files(tmp_path / "state" / "sync.db") == {}

        # The plan reaches the console even though the config silences it.
        output = capsys.readouterr().err
        assert "Would UPLOAD   a.pdf" in output
        assert "Would UPLOAD   nested/b.pdf" in output
        assert "nothing was sent to the backend" in output

    def test_reports_updates_and_deletions(
        self, write_config, tmp_path: Path, source_tree: Path, offline, capsys
    ):
        config_path = write_config()
        main(["--config", str(config_path)])
        capsys.readouterr()

        (source_tree / "a.pdf").write_bytes(b"%PDF-1.4 revised")
        (source_tree / "nested" / "b.pdf").unlink()
        assert main(["--config", str(config_path), "--dry-run"]) == EXIT_OK

        output = capsys.readouterr().err
        assert "Would REPLACE  a.pdf" in output
        assert "Would DELETE   nested/b.pdf" in output

    def test_clean_tree_says_so(self, write_config, offline, capsys):
        config_path = write_config()
        main(["--config", str(config_path)])
        capsys.readouterr()

        main(["--config", str(config_path), "--dry-run"])
        assert "nothing to do" in capsys.readouterr().err


class TestSync:
    def test_uploads_and_records_a_successful_run(
        self, write_config, tmp_path: Path, offline
    ):
        config_path = write_config()
        assert main(["--config", str(config_path)]) == EXIT_OK

        assert len(offline.documents) == 2
        files = read_files(tmp_path / "state" / "sync.db")
        assert set(files) == {"a.pdf", "nested/b.pdf"}
        assert all(row["sync_state"] == "SYNCED" for row in files.values())
        assert all(row["document_id"] for row in files.values())

        connection = sqlite3.connect(tmp_path / "state" / "sync.db")
        connection.row_factory = sqlite3.Row
        run = connection.execute("SELECT * FROM sync_run").fetchone()
        connection.close()
        assert run["status"] == "SUCCESS"
        assert run["mode"] == "sync"
        assert run["files_uploaded"] == 2
        assert run["files_skipped"] == 4

    def test_exits_nonzero_when_a_file_fails(self, write_config, tmp_path, offline):
        config_path = write_config()
        original = offline.init_upload

        def selective(**kwargs):
            if kwargs["filename"] == "a.pdf":
                raise OSError("disk gone")
            return original(**kwargs)

        offline.init_upload = selective
        assert main(["--config", str(config_path)]) == EXIT_FAILURE

        connection = sqlite3.connect(tmp_path / "state" / "sync.db")
        connection.row_factory = sqlite3.Row
        run = connection.execute("SELECT * FROM sync_run").fetchone()
        connection.close()
        assert run["status"] == "FAILED"
        assert "a.pdf" in run["error"]

    def test_honours_delete_remote_when_missing_false(
        self, write_config, tmp_path: Path, source_tree: Path, keys_dir, offline
    ):
        body = (write_config().read_text()) + "\n[sync]\ndelete_remote_when_missing = false\n"
        config_path = write_config(body, name="nodelete.toml")

        main(["--config", str(config_path)])
        (source_tree / "nested" / "b.pdf").unlink()
        assert main(["--config", str(config_path)]) == EXIT_OK

        # The document stays, and so does its row.
        assert len(offline.documents) == 2
        assert "nested/b.pdf" in read_files(tmp_path / "state" / "sync.db")


class TestRebuildDb:
    def test_restores_state_from_the_corpus(self, write_config, tmp_path: Path, offline):
        config_path = write_config()
        main(["--config", str(config_path)])

        db_path = tmp_path / "state" / "sync.db"
        expected = {p: row["document_id"] for p, row in read_files(db_path).items()}
        db_path.unlink()  # lose the database entirely

        assert main(["--config", str(config_path), "--rebuild-db"]) == EXIT_OK

        restored = read_files(db_path)
        assert {p: row["document_id"] for p, row in restored.items()} == expected
        assert all(row["sync_state"] == "SYNCED" for row in restored.values())

    def test_a_rebuilt_database_needs_no_further_sync(
        self, write_config, tmp_path: Path, offline
    ):
        config_path = write_config()
        main(["--config", str(config_path)])
        (tmp_path / "state" / "sync.db").unlink()
        main(["--config", str(config_path), "--rebuild-db"])

        calls_before = len(offline.calls)
        assert main(["--config", str(config_path)]) == EXIT_OK
        assert len(offline.calls) == calls_before  # nothing re-uploaded


class TestCheck:
    """--check is the first command anyone runs, and the one that has to give
    a precise answer when credentials are wrong."""

    def test_passes_with_good_credentials(self, write_config, tmp_path: Path, offline):
        config_path = write_config()
        assert main(["--config", str(config_path), "--check"]) == EXIT_OK

        log = (tmp_path / "logs" / "sync.log").read_text()
        assert "Check passed" in log
        assert "Authenticated" in log
        assert "Platform accepts 3 content type(s)" in log

    def test_reports_the_key_and_organisation(self, write_config, tmp_path: Path, offline):
        from conftest import KEY_ID, ORG_ID

        main(["--config", str(write_config()), "--check"])
        log = (tmp_path / "logs" / "sync.log").read_text()
        assert f"kid={KEY_ID}" in log
        assert f"oid={ORG_ID}" in log

    def test_verifies_the_token_against_the_local_public_key(
        self, write_config, tmp_path: Path, offline
    ):
        main(["--config", str(write_config()), "--check"])
        log = (tmp_path / "logs" / "sync.log").read_text()
        assert "Token verifies against staging.pub" in log
        assert "iss=verbatim-ai.com" in log

    def test_warns_when_there_is_no_public_key(
        self, write_config, tmp_path: Path, keys_dir: Path, offline
    ):
        (keys_dir / "staging.pub").unlink()
        assert main(["--config", str(write_config()), "--check"]) == EXIT_OK

        log = (tmp_path / "logs" / "sync.log").read_text()
        assert "skipping the local key self-check" in log

    def test_fails_loudly_on_a_mismatched_key_pair(
        self, write_config, tmp_path: Path, keys_dir: Path, offline
    ):
        """A stale .pub would otherwise surface as an opaque 403 later."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        (keys_dir / "staging.pub").write_bytes(
            other.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )

        assert main(["--config", str(write_config()), "--check"]) == EXIT_FAILURE
        assert "does not verify" in (tmp_path / "logs" / "sync.log").read_text()

    def test_warns_about_a_content_type_the_platform_rejects(
        self, write_config, tmp_path: Path, source_tree: Path, api_section, offline
    ):
        body = (
            f'[source]\nroot_dir = "{source_tree}"\n'
            '[corpus]\nid = "550e8400-e29b-41d4-a716-446655440001"\n'
            '[filters]\ncontent_types = ["application/pdf", "application/zip"]\n'
            '[database]\npath = "./state/sync.db"\n'
            '[logging]\nfile = "./logs/sync.log"\nconsole = false\n'
        ) + api_section
        config_path = write_config(body, name="zip.toml")

        assert main(["--config", str(config_path), "--check"]) == EXIT_OK

        log = (tmp_path / "logs" / "sync.log").read_text()
        assert "does not accept" in log
        assert "application/zip" in log

    def test_notes_an_empty_content_type_allowlist(
        self, write_config, tmp_path: Path, source_tree: Path, api_section, offline
    ):
        body = (
            f'[source]\nroot_dir = "{source_tree}"\n'
            '[corpus]\nid = "550e8400-e29b-41d4-a716-446655440001"\n'
            '[database]\npath = "./state/sync.db"\n'
            '[logging]\nfile = "./logs/sync.log"\nconsole = false\n'
        ) + api_section
        config_path = write_config(body, name="anytype.toml")

        main(["--config", str(config_path), "--check"])
        assert "every platform-accepted type" in (tmp_path / "logs" / "sync.log").read_text()

    def test_confirms_when_all_types_are_supported(
        self, write_config, tmp_path: Path, offline
    ):
        main(["--config", str(write_config()), "--check"])
        log = (tmp_path / "logs" / "sync.log").read_text()
        assert "All configured content types are accepted" in log

    def test_reports_an_api_rejection(self, write_config, tmp_path: Path, offline):
        from verbatim_sync.errors import ApiError

        def forbidden():
            raise ApiError("GET /v1/auth/whoami returned HTTP 403", status_code=403)

        offline.whoami = forbidden
        assert main(["--config", str(write_config()), "--check"]) == EXIT_FAILURE
        assert "403" in (tmp_path / "logs" / "sync.log").read_text()

    def test_migrates_the_database_as_a_side_effect(
        self, write_config, tmp_path: Path, offline
    ):
        main(["--config", str(write_config()), "--check"])
        assert (tmp_path / "state" / "sync.db").exists()


class TestMainErrorHandling:
    def test_unexpected_exceptions_are_reported_not_raised(
        self, write_config, tmp_path: Path, monkeypatch, offline
    ):
        def boom(config):
            raise ZeroDivisionError("something impossible")

        monkeypatch.setattr("verbatim_sync.cli.run_stats", boom)
        assert main(["--config", str(write_config()), "--stats"]) == EXIT_FAILURE
        assert "Unexpected failure" in (tmp_path / "logs" / "sync.log").read_text()

    def test_keyboard_interrupt_exits_cleanly(
        self, write_config, tmp_path: Path, monkeypatch, offline
    ):
        def interrupted(config):
            raise KeyboardInterrupt

        monkeypatch.setattr("verbatim_sync.cli.run_stats", interrupted)
        assert main(["--config", str(write_config()), "--stats"]) == EXIT_FAILURE
        assert "Interrupted" in (tmp_path / "logs" / "sync.log").read_text()

    def test_modes_are_mutually_exclusive(self, write_config):
        with pytest.raises(SystemExit) as exc:
            main(["--config", str(write_config()), "--check", "--stats"])
        assert exc.value.code == 2

    def test_config_is_required(self):
        with pytest.raises(SystemExit) as exc:
            main([])
        assert exc.value.code == 2

    def test_log_file_override(self, write_config, tmp_path: Path, offline):
        elsewhere = tmp_path / "other" / "run.log"
        main(["--config", str(write_config()), "--check", "--log-file", str(elsewhere)])
        assert elsewhere.exists()
        assert "Check passed" in elsewhere.read_text()


class TestModuleEntryPoint:
    def test_python_m_verbatim_sync_works(self):
        """The console script is not the only way in; -m must work too."""
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-m", "verbatim_sync", "--version"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "verbatim-sync" in result.stdout

    def test_module_entry_point_reports_config_errors(self, tmp_path: Path):
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-m", "verbatim_sync", "--config", str(tmp_path / "no.toml")],
            capture_output=True,
            text=True,
        )
        assert result.returncode == EXIT_CONFIG_ERROR
        assert "configuration error" in result.stderr


class TestScanOnlyFailurePaths:
    def test_an_unreadable_file_is_counted_not_fatal(
        self, write_config, tmp_path: Path, monkeypatch
    ):
        import verbatim_sync.cli as cli_module

        real = cli_module.hash_file

        def selective(path, chunk_size=1 << 20):
            if str(path).endswith("a.pdf"):
                raise OSError("input/output error")
            return real(path, chunk_size)

        monkeypatch.setattr(cli_module, "hash_file", selective)
        assert main(["--config", str(write_config()), "--scan-only"]) == EXIT_OK

        log = (tmp_path / "logs" / "sync.log").read_text()
        assert "Cannot read a.pdf" in log
        assert "1 unreadable" in log
        assert "a.pdf" not in read_files(tmp_path / "state" / "sync.db")

    def test_a_crash_mid_scan_marks_the_run_failed(
        self, write_config, tmp_path: Path, monkeypatch
    ):
        import verbatim_sync.cli as cli_module

        def explode(source):
            raise RuntimeError("filesystem went away")
            yield  # pragma: no cover - generator marker

        monkeypatch.setattr(cli_module, "walk", explode)
        assert main(["--config", str(write_config()), "--scan-only"]) == EXIT_FAILURE

        connection = sqlite3.connect(tmp_path / "state" / "sync.db")
        connection.row_factory = sqlite3.Row
        run = connection.execute("SELECT * FROM sync_run").fetchone()
        connection.close()
        assert run["status"] == "FAILED"
        assert "filesystem went away" in run["error"]


class TestRebuildDbFailure:
    def test_a_crash_marks_the_run_failed(self, write_config, tmp_path: Path, offline):
        def explode(corpus_id, **kwargs):
            raise RuntimeError("corpus unreachable")

        offline.iter_all = explode
        assert main(["--config", str(write_config()), "--rebuild-db"]) == EXIT_FAILURE

        connection = sqlite3.connect(tmp_path / "state" / "sync.db")
        connection.row_factory = sqlite3.Row
        run = connection.execute("SELECT * FROM sync_run").fetchone()
        connection.close()
        assert run["status"] == "FAILED"
        assert run["mode"] == "rebuild-db"
