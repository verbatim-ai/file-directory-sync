"""Local SQLite state: the mapping between local files and corpus documents."""

from verbatim_sync.db.migrations import SCHEMA_VERSION, connect, migrate, schema_version
from verbatim_sync.db.models import Event, FileRecord, RemoteStatus, RunStatus, SyncRun, SyncState
from verbatim_sync.db.repository import Repository

__all__ = [
    "Event",
    "FileRecord",
    "RemoteStatus",
    "Repository",
    "RunStatus",
    "SCHEMA_VERSION",
    "SyncRun",
    "SyncState",
    "connect",
    "migrate",
    "schema_version",
]
