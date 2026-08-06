from __future__ import annotations

from pathlib import Path

import pytest
from conftest import CORPUS_ID, FakeBackend

from verbatim_sync.config import load_config
from verbatim_sync.db import Repository, connect, migrate
from verbatim_sync.errors import ApiError
from verbatim_sync.db.models import SyncState
from verbatim_sync.sync import engine, planner
from verbatim_sync.sync.engine import FULLPATH_METADATA_KEY
from verbatim_sync.sync.planner import ActionKind


@pytest.fixture
def env(write_config, source_tree: Path, backend: FakeBackend):
    """A loaded config plus an open repository over a fresh database."""
    config = load_config(write_config())
    connection = connect(config.database.path)
    migrate(connection)
    repository = Repository(connection)

    class Env:
        def __init__(self):
            self.config = config
            self.repository = repository
            self.backend = backend
            self.tree = source_tree
            self._runs = 0

        def sync(self):
            self._runs += 1
            run_id = repository.start_run(
                mode="sync", corpus_id=CORPUS_ID, root_dir=str(source_tree)
            )
            result = planner.plan(config, repository)
            return engine.run(config, repository, backend, run_id, result)

        def plan(self):
            return planner.plan(config, repository)

        def record(self, rel_path: str):
            return repository.get_file_by_rel_path(CORPUS_ID, rel_path)

    yield Env()
    connection.close()


# The default fixture config accepts PDFs under 1KiB, excluding dotfiles, so
# a.pdf and nested/b.pdf are in scope and the rest are filtered out.
IN_SCOPE = {"a.pdf", "nested/b.pdf"}


class TestPlanner:
    def test_new_files_are_uploads(self, env):
        result = env.plan()
        assert {a.rel_path for a in result.of_kind(ActionKind.UPLOAD)} == IN_SCOPE
        assert result.of_kind(ActionKind.REPLACE) == []
        assert result.of_kind(ActionKind.DELETE) == []

    def test_skips_are_reported_with_a_reason(self, env):
        result = env.plan()
        reasons = {s.rel_path: str(s.reason) for s in result.skipped}
        assert "larger than" in reasons["reports/big.pdf"]
        assert "not in filters.content_types" in reasons["notes.txt"]
        assert "could not be determined" in reasons["archive.zzz"]
        assert "smaller than" in reasons["empty.pdf"]
        assert ".hidden.pdf" not in reasons  # excluded before filtering

    def test_planner_writes_nothing(self, env):
        env.plan()
        assert env.repository.count_files(CORPUS_ID) == 0

    def test_synced_files_become_noops(self, env):
        env.sync()
        result = env.plan()
        assert {a.rel_path for a in result.of_kind(ActionKind.NOOP)} == IN_SCOPE
        assert result.changes == []

    def test_changed_content_becomes_a_replace(self, env):
        env.sync()
        (env.tree / "a.pdf").write_bytes(b"%PDF-1.4 revised")

        actions = env.plan().of_kind(ActionKind.REPLACE)
        assert [a.rel_path for a in actions] == ["a.pdf"]
        assert actions[0].reason == "content changed since last sync"

    def test_touched_but_identical_is_a_noop(self, env):
        """mtime moving is not enough; re-uploading identical bytes is waste."""
        env.sync()
        path = env.tree / "a.pdf"
        content = path.read_bytes()
        path.write_bytes(content)  # rewrite identical content, new mtime

        result = env.plan()
        assert result.changes == []
        noop = next(a for a in result.of_kind(ActionKind.NOOP) if a.rel_path == "a.pdf")
        assert noop.reason == "modified but content identical"

    def test_deleted_file_becomes_a_delete(self, env):
        env.sync()
        (env.tree / "nested" / "b.pdf").unlink()

        actions = env.plan().of_kind(ActionKind.DELETE)
        assert [a.rel_path for a in actions] == ["nested/b.pdf"]

    def test_file_that_fell_out_of_scope_is_not_deleted(self, env):
        """Tightening a filter must not silently destroy corpus documents."""
        env.sync()
        (env.tree / "a.pdf").write_bytes(b"x" * 5000)  # now over max_file_size

        result = env.plan()
        assert result.of_kind(ActionKind.DELETE) == []
        assert "a.pdf" in result.out_of_scope

    def test_failed_files_are_retried(self, env):
        env.sync()
        record = env.record("a.pdf")
        env.repository.set_sync_state(record.id, SyncState.FAILED, last_error="boom")

        actions = env.plan().of_kind(ActionKind.REPLACE)
        assert [a.rel_path for a in actions] == ["a.pdf"]
        assert actions[0].reason == "previous attempt failed"

    def test_in_flight_files_are_resumed(self, env):
        env.sync()
        record = env.record("a.pdf")
        env.repository.set_sync_state(record.id, SyncState.COMMITTED)

        actions = env.plan().of_kind(ActionKind.RESUME)
        assert [a.rel_path for a in actions] == ["a.pdf"]


class TestUpload:
    def test_pushes_new_files_and_records_the_mapping(self, env):
        outcome = env.sync()

        assert outcome.ok
        assert outcome.counters["uploaded"] == 2
        assert len(env.backend.documents) == 2

        record = env.record("a.pdf")
        assert record.document_id is not None
        assert record.sync_state == SyncState.SYNCED
        assert record.remote_status == "READY"
        assert record.synced_hash == record.content_hash
        assert record.last_synced_at is not None

    def test_follows_init_put_commit_in_order(self, env):
        """Per document, not globally: workers interleave across files."""
        env.sync()
        document_id = env.record("a.pdf").document_id
        # The presigned URL embeds the document id, so PUTs are attributable.
        sequence = [
            call
            for call, target in env.backend.calls
            if document_id in target
        ]
        assert sequence == ["init", "put", "commit", "status"]

    def test_uploads_the_real_bytes(self, env):
        env.sync()
        document_id = env.record("a.pdf").document_id
        assert env.backend.content_of(document_id) == (env.tree / "a.pdf").read_bytes()

    def test_stores_the_full_path_in_metadata(self, env):
        """This is what makes rebuild-db possible after losing the database."""
        env.sync()
        document_id = env.record("a.pdf").document_id
        metadata = env.backend.metadata_of(document_id)
        assert metadata[FULLPATH_METADATA_KEY] == str(env.tree / "a.pdf")

    def test_sends_the_file_dates_and_corpus_settings(self, env):
        env.sync()
        document_id = env.record("a.pdf").document_id
        document = env.backend.documents[document_id]
        assert document["contentType"] == "application/pdf"
        assert document["lang"] == env.config.corpus.lang
        assert document["provider"] == env.config.corpus.provider
        assert document["docUpdate"].endswith("Z")
        assert document["docCreate"].endswith("Z")

    def test_second_run_is_a_no_op(self, env):
        env.sync()
        calls_before = len(env.backend.calls)

        outcome = env.sync()
        assert outcome.counters["unchanged"] == 2
        assert outcome.counters["uploaded"] == 0
        assert len(env.backend.calls) == calls_before  # nothing sent


class TestReplace:
    def test_reinitialises_the_same_document(self, env):
        env.sync()
        document_id = env.record("a.pdf").document_id
        (env.tree / "a.pdf").write_bytes(b"%PDF-1.4 revised content")

        outcome = env.sync()

        assert outcome.counters["updated"] == 1
        # Same document id: the corpus keeps the document's identity.
        assert env.record("a.pdf").document_id == document_id
        assert ("reinit", document_id) in env.backend.calls
        assert env.backend.content_of(document_id) == b"%PDF-1.4 revised content"

    def test_updates_the_synced_hash(self, env):
        env.sync()
        first = env.record("a.pdf").synced_hash
        (env.tree / "a.pdf").write_bytes(b"%PDF-1.4 revised content")
        env.sync()

        record = env.record("a.pdf")
        assert record.synced_hash != first
        assert record.synced_hash == record.content_hash

    def test_falls_back_to_a_new_upload_when_the_document_is_gone(self, env):
        """Deleted in the backoffice: re-init 404s, so start over."""
        env.sync()
        stale_id = env.record("a.pdf").document_id
        env.backend.reinit_error = 404
        (env.tree / "a.pdf").write_bytes(b"%PDF-1.4 revised content")

        outcome = env.sync()

        assert outcome.ok
        new_id = env.record("a.pdf").document_id
        assert new_id != stale_id
        assert env.record("a.pdf").sync_state == SyncState.SYNCED


class TestDelete:
    def test_removes_the_document_and_the_row(self, env):
        env.sync()
        document_id = env.record("nested/b.pdf").document_id
        (env.tree / "nested" / "b.pdf").unlink()

        outcome = env.sync()

        assert outcome.counters["deleted"] == 1
        assert document_id not in env.backend.documents
        assert env.record("nested/b.pdf") is None

    def test_tolerates_a_document_already_gone(self, env):
        env.sync()
        document_id = env.record("nested/b.pdf").document_id
        del env.backend.documents[document_id]
        (env.tree / "nested" / "b.pdf").unlink()

        outcome = env.sync()
        assert outcome.ok
        assert env.record("nested/b.pdf") is None

    def test_drops_rows_that_were_never_uploaded(self, env):
        env.repository.upsert_file(
            corpus_id=CORPUS_ID,
            rel_path="ghost.pdf",
            abs_path=str(env.tree / "ghost.pdf"),
            filename="ghost.pdf",
            content_type="application/pdf",
            size=1,
            mtime_ns=1,
            content_hash="h",
        )
        outcome = env.sync()
        assert outcome.counters["deleted"] == 1
        assert env.record("ghost.pdf") is None


class TestFailures:
    def test_ingestion_failure_marks_the_file_and_the_run(self, env):
        # Fail whatever document the next upload creates.
        original_commit = env.backend.commit

        def failing_commit(document_id):
            env.backend.fail_ingestion_for.add(document_id)
            return original_commit(document_id)

        env.backend.commit = failing_commit
        outcome = env.sync()

        assert not outcome.ok
        assert outcome.counters["failed"] == 2
        record = env.record("a.pdf")
        assert record.sync_state == SyncState.FAILED
        assert "ingestion failed" in record.last_error
        assert record.attempts == 1

    def test_one_bad_file_does_not_stop_the_others(self, env):
        original_init = env.backend.init_upload

        def selective_init(**kwargs):
            if kwargs["filename"] == "a.pdf":
                raise OSError("disk gone")
            return original_init(**kwargs)

        env.backend.init_upload = selective_init
        outcome = env.sync()

        assert outcome.counters["failed"] == 1
        assert outcome.counters["uploaded"] == 1
        assert env.record("nested/b.pdf").sync_state == SyncState.SYNCED

    def test_duplicate_content_is_recorded_as_synced(self, env):
        """The corpus already holds these bytes; retrying forever helps nobody."""
        env.backend.detect_duplicates = True
        same = b"%PDF-1.4 identical"
        (env.tree / "a.pdf").write_bytes(same)
        (env.tree / "nested" / "b.pdf").write_bytes(same)

        outcome = env.sync()

        assert outcome.ok
        assert {env.record(p).sync_state for p in IN_SCOPE} == {SyncState.SYNCED}


class TestResume:
    def test_reuses_a_live_presigned_url(self, env):
        """A run that died after init should not need a second init."""
        run_id = env.repository.start_run(
            mode="sync", corpus_id=CORPUS_ID, root_dir=str(env.tree)
        )
        result = planner.plan(env.config, env.repository)
        action = next(a for a in result.actions if a.rel_path == "a.pdf")

        record = engine._record_scan(env.repository, env.config, action, run_id)
        init = env.backend.init_upload(
            corpus_id=CORPUS_ID,
            filename="a.pdf",
            content_type="application/pdf",
        )
        engine._store_init(env.repository, record.id, init)

        inits_before = sum(1 for call, _ in env.backend.calls if call == "init")
        outcome = env.sync()

        assert outcome.ok
        assert env.record("a.pdf").sync_state == SyncState.SYNCED
        # The resumed file reused its URL, so only b.pdf triggered a new init.
        inits_after = sum(1 for call, _ in env.backend.calls if call == "init")
        assert inits_after - inits_before == 1

    def test_reinitialises_when_the_url_expired(self, env):
        env.sync()
        record = env.record("a.pdf")
        env.repository.set_document_id(
            record.id,
            record.document_id,
            upload_url="https://storage.example/stale?sig=x",
            upload_url_expires_at="2020-01-01T00:00:00Z",
        )
        env.repository.set_sync_state(record.id, SyncState.PENDING_UPLOAD)

        outcome = env.sync()
        assert outcome.ok
        assert ("reinit", record.document_id) in env.backend.calls
        assert env.record("a.pdf").sync_state == SyncState.SYNCED


class TestRebuild:
    def test_restores_the_mapping_from_metadata(self, env):
        env.sync()
        expected = {p: env.record(p).document_id for p in IN_SCOPE}

        # Simulate losing the local database entirely.
        for rel_path in IN_SCOPE:
            env.repository.delete_file(env.record(rel_path).id)
        assert env.repository.count_files(CORPUS_ID) == 0

        run_id = env.repository.start_run(
            mode="rebuild-db", corpus_id=CORPUS_ID, root_dir=str(env.tree)
        )
        counters = engine.rebuild(env.config, env.repository, env.backend, run_id)

        assert counters["restored"] == 2
        for rel_path, document_id in expected.items():
            record = env.record(rel_path)
            assert record.document_id == document_id
            assert record.sync_state == SyncState.SYNCED
            assert record.synced_hash is not None

    def test_a_rebuilt_database_produces_no_further_work(self, env):
        env.sync()
        for rel_path in IN_SCOPE:
            env.repository.delete_file(env.record(rel_path).id)

        run_id = env.repository.start_run(
            mode="rebuild-db", corpus_id=CORPUS_ID, root_dir=str(env.tree)
        )
        engine.rebuild(env.config, env.repository, env.backend, run_id)

        assert planner.plan(env.config, env.repository).changes == []

    def test_locally_changed_files_are_marked_for_update(self, env):
        """Rebuild must not claim a file is synced when the sizes disagree."""
        env.sync()
        for rel_path in IN_SCOPE:
            env.repository.delete_file(env.record(rel_path).id)
        (env.tree / "a.pdf").write_bytes(b"%PDF-1.4 changed while the db was gone")

        run_id = env.repository.start_run(
            mode="rebuild-db", corpus_id=CORPUS_ID, root_dir=str(env.tree)
        )
        counters = engine.rebuild(env.config, env.repository, env.backend, run_id)

        assert counters["stale"] == 1
        assert env.record("a.pdf").synced_hash is None
        actions = planner.plan(env.config, env.repository).of_kind(ActionKind.REPLACE)
        assert [a.rel_path for a in actions] == ["a.pdf"]

    def test_skips_documents_without_the_metadata_key(self, env, caplog):
        env.backend.init_upload(
            corpus_id=CORPUS_ID, filename="foreign.pdf", content_type="application/pdf"
        )
        run_id = env.repository.start_run(
            mode="rebuild-db", corpus_id=CORPUS_ID, root_dir=str(env.tree)
        )
        with caplog.at_level("WARNING"):
            counters = engine.rebuild(env.config, env.repository, env.backend, run_id)

        assert counters["no_metadata"] == 1
        assert counters["restored"] == 0
        assert "not managed by this job" in caplog.text

    def test_leaves_documents_whose_file_is_missing(self, env):
        env.sync()
        (env.tree / "a.pdf").unlink()
        for rel_path in IN_SCOPE:
            env.repository.delete_file(env.record(rel_path).id)

        run_id = env.repository.start_run(
            mode="rebuild-db", corpus_id=CORPUS_ID, root_dir=str(env.tree)
        )
        counters = engine.rebuild(env.config, env.repository, env.backend, run_id)

        assert counters["missing_local"] == 1
        assert counters["restored"] == 1
        # The document survives: rebuild reads, it does not prune.
        assert len(env.backend.documents) == 2

    def test_skips_documents_pointing_outside_the_tree(self, env, tmp_path):
        env.backend.init_upload(
            corpus_id=CORPUS_ID,
            filename="outside.pdf",
            content_type="application/pdf",
            metadata={FULLPATH_METADATA_KEY: str(tmp_path / "elsewhere.pdf")},
        )
        run_id = env.repository.start_run(
            mode="rebuild-db", corpus_id=CORPUS_ID, root_dir=str(env.tree)
        )
        counters = engine.rebuild(env.config, env.repository, env.backend, run_id)
        assert counters["outside_tree"] == 1


class TestPolling:
    def _config_with(self, write_config, source_tree, keys_dir, api_section, **sync):
        body = (
            f'[source]\nroot_dir = "{source_tree}"\nexclude = ["**/.*"]\n'
            '[corpus]\nid = "550e8400-e29b-41d4-a716-446655440001"\n'
            '[filters]\ncontent_types = ["application/pdf"]\n'
            'max_file_size = "1KiB"\nmin_file_size = "1B"\n'
            '[database]\npath = "./state/sync.db"\n'
            '[logging]\nfile = "./logs/sync.log"\nconsole = false\n'
            "[sync]\n"
            + "".join(f"{k} = {str(v).lower()}\n" for k, v in sync.items())
        ) + api_section
        return load_config(write_config(body, name="poll.toml"))

    def test_waits_for_a_document_that_is_not_ready_yet(self, env):
        """PROCESSING must be polled through, not mistaken for a failure."""
        original = env.backend.status
        polls: dict[str, int] = {}

        def slow_status(document_id):
            polls[document_id] = polls.get(document_id, 0) + 1
            env.backend.documents[document_id]["status"] = (
                "READY" if polls[document_id] >= 3 else "PROCESSING"
            )
            return original(document_id)

        env.backend.status = slow_status
        outcome = env.sync()

        assert outcome.ok
        assert all(count >= 3 for count in polls.values())
        assert {env.record(p).sync_state for p in IN_SCOPE} == {SyncState.SYNCED}

    def test_gives_up_after_the_poll_timeout(self, env, monkeypatch):
        """A slow ingestion is left COMMITTED for the next run to resume."""
        import itertools

        import verbatim_sync.sync.engine as engine_module

        original = env.backend.status

        def never_ready(document_id):
            env.backend.documents[document_id]["status"] = "PROCESSING"
            return original(document_id)

        env.backend.status = never_ready
        # A clock that leaps past any deadline, however many times it is read.
        ticks = itertools.count(0, 10_000)
        monkeypatch.setattr(engine_module.time, "monotonic", lambda: next(ticks))

        outcome = env.sync()

        assert outcome.ok  # not a failure; just unfinished
        assert env.record("a.pdf").sync_state == SyncState.COMMITTED
        # The bytes are in the backend, so the hash is recorded either way.
        assert env.record("a.pdf").synced_hash is not None

    def test_poll_status_false_marks_synced_without_waiting(
        self, write_config, source_tree, keys_dir, api_section, backend, tmp_path
    ):
        config = self._config_with(
            write_config, source_tree, keys_dir, api_section, poll_status=False
        )
        connection = connect(config.database.path)
        migrate(connection)
        repository = Repository(connection)
        try:
            run_id = repository.start_run(
                mode="sync", corpus_id=CORPUS_ID, root_dir=str(source_tree)
            )
            result = planner.plan(config, repository)
            outcome = engine.run(config, repository, backend, run_id, result)

            assert outcome.ok
            record = repository.get_file_by_rel_path(CORPUS_ID, "a.pdf")
            assert record.sync_state == SyncState.SYNCED
            assert record.remote_status == "PROCESSING"
            assert not any(call == "status" for call, _ in backend.calls)
        finally:
            connection.close()


class TestUrlExpiry:
    @pytest.mark.parametrize(
        ("expires_at", "live"),
        [
            (None, False),
            ("", False),
            ("not-a-timestamp", False),
            ("2020-01-01T00:00:00Z", False),
            ("2999-01-01T00:00:00Z", True),
            ("2999-01-01T00:00:00", True),  # naive, treated as UTC
            ("2999-01-01T00:00:00+00:00", True),
        ],
    )
    def test_liveness(self, expires_at, live):
        assert engine._url_is_live(expires_at) is live


class TestEngineFailurePaths:
    def test_a_delete_error_other_than_404_fails_the_file(self, env):
        env.sync()
        (env.tree / "nested" / "b.pdf").unlink()

        def refuse(document_id):
            raise ApiError("server said no", status_code=500)

        env.backend.delete = refuse
        outcome = env.sync()

        assert not outcome.ok
        assert "nested/b.pdf" in outcome.failures
        # The row survives so the next run can retry the deletion.
        assert env.record("nested/b.pdf") is not None

    def test_a_reinit_error_other_than_404_or_409_propagates(self, env):
        env.sync()
        env.backend.reinit_error = 500
        (env.tree / "a.pdf").write_bytes(b"%PDF-1.4 revised")

        outcome = env.sync()
        assert not outcome.ok
        assert "a.pdf" in outcome.failures
        assert env.record("a.pdf").sync_state == SyncState.FAILED

    def test_an_unreadable_file_is_reported_not_uploaded(self, env, monkeypatch):
        import verbatim_sync.sync.planner as planner_module

        def unreadable(path, chunk_size=1 << 20):
            if str(path).endswith("a.pdf"):
                raise OSError("input/output error")
            return "hash-" + Path(path).name

        monkeypatch.setattr(planner_module, "hash_file", unreadable)
        result = env.plan()

        assert result.unreadable == ["a.pdf"]
        assert all(a.rel_path != "a.pdf" for a in result.actions)


class TestRebuildEdgeCases:
    def test_an_unreadable_local_file_is_counted(self, env, monkeypatch):
        env.sync()
        for rel_path in IN_SCOPE:
            env.repository.delete_file(env.record(rel_path).id)

        import verbatim_sync.sync.engine as engine_module

        def unreadable(path, chunk_size=1 << 20):
            raise OSError("input/output error")

        monkeypatch.setattr(engine_module, "hash_file", unreadable)
        run_id = env.repository.start_run(
            mode="rebuild-db", corpus_id=CORPUS_ID, root_dir=str(env.tree)
        )
        counters = engine.rebuild(env.config, env.repository, env.backend, run_id)

        assert counters["unreadable"] == 2
        assert counters["restored"] == 0

    def test_a_document_never_ingested_is_restored_as_synced(self, env):
        """size is only set after ingestion, so its absence cannot mean 'differs'."""
        env.backend.init_upload(
            corpus_id=CORPUS_ID,
            filename="a.pdf",
            content_type="application/pdf",
            metadata={FULLPATH_METADATA_KEY: str(env.tree / "a.pdf")},
        )
        run_id = env.repository.start_run(
            mode="rebuild-db", corpus_id=CORPUS_ID, root_dir=str(env.tree)
        )
        counters = engine.rebuild(env.config, env.repository, env.backend, run_id)

        assert counters["restored"] == 1
        assert env.record("a.pdf").sync_state == SyncState.SYNCED


class TestResumeFromEachState:
    """A run can die at any point in init -> PUT -> commit -> poll. Each
    resting place needs its own way back."""

    def _stage(self, env, rel_path: str, state: SyncState):
        run_id = env.repository.start_run(
            mode="sync", corpus_id=CORPUS_ID, root_dir=str(env.tree)
        )
        result = planner.plan(env.config, env.repository)
        action = next(a for a in result.actions if a.rel_path == rel_path)
        record = engine._record_scan(env.repository, env.config, action, run_id)
        init = env.backend.init_upload(
            corpus_id=CORPUS_ID,
            filename=Path(rel_path).name,
            content_type="application/pdf",
        )
        engine._store_init(env.repository, record.id, init)
        return record, init, action

    def test_resumes_from_uploaded_by_committing(self, env):
        """The bytes are in storage; only the commit was missed."""
        record, init, action = self._stage(env, "a.pdf", SyncState.UPLOADED)
        env.backend.upload_content(
            init.upload_url, (env.tree / "a.pdf").read_bytes(), "application/pdf"
        )
        env.repository.set_sync_state(record.id, SyncState.UPLOADED)
        env.repository.clear_upload_url(record.id)

        calls_before = len(env.backend.calls)
        outcome = env.sync()

        assert outcome.ok
        assert env.record("a.pdf").sync_state == SyncState.SYNCED
        # It committed the existing document rather than starting over.
        new_calls = env.backend.calls[calls_before:]
        assert ("commit", init.document.id) in new_calls
        assert not any(c == "reinit" for c, _ in new_calls)

    def test_resumes_from_committed_by_polling(self, env):
        """Ingestion was queued; only its outcome is unknown."""
        record, init, action = self._stage(env, "a.pdf", SyncState.COMMITTED)
        env.backend.upload_content(
            init.upload_url, (env.tree / "a.pdf").read_bytes(), "application/pdf"
        )
        env.backend.commit(init.document.id)
        env.repository.set_sync_state(record.id, SyncState.COMMITTED)

        calls_before = len(env.backend.calls)
        outcome = env.sync()

        assert outcome.ok
        assert env.record("a.pdf").sync_state == SyncState.SYNCED
        new_calls = env.backend.calls[calls_before:]
        # Polled only; nothing was re-sent.
        assert ("status", init.document.id) in new_calls
        assert not any(c in ("init", "reinit", "put") for c, i in new_calls if i == init.document.id)

    def test_a_committed_file_whose_ingestion_failed_is_reported(self, env):
        record, init, action = self._stage(env, "a.pdf", SyncState.COMMITTED)
        env.backend.upload_content(
            init.upload_url, (env.tree / "a.pdf").read_bytes(), "application/pdf"
        )
        env.backend.fail_ingestion_for.add(init.document.id)
        env.backend.commit(init.document.id)
        env.repository.set_sync_state(record.id, SyncState.COMMITTED)

        outcome = env.sync()

        assert not outcome.ok
        assert "a.pdf" in outcome.failures
        assert env.record("a.pdf").sync_state == SyncState.FAILED


class TestCommitErrors:
    def test_a_non_409_commit_error_fails_the_file(self, env):
        def refuse(document_id):
            raise ApiError("server exploded", status_code=500)

        env.backend.commit = refuse
        outcome = env.sync()

        assert not outcome.ok
        assert outcome.counters["failed"] == 2
        assert env.record("a.pdf").sync_state == SyncState.FAILED
        assert "server exploded" in env.record("a.pdf").last_error
        # No commit succeeded, so nothing may claim to be in the corpus.
        assert env.record("a.pdf").synced_hash is None


class TestPlannerStateEdges:
    def test_a_row_without_a_document_id_is_an_upload(self, env):
        """--scan-only records files before anything is uploaded."""
        env.repository.upsert_file(
            corpus_id=CORPUS_ID,
            rel_path="a.pdf",
            abs_path=str(env.tree / "a.pdf"),
            filename="a.pdf",
            content_type="application/pdf",
            size=(env.tree / "a.pdf").stat().st_size,
            mtime_ns=(env.tree / "a.pdf").stat().st_mtime_ns,
            content_hash="whatever",
        )
        actions = env.plan().of_kind(ActionKind.UPLOAD)
        action = next(a for a in actions if a.rel_path == "a.pdf")
        assert action.reason == "no document id recorded"

    def test_a_synced_row_without_a_synced_hash_is_replaced(self, env):
        env.sync()
        record = env.record("a.pdf")
        env.repository.set_synced_hash(record.id, None)

        actions = env.plan().of_kind(ActionKind.REPLACE)
        assert [a.rel_path for a in actions] == ["a.pdf"]
        assert actions[0].reason == "no synced content recorded"


def _many_files_config(write_config, source_tree: Path, api_section: str, threads: int, count: int):
    """A tree of `count` PDFs plus a config with the given thread count."""
    bulk = source_tree / "bulk"
    bulk.mkdir(exist_ok=True)
    for i in range(count):
        (bulk / f"f{i:03d}.pdf").write_bytes(f"%PDF-1.4 body {i}".encode())

    body = (
        f'[source]\nroot_dir = "{source_tree}"\nexclude = ["**/.*"]\n'
        '[corpus]\nid = "550e8400-e29b-41d4-a716-446655440001"\n'
        '[filters]\ncontent_types = ["application/pdf"]\n'
        'max_file_size = "1KiB"\nmin_file_size = "1B"\n'
        # A database per thread count, so two runs in one test start clean.
        f'[database]\npath = "./state/sync-{threads}.db"\n'
        '[logging]\nfile = "./logs/sync.log"\nconsole = false\n'
        f"[sync]\nthreads = {threads}\n"
    ) + api_section
    return load_config(write_config(body, name=f"threads{threads}.toml"))


@pytest.fixture
def bulk_env(write_config, source_tree, api_section, backend):
    """Build a sync environment over a large tree at a chosen thread count."""

    def _build(threads: int, count: int = 20):
        config = _many_files_config(write_config, source_tree, api_section, threads, count)
        connection = connect(config.database.path)
        migrate(connection)
        repository = Repository(connection)

        def sync():
            run_id = repository.start_run(
                mode="sync", corpus_id=CORPUS_ID, root_dir=str(config.source.root_dir)
            )
            result = planner.plan(config, repository)
            return engine.run(config, repository, backend, run_id, result)

        return config, repository, backend, sync, connection

    return _build


class TestThreading:
    def test_every_file_is_synced_exactly_once(self, bulk_env):
        config, repository, backend, sync, connection = bulk_env(threads=8, count=30)
        try:
            outcome = sync()

            assert outcome.ok
            assert outcome.counters["uploaded"] == 32  # 30 bulk + a.pdf + nested/b.pdf
            assert len(backend.documents) == 32
            # One init per file, no duplicates from a race.
            inits = [t for c, t in backend.calls if c == "init"]
            assert len(inits) == len(set(inits)) == 32
        finally:
            connection.close()

    def test_results_match_the_single_threaded_run(self, bulk_env):
        """Concurrency must not change the outcome, only the wall time."""
        config, repository, backend, sync, connection = bulk_env(threads=1, count=15)
        try:
            serial = sync()
            serial_state = {
                r.rel_path: (r.sync_state, r.synced_hash)
                for r in repository.all_files(CORPUS_ID)
            }
        finally:
            connection.close()

        config, repository, backend, sync, connection = bulk_env(threads=8, count=15)
        try:
            parallel = sync()
            parallel_state = {
                r.rel_path: (r.sync_state, r.synced_hash)
                for r in repository.all_files(CORPUS_ID)
            }
        finally:
            connection.close()

        assert parallel.counters == serial.counters
        assert parallel_state == serial_state

    def test_work_actually_runs_in_parallel(self, bulk_env):
        """Guard against the pool silently degrading to serial execution."""
        import threading

        config, repository, backend, sync, connection = bulk_env(threads=5, count=20)
        try:
            active = 0
            peak = 0
            lock = threading.Lock()
            barrier = threading.Event()
            original = backend.init_upload

            def slow_init(**kwargs):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                # Hold long enough for siblings to pile up, but never block
                # the whole suite if only one thread ever arrives.
                barrier.wait(timeout=0.25)
                with lock:
                    active -= 1
                return original(**kwargs)

            backend.init_upload = slow_init
            sync()

            assert peak > 1, "no two files were ever in flight at once"
            assert peak <= 5, f"more workers ran than configured: {peak}"
        finally:
            connection.close()

    def test_thread_count_is_capped_by_the_work_available(self, bulk_env, caplog):
        config, repository, backend, sync, connection = bulk_env(threads=16, count=0)
        try:
            with caplog.at_level("INFO"):
                sync()
            # Only a.pdf and nested/b.pdf are in scope, so 2 workers, not 16.
            assert "with 2 worker thread(s)" in caplog.text
        finally:
            connection.close()

    def test_failures_are_isolated_across_threads(self, bulk_env):
        config, repository, backend, sync, connection = bulk_env(threads=8, count=20)
        try:
            original = backend.init_upload

            def selective(**kwargs):
                if kwargs["filename"].endswith(("0.pdf", "5.pdf")):
                    raise OSError("disk gone")
                return original(**kwargs)

            backend.init_upload = selective
            outcome = sync()

            assert not outcome.ok
            assert outcome.counters["failed"] == 4  # f000, f005, f010, f015
            assert outcome.counters["uploaded"] == 18
            assert len(outcome.failures) == 4
        finally:
            connection.close()

    def test_failures_are_reported_in_a_stable_order(self, bulk_env):
        """Completion order varies; the run summary must not."""
        orders = []
        for _ in range(3):
            config, repository, backend, sync, connection = bulk_env(threads=8, count=20)
            try:
                original = backend.init_upload

                def selective(**kwargs):
                    if kwargs["filename"].endswith(("1.pdf", "7.pdf")):
                        raise OSError("disk gone")
                    return original(**kwargs)

                backend.init_upload = selective
                orders.append(sync().failures)
            finally:
                connection.close()

        assert orders[0] == orders[1] == orders[2]
        assert orders[0] == sorted(orders[0])

    def test_a_bug_in_a_worker_is_not_swallowed(self, bulk_env):
        """Only ApiError and OSError mean 'bad file'; anything else is a defect."""
        config, repository, backend, sync, connection = bulk_env(threads=4, count=10)
        try:
            def explode(**kwargs):
                raise ZeroDivisionError("programmer error")

            backend.init_upload = explode
            with pytest.raises(ZeroDivisionError):
                sync()
        finally:
            connection.close()

    def test_single_thread_avoids_the_pool_entirely(self, bulk_env, monkeypatch):
        import verbatim_sync.sync.engine as engine_module

        def forbidden(*args, **kwargs):
            raise AssertionError("threads = 1 must not create a pool")

        monkeypatch.setattr(engine_module, "ThreadPoolExecutor", forbidden)
        config, repository, backend, sync, connection = bulk_env(threads=1, count=5)
        try:
            assert sync().ok
        finally:
            connection.close()

    def test_deletions_run_concurrently_too(self, bulk_env):
        config, repository, backend, sync, connection = bulk_env(threads=6, count=12)
        try:
            sync()
            for path in (config.source.root_dir / "bulk").glob("*.pdf"):
                path.unlink()

            outcome = sync()
            assert outcome.ok
            assert outcome.counters["deleted"] == 12
            assert len(backend.documents) == 2  # only a.pdf and nested/b.pdf remain
        finally:
            connection.close()
