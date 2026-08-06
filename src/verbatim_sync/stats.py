"""Summarise the state of a sync from the local database.

Everything here is answered from SQLite alone — ``--stats`` never contacts the
corpus, so it is safe to run at any time, including while a sync is in flight.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from verbatim_sync.config import Config
from verbatim_sync.db.models import SyncRun, SyncState
from verbatim_sync.db.repository import Repository

#: States meaning "the corpus does not hold this file's current content yet".
PENDING_STATES = (
    SyncState.NEW,
    SyncState.PENDING_UPLOAD,
    SyncState.UPLOADED,
    SyncState.COMMITTED,
)

#: Modes whose runs walk the tree, and so know how many files were excluded.
_SCANNING_MODES = ("sync", "scan-only")

_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def format_size(num_bytes: int) -> str:
    """Human-readable size in SI units, matching the config's ``50MB`` sense."""
    if num_bytes < 1000:
        return f"{num_bytes} B"
    value = float(num_bytes)
    for unit in _UNITS[1:]:
        value /= 1000.0
        if value < 1000.0:
            return f"{value:.2f} {unit}"
    return f"{value:.2f} {_UNITS[-1]}"


@dataclass(frozen=True)
class Stats:
    corpus_id: str
    root_dir: str
    database: str
    by_state: dict[str, int] = field(default_factory=dict)
    synced_bytes: int = 0
    last_synced_at: str | None = None
    excluded_last_run: int | None = None
    excluded_run_at: str | None = None
    recent_runs: list[SyncRun] = field(default_factory=list)

    @property
    def total_files(self) -> int:
        return sum(self.by_state.values())

    @property
    def synced_files(self) -> int:
        return self.by_state.get(SyncState.SYNCED.value, 0)

    @property
    def pending_files(self) -> int:
        return sum(self.by_state.get(state.value, 0) for state in PENDING_STATES)

    @property
    def failed_files(self) -> int:
        return self.by_state.get(SyncState.FAILED.value, 0)

    @property
    def missing_files(self) -> int:
        return self.by_state.get(SyncState.MISSING_LOCAL.value, 0)


def collect(config: Config, repository: Repository, run_limit: int = 5) -> Stats:
    corpus_id = config.corpus.id
    last_scanning_run = repository.last_run_of_modes(corpus_id, _SCANNING_MODES)

    return Stats(
        corpus_id=corpus_id,
        root_dir=str(config.source.root_dir),
        database=str(config.database.path),
        by_state=repository.count_by_state(corpus_id),
        synced_bytes=repository.bytes_in_state(corpus_id, SyncState.SYNCED),
        last_synced_at=repository.last_synced_at(corpus_id),
        excluded_last_run=(
            last_scanning_run.files_skipped if last_scanning_run else None
        ),
        excluded_run_at=(last_scanning_run.started_at if last_scanning_run else None),
        recent_runs=repository.last_runs(corpus_id, run_limit),
    )


def render(stats: Stats) -> str:
    """A plain-text report, for stdout."""
    lines = [
        "Verbatim file directory sync — statistics",
        "",
        f"  Corpus     {stats.corpus_id}",
        f"  Tree       {stats.root_dir}",
        f"  Database   {stats.database}",
        "",
        "Files",
        f"  Synced                  {stats.synced_files:>8,}  ({format_size(stats.synced_bytes)})",
        f"  Not synced yet          {stats.pending_files:>8,}",
    ]

    # Break the pending figure down only when there is something to break down,
    # so a healthy corpus reads as three lines rather than eight.
    for state in PENDING_STATES:
        count = stats.by_state.get(state.value, 0)
        if count:
            lines.append(f"    {state.value:<22}{count:>8,}")

    lines += [
        f"  Failed                  {stats.failed_files:>8,}",
    ]
    if stats.missing_files:
        lines.append(f"  Missing locally         {stats.missing_files:>8,}")
    lines += [
        f"  Tracked in total        {stats.total_files:>8,}",
        "",
    ]

    if stats.excluded_last_run is None:
        lines.append("Excluded by filters       (no completed run yet)")
    else:
        when = _short(stats.excluded_run_at)
        lines.append(
            f"Excluded by filters       {stats.excluded_last_run:>8,}  "
            f"(as of the run on {when})"
        )

    lines += ["", f"Last sync                 {_short(stats.last_synced_at) or 'never'}", ""]

    if not stats.recent_runs:
        lines.append("No runs recorded yet.")
        return "\n".join(lines)

    lines.append("Recent runs")
    for run in stats.recent_runs:
        state = run.status if run.finished_at else "RUNNING"
        lines.append(
            f"  #{run.id:<5} {_short(run.started_at):<17} {run.mode:<11} {state:<8}"
            f"  {run.files_scanned:>5} scanned"
            f"  {run.files_uploaded:>4} new"
            f"  {run.files_updated:>4} upd"
            f"  {run.files_deleted:>4} del"
            f"  {run.files_skipped:>4} skip"
            f"  {run.files_failed:>4} fail"
        )
        if run.error:
            lines.append(f"        {run.error}")

    return "\n".join(lines)


def _short(timestamp: str | None) -> str | None:
    """``2026-08-05T21:04:02Z`` -> ``2026-08-05 21:04``."""
    if not timestamp:
        return None
    return timestamp.replace("T", " ").replace("Z", "")[:16]
