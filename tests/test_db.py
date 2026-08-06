from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from verbatim_sync.db import SCHEMA_VERSION, Repository, connect, migrate, schema_version
from verbatim_sync.db.models import RunStatus, SyncState
from verbatim_sync.errors import DbError

CORPUS = "550e8400-e29b-41d4-a716-446655440001"


@pytest.fixture
def repo(tmp_path: Path):
    connection = connect(tmp_path / "state" / "sync.db")
    migrate(connection)
    yield Repository(connection)
    connection.close()


class TestMigrations:
    def test_creates_the_schema(self, tmp_path: Path):
        connection = connect(tmp_path / "sync.db")
        assert schema_version(connection) == 0
        assert migrate(connection) == SCHEMA_VERSION

        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"sync_run", "file", "event"} <= tables
        connection.close()

    def test_is_idempotent(self, tmp_path: Path):
        connection = connect(tmp_path / "sync.db")
        migrate(connection)
        assert migrate(connection) == SCHEMA_VERSION
        assert schema_version(connection) == SCHEMA_VERSION
        connection.close()

    def test_creates_parent_directories(self, tmp_path: Path):
        connection = connect(tmp_path / "deep" / "nested" / "sync.db")
        migrate(connection)
        assert (tmp_path / "deep" / "nested" / "sync.db").exists()
        connection.close()

    def test_refuses_a_newer_schema(self, tmp_path: Path):
        connection = connect(tmp_path / "sync.db")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        with pytest.raises(DbError, match="newer than this build"):
            migrate(connection)
        connection.close()


class TestRuns:
    def test_records_a_run_lifecycle(self, repo: Repository):
        run_id = repo.start_run(mode="scan-only", corpus_id=CORPUS, root_dir="/docs")

        run = repo.get_run(run_id)
        assert run is not None
        assert run.status == RunStatus.RUNNING
        assert run.finished_at is None

        repo.finish_run(
            run_id, status=RunStatus.SUCCESS, counters={"scanned": 7, "skipped": 2}
        )
        run = repo.get_run(run_id)
        assert run.status == RunStatus.SUCCESS
        assert run.files_scanned == 7
        assert run.files_skipped == 2
        assert run.finished_at is not None

    def test_abandoned_runs_are_detectable(self, repo: Repository):
        crashed = repo.start_run(mode="sync", corpus_id=CORPUS, root_dir="/docs")
        finished = repo.start_run(mode="sync", corpus_id=CORPUS, root_dir="/docs")
        repo.finish_run(finished, status=RunStatus.SUCCESS)

        assert [run.id for run in repo.abandoned_runs(CORPUS)] == [crashed]


class TestFiles:
    def _upsert(self, repo: Repository, run_id: int, **overrides):
        params = {
            "corpus_id": CORPUS,
            "rel_path": "a.pdf",
            "abs_path": "/docs/a.pdf",
            "filename": "a.pdf",
            "content_type": "application/pdf",
            "size": 100,
            "mtime_ns": 1_000,
            "content_hash": "hash-1",
            "run_id": run_id,
        }
        params.update(overrides)
        return repo.upsert_file(**params)

    def test_insert_then_update(self, repo: Repository):
        run_id = repo.start_run(mode="scan-only", corpus_id=CORPUS, root_dir="/docs")
        record = self._upsert(repo, run_id)

        assert record.sync_state == SyncState.NEW
        assert record.document_id is None
        assert record.content_hash == "hash-1"

        updated = self._upsert(repo, run_id, size=200, mtime_ns=2_000, content_hash="hash-2")
        assert updated.id == record.id
        assert updated.size == 200
        assert updated.content_hash == "hash-2"
        assert repo.count_files(CORPUS) == 1

    def test_upsert_preserves_engine_owned_columns(self, repo: Repository):
        """Re-scanning a file must not forget which document it maps to."""
        run_id = repo.start_run(mode="scan-only", corpus_id=CORPUS, root_dir="/docs")
        record = self._upsert(repo, run_id)
        repo.set_document_id(record.id, "doc-uid-1", remote_status="READY")
        repo.set_sync_state(record.id, SyncState.SYNCED, synced=True)

        rescanned = self._upsert(repo, run_id, size=300, mtime_ns=3_000, content_hash="h3")
        assert rescanned.document_id == "doc-uid-1"
        assert rescanned.sync_state == SyncState.SYNCED
        assert rescanned.remote_status == "READY"
        assert rescanned.last_synced_at is not None

    def test_content_changed_fast_path(self, repo: Repository):
        run_id = repo.start_run(mode="scan-only", corpus_id=CORPUS, root_dir="/docs")
        record = self._upsert(repo, run_id)

        assert record.content_changed(100, 1_000) is False
        assert record.content_changed(101, 1_000) is True
        assert record.content_changed(100, 1_001) is True

    def test_document_id_is_unique(self, repo: Repository):
        run_id = repo.start_run(mode="scan-only", corpus_id=CORPUS, root_dir="/docs")
        first = self._upsert(repo, run_id, rel_path="a.pdf")
        second = self._upsert(repo, run_id, rel_path="b.pdf")

        repo.set_document_id(first.id, "doc-uid-1")
        with pytest.raises(DbError):
            repo.set_document_id(second.id, "doc-uid-1")

    def test_multiple_null_document_ids_allowed(self, repo: Repository):
        """The uniqueness index must be partial, or only one file could be NEW."""
        run_id = repo.start_run(mode="scan-only", corpus_id=CORPUS, root_dir="/docs")
        self._upsert(repo, run_id, rel_path="a.pdf")
        self._upsert(repo, run_id, rel_path="b.pdf")
        assert repo.count_files(CORPUS) == 2

    def test_files_not_seen_in_run(self, repo: Repository):
        first_run = repo.start_run(mode="scan-only", corpus_id=CORPUS, root_dir="/docs")
        self._upsert(repo, first_run, rel_path="stays.pdf")
        self._upsert(repo, first_run, rel_path="vanishes.pdf")

        second_run = repo.start_run(mode="scan-only", corpus_id=CORPUS, root_dir="/docs")
        self._upsert(repo, second_run, rel_path="stays.pdf")

        missing = repo.files_not_seen_in_run(CORPUS, second_run)
        assert [record.rel_path for record in missing] == ["vanishes.pdf"]

    def test_corpora_are_isolated(self, repo: Repository):
        run_id = repo.start_run(mode="scan-only", corpus_id=CORPUS, root_dir="/docs")
        other = "550e8400-e29b-41d4-a716-4466554400ff"
        self._upsert(repo, run_id, rel_path="a.pdf")
        self._upsert(repo, run_id, corpus_id=other, rel_path="a.pdf")

        assert repo.count_files(CORPUS) == 1
        assert repo.count_files(other) == 1

    def test_attempts_and_state(self, repo: Repository):
        run_id = repo.start_run(mode="scan-only", corpus_id=CORPUS, root_dir="/docs")
        record = self._upsert(repo, run_id)

        assert repo.increment_attempts(record.id, "boom") == 1
        assert repo.increment_attempts(record.id, "boom again") == 2

        repo.set_sync_state(record.id, SyncState.FAILED, last_error="boom again")
        assert repo.files_in_state(CORPUS, SyncState.FAILED)[0].attempts == 2

        repo.reset_attempts(record.id)
        refreshed = repo.get_file_by_rel_path(CORPUS, "a.pdf")
        assert refreshed.attempts == 0
        assert refreshed.last_error is None

    def test_rejects_invalid_sync_state(self, repo: Repository):
        """The CHECK constraint is the backstop against a typo'd state name."""
        run_id = repo.start_run(mode="scan-only", corpus_id=CORPUS, root_dir="/docs")
        record = self._upsert(repo, run_id)
        with pytest.raises(sqlite3.IntegrityError):
            repo.connection.execute(
                "UPDATE file SET sync_state = 'BOGUS' WHERE id = ?", (record.id,)
            )


class TestEvents:
    def test_records_and_reads_back(self, repo: Repository):
        run_id = repo.start_run(mode="scan-only", corpus_id=CORPUS, root_dir="/docs")
        repo.record_event(
            run_id=run_id,
            event_type="file.skipped",
            message="big.pdf too large",
            level="INFO",
            details={"size": 999, "reason": "TOO_LARGE"},
        )

        events = repo.events_for_run(run_id)
        assert len(events) == 1
        assert events[0].event_type == "file.skipped"
        assert '"size": 999' in events[0].details


class TestTransaction:
    def test_rolls_back_on_error(self, repo: Repository):
        run_id = repo.start_run(mode="scan-only", corpus_id=CORPUS, root_dir="/docs")
        with pytest.raises(RuntimeError):
            with repo.transaction():
                repo.upsert_file(
                    corpus_id=CORPUS,
                    rel_path="a.pdf",
                    abs_path="/docs/a.pdf",
                    filename="a.pdf",
                    content_type="application/pdf",
                    size=1,
                    mtime_ns=1,
                    content_hash="h",
                    run_id=run_id,
                )
                raise RuntimeError("boom")

        assert repo.count_files(CORPUS) == 0


class TestConnectionFailures:
    def test_a_path_that_cannot_be_created(self, tmp_path: Path):
        blocker = tmp_path / "afile"
        blocker.write_text("x")
        with pytest.raises(DbError, match="cannot open database"):
            connect(blocker / "nested" / "sync.db")


class TestTransactionCommit:
    def test_commits_on_success(self, repo: Repository):
        run_id = repo.start_run(mode="sync", corpus_id=CORPUS, root_dir="/docs")
        with repo.transaction():
            repo.upsert_file(
                corpus_id=CORPUS,
                rel_path="a.pdf",
                abs_path="/docs/a.pdf",
                filename="a.pdf",
                content_type="application/pdf",
                size=1,
                mtime_ns=1,
                content_hash="h",
                run_id=run_id,
            )
        assert repo.count_files(CORPUS) == 1


class TestEmptyArgumentGuards:
    def test_files_in_state_with_no_states(self, repo: Repository):
        assert repo.files_in_state(CORPUS) == []

    def test_last_run_of_modes_with_no_modes(self, repo: Repository):
        assert repo.last_run_of_modes(CORPUS, ()) is None

    def test_last_run_of_modes_with_no_match(self, repo: Repository):
        run_id = repo.start_run(mode="sync", corpus_id=CORPUS, root_dir="/docs")
        repo.finish_run(run_id, status=RunStatus.SUCCESS)
        assert repo.last_run_of_modes(CORPUS, ("rebuild-db",)) is None

    def test_synced_hash_can_be_cleared(self, repo: Repository):
        run_id = repo.start_run(mode="sync", corpus_id=CORPUS, root_dir="/docs")
        record = repo.upsert_file(
            corpus_id=CORPUS,
            rel_path="a.pdf",
            abs_path="/docs/a.pdf",
            filename="a.pdf",
            content_type="application/pdf",
            size=1,
            mtime_ns=1,
            content_hash="h",
            run_id=run_id,
        )
        repo.set_synced_hash(record.id, "abc")
        assert repo.get_file_by_rel_path(CORPUS, "a.pdf").synced_hash == "abc"
        repo.set_synced_hash(record.id, None)
        assert repo.get_file_by_rel_path(CORPUS, "a.pdf").synced_hash is None


class TestRemoteStatus:
    @pytest.mark.parametrize(
        ("status", "terminal"),
        [
            ("AWAITING_UPLOAD", False),
            ("PENDING", False),
            ("PROCESSING", False),
            ("READY", True),
            ("FAILED", True),
        ],
    )
    def test_is_terminal(self, status, terminal):
        from verbatim_sync.db.models import RemoteStatus

        assert RemoteStatus(status).is_terminal is terminal


class TestMigrationFailure:
    def test_a_broken_migration_raises_dberror(self, tmp_path: Path, monkeypatch):
        import sqlite3 as sqlite3_module

        from verbatim_sync.db import migrations

        def explode(connection):
            raise sqlite3_module.OperationalError("disk image is malformed")

        monkeypatch.setattr(
            migrations, "_MIGRATIONS", [("001_broken", explode)]
        )
        monkeypatch.setattr(migrations, "SCHEMA_VERSION", 1)

        connection = connect(tmp_path / "sync.db")
        try:
            with pytest.raises(DbError, match="migration 001_broken failed"):
                migrations.migrate(connection)
            # The version is left untouched, so the next run replays it.
            assert schema_version(connection) == 0
        finally:
            connection.close()


class TestConcurrentAccess:
    """The sync engine drives one connection from a pool of worker threads."""

    def test_concurrent_upserts_all_land(self, repo: Repository):
        """Regression: _execute used to hand a live cursor back to the caller,
        so a read could be fetched while another thread ran statements on the
        shared connection — which returns an empty row rather than raising."""
        from concurrent.futures import ThreadPoolExecutor

        run_id = repo.start_run(mode="sync", corpus_id=CORPUS, root_dir="/docs")

        def upsert(index: int):
            return repo.upsert_file(
                corpus_id=CORPUS,
                rel_path=f"bulk/f{index:04d}.pdf",
                abs_path=f"/docs/bulk/f{index:04d}.pdf",
                filename=f"f{index:04d}.pdf",
                content_type="application/pdf",
                size=index,
                mtime_ns=index,
                content_hash=f"hash-{index}",
                run_id=run_id,
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            records = list(pool.map(upsert, range(200)))

        assert len(records) == 200
        assert all(record is not None for record in records)
        assert repo.count_files(CORPUS) == 200
        # Each worker must have read back its own row, not a neighbour's.
        assert {r.content_hash for r in records} == {f"hash-{i}" for i in range(200)}

    def test_concurrent_reads_and_writes(self, repo: Repository):
        from concurrent.futures import ThreadPoolExecutor

        run_id = repo.start_run(mode="sync", corpus_id=CORPUS, root_dir="/docs")
        record = repo.upsert_file(
            corpus_id=CORPUS,
            rel_path="a.pdf",
            abs_path="/docs/a.pdf",
            filename="a.pdf",
            content_type="application/pdf",
            size=1,
            mtime_ns=1,
            content_hash="h",
            run_id=run_id,
        )

        def churn(index: int):
            repo.record_event(run_id=run_id, event_type="noise", message=str(index))
            repo.set_synced_hash(record.id, f"hash-{index}")
            found = repo.get_file_by_rel_path(CORPUS, "a.pdf")
            assert found is not None
            return len(repo.all_files(CORPUS))

        with ThreadPoolExecutor(max_workers=8) as pool:
            counts = list(pool.map(churn, range(200)))

        assert set(counts) == {1}
        assert len(repo.events_for_run(run_id)) == 200

    def test_concurrent_attempt_counters_do_not_lose_increments(self, repo: Repository):
        from concurrent.futures import ThreadPoolExecutor

        run_id = repo.start_run(mode="sync", corpus_id=CORPUS, root_dir="/docs")
        record = repo.upsert_file(
            corpus_id=CORPUS,
            rel_path="a.pdf",
            abs_path="/docs/a.pdf",
            filename="a.pdf",
            content_type="application/pdf",
            size=1,
            mtime_ns=1,
            content_hash="h",
            run_id=run_id,
        )

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(lambda _: repo.increment_attempts(record.id, "boom"), range(100))
            )

        # UPDATE ... RETURNING must give each caller a distinct value.
        assert sorted(results) == list(range(1, 101))
        assert repo.get_file_by_rel_path(CORPUS, "a.pdf").attempts == 100
