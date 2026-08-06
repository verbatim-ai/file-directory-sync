"""Open and migrate the local SQLite state database.

The applied version lives in ``PRAGMA user_version``. Migrations are a plain
ordered list, each one idempotent, so ``migrate()`` can be called on every run
without a separate "is it set up?" check.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path

from verbatim_sync.errors import DbError

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _migration_001_initial(connection: sqlite3.Connection) -> None:
    connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _migration_002_synced_hash(connection: sqlite3.Connection) -> None:
    """Record which content hash the backend actually holds.

    ``content_hash`` tracks what is on disk right now and is rewritten on every
    scan, so on its own it cannot answer "does the corpus already have this?".
    ``synced_hash`` is only advanced once a commit succeeds.
    """
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(file)")}
    if "synced_hash" not in columns:
        connection.execute("ALTER TABLE file ADD COLUMN synced_hash TEXT")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_file_synced_hash "
        "ON file (corpus_id, synced_hash)"
    )


#: Migrations in application order. Index + 1 is the resulting user_version.
#: Each one must be safe to re-run: ``migrate()`` cannot wrap them in a
#: transaction, since DDL via executescript commits implicitly.
_MIGRATIONS: list[tuple[str, Callable[[sqlite3.Connection], None]]] = [
    ("001_initial", _migration_001_initial),
    ("002_synced_hash", _migration_002_synced_hash),
]

SCHEMA_VERSION = len(_MIGRATIONS)


def connect(path: str | Path) -> sqlite3.Connection:
    """Open the state database, creating parent directories as needed."""
    db_path = Path(path)
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # The sync engine hands this connection to a pool of worker threads.
        # Every access goes through Repository, which serialises with a lock,
        # so sqlite3's own same-thread guard would only get in the way.
        connection = sqlite3.connect(
            db_path, isolation_level=None, check_same_thread=False
        )
    except (OSError, sqlite3.Error) as exc:
        raise DbError(f"cannot open database {db_path}: {exc}") from exc

    connection.row_factory = sqlite3.Row
    # WAL keeps a long scan from blocking readers; busy_timeout stops two
    # overlapping cron runs from failing instantly on a locked database.
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA synchronous = NORMAL")
    return connection


def schema_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def migrate(connection: sqlite3.Connection) -> int:
    """Apply any outstanding migrations. Returns the resulting version."""
    current = schema_version(connection)
    if current > SCHEMA_VERSION:
        raise DbError(
            f"database schema version {current} is newer than this build "
            f"supports ({SCHEMA_VERSION}); upgrade verbatim-sync"
        )
    if current == SCHEMA_VERSION:
        return current

    for index in range(current, SCHEMA_VERSION):
        name, apply = _MIGRATIONS[index]
        version = index + 1
        logger.info("Applying database migration %s (-> version %d)", name, version)
        # A crash part-way leaves user_version untouched and the next run
        # replays the migration, which is why each one is idempotent.
        try:
            apply(connection)
            # PRAGMA does not accept a bound parameter; version is an int we
            # derived from the migration list, not user input.
            connection.execute(f"PRAGMA user_version = {version}")
        except sqlite3.Error as exc:
            raise DbError(f"migration {name} failed: {exc}") from exc

    return SCHEMA_VERSION
