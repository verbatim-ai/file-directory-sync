from __future__ import annotations

from pathlib import Path

import pytest
from conftest import CORPUS_ID

from verbatim_sync.config import load_config
from verbatim_sync.db import Repository, connect, migrate
from verbatim_sync.db.models import RunStatus, SyncState
from verbatim_sync.stats import collect, format_size, render


class TestFormatSize:
    @pytest.mark.parametrize(
        ("num_bytes", "expected"),
        [
            (0, "0 B"),
            (999, "999 B"),
            (1000, "1.00 KB"),
            (1_500_000, "1.50 MB"),
            (12_400_000_000, "12.40 GB"),
            (3_000_000_000_000, "3.00 TB"),
        ],
    )
    def test_si_units(self, num_bytes, expected):
        assert format_size(num_bytes) == expected


@pytest.fixture
def repo(write_config):
    config = load_config(write_config())
    connection = connect(config.database.path)
    migrate(connection)
    yield config, Repository(connection)
    connection.close()


def add_file(repository, rel_path, *, state, size=1000, run_id=None, synced=False):
    record = repository.upsert_file(
        corpus_id=CORPUS_ID,
        rel_path=rel_path,
        abs_path=f"/docs/{rel_path}",
        filename=rel_path,
        content_type="application/pdf",
        size=size,
        mtime_ns=1,
        content_hash="h" + rel_path,
        run_id=run_id,
    )
    repository.set_sync_state(record.id, state, synced=synced)
    return record


class TestCollect:
    def test_empty_database(self, repo):
        config, repository = repo
        stats = collect(config, repository)

        assert stats.total_files == 0
        assert stats.synced_files == 0
        assert stats.synced_bytes == 0
        assert stats.last_synced_at is None
        assert stats.excluded_last_run is None
        assert stats.recent_runs == []

    def test_counts_and_bytes_by_state(self, repo):
        config, repository = repo
        add_file(repository, "a.pdf", state=SyncState.SYNCED, size=2000, synced=True)
        add_file(repository, "b.pdf", state=SyncState.SYNCED, size=3000, synced=True)
        add_file(repository, "c.pdf", state=SyncState.NEW, size=9999)
        add_file(repository, "d.pdf", state=SyncState.COMMITTED, size=9999)
        add_file(repository, "e.pdf", state=SyncState.FAILED, size=9999)

        stats = collect(config, repository)

        assert stats.synced_files == 2
        # Only synced files count towards the volume actually in the corpus.
        assert stats.synced_bytes == 5000
        assert stats.pending_files == 2  # NEW + COMMITTED
        assert stats.failed_files == 1
        assert stats.total_files == 5
        assert stats.last_synced_at is not None

    def test_missing_local_is_counted_separately(self, repo):
        config, repository = repo
        add_file(repository, "gone.pdf", state=SyncState.MISSING_LOCAL)

        stats = collect(config, repository)
        assert stats.missing_files == 1
        assert stats.pending_files == 0
        assert stats.failed_files == 0

    def test_excluded_count_comes_from_the_last_scanning_run(self, repo):
        config, repository = repo
        old = repository.start_run(mode="sync", corpus_id=CORPUS_ID, root_dir="/docs")
        repository.finish_run(old, status=RunStatus.SUCCESS, counters={"skipped": 3})
        recent = repository.start_run(mode="sync", corpus_id=CORPUS_ID, root_dir="/docs")
        repository.finish_run(recent, status=RunStatus.SUCCESS, counters={"skipped": 7})

        assert collect(config, repository).excluded_last_run == 7

    def test_rebuild_runs_do_not_shadow_the_excluded_count(self, repo):
        """rebuild-db never walks the tree, so its zero would be a lie."""
        config, repository = repo
        sync_run = repository.start_run(
            mode="sync", corpus_id=CORPUS_ID, root_dir="/docs"
        )
        repository.finish_run(sync_run, status=RunStatus.SUCCESS, counters={"skipped": 5})
        rebuild = repository.start_run(
            mode="rebuild-db", corpus_id=CORPUS_ID, root_dir="/docs"
        )
        repository.finish_run(rebuild, status=RunStatus.SUCCESS)

        assert collect(config, repository).excluded_last_run == 5

    def test_unfinished_runs_are_not_used_for_the_excluded_count(self, repo):
        config, repository = repo
        done = repository.start_run(mode="sync", corpus_id=CORPUS_ID, root_dir="/docs")
        repository.finish_run(done, status=RunStatus.SUCCESS, counters={"skipped": 4})
        repository.start_run(mode="sync", corpus_id=CORPUS_ID, root_dir="/docs")

        assert collect(config, repository).excluded_last_run == 4

    def test_recent_runs_are_newest_first_and_capped(self, repo):
        config, repository = repo
        for _ in range(8):
            run_id = repository.start_run(
                mode="sync", corpus_id=CORPUS_ID, root_dir="/docs"
            )
            repository.finish_run(run_id, status=RunStatus.SUCCESS)

        runs = collect(config, repository, run_limit=3).recent_runs
        assert len(runs) == 3
        assert [run.id for run in runs] == sorted((r.id for r in runs), reverse=True)

    def test_other_corpora_are_excluded(self, repo):
        config, repository = repo
        add_file(repository, "mine.pdf", state=SyncState.SYNCED, synced=True)
        repository.upsert_file(
            corpus_id="550e8400-e29b-41d4-a716-4466554400ff",
            rel_path="theirs.pdf",
            abs_path="/docs/theirs.pdf",
            filename="theirs.pdf",
            content_type="application/pdf",
            size=1,
            mtime_ns=1,
            content_hash="x",
        )
        assert collect(config, repository).total_files == 1


class TestRender:
    def test_reports_every_requested_figure(self, repo):
        config, repository = repo
        add_file(repository, "a.pdf", state=SyncState.SYNCED, size=2_500_000, synced=True)
        add_file(repository, "b.pdf", state=SyncState.NEW)
        run_id = repository.start_run(mode="sync", corpus_id=CORPUS_ID, root_dir="/docs")
        repository.finish_run(
            run_id, status=RunStatus.SUCCESS, counters={"scanned": 9, "skipped": 6}
        )

        report = render(collect(config, repository))

        assert "Synced" in report
        assert "2.50 MB" in report  # volume synced
        assert "Not synced yet" in report
        assert "Excluded by filters" in report and "6" in report
        assert "Last sync" in report
        assert "Recent runs" in report
        assert f"#{run_id}" in report

    def test_empty_database_reads_sensibly(self, repo):
        config, repository = repo
        report = render(collect(config, repository))

        assert "no completed run yet" in report
        assert "never" in report
        assert "No runs recorded yet." in report

    def test_pending_breakdown_only_when_relevant(self, repo):
        config, repository = repo
        add_file(repository, "a.pdf", state=SyncState.SYNCED, synced=True)
        assert "PENDING_UPLOAD" not in render(collect(config, repository))

        add_file(repository, "b.pdf", state=SyncState.PENDING_UPLOAD)
        assert "PENDING_UPLOAD" in render(collect(config, repository))

    def test_surfaces_the_error_of_a_failed_run(self, repo):
        config, repository = repo
        run_id = repository.start_run(mode="sync", corpus_id=CORPUS_ID, root_dir="/docs")
        repository.finish_run(run_id, status=RunStatus.FAILED, error="2 file(s) failed")

        report = render(collect(config, repository))
        assert "FAILED" in report
        assert "2 file(s) failed" in report

    def test_marks_an_unfinished_run_as_running(self, repo):
        config, repository = repo
        repository.start_run(mode="sync", corpus_id=CORPUS_ID, root_dir="/docs")
        assert "RUNNING" in render(collect(config, repository))


class TestStatsCommand:
    def test_prints_to_stdout_without_touching_the_network(
        self, write_config, tmp_path: Path, capsys, monkeypatch
    ):
        from verbatim_sync.cli import EXIT_OK, main

        def explode(config):
            raise AssertionError("--stats must not build an API client")

        monkeypatch.setattr("verbatim_sync.cli._build_documents_api", explode)

        config_path = write_config()
        assert main(["--config", str(config_path), "--stats"]) == EXIT_OK

        out = capsys.readouterr().out
        assert "Verbatim file directory sync — statistics" in out
        assert CORPUS_ID in out

    def test_works_before_any_run(self, write_config, capsys):
        from verbatim_sync.cli import EXIT_OK, main

        assert main(["--config", str(write_config()), "--stats"]) == EXIT_OK
        assert "No runs recorded yet." in capsys.readouterr().out


class TestFormatSizeExtremes:
    def test_beyond_the_largest_unit(self):
        assert format_size(5 * 10**18).endswith(" PB")

    def test_the_boundary_of_each_unit(self):
        assert format_size(999_999) == "1000.00 KB"
        assert format_size(1_000_000) == "1.00 MB"
