"""Exception hierarchy shared by every layer of the sync job."""

from __future__ import annotations


class VerbatimSyncError(Exception):
    """Base class for every error raised by this package."""


class ConfigError(VerbatimSyncError):
    """The configuration file is missing, malformed or invalid.

    Carries the offending key so the CLI can point at it without dumping the
    whole file (which may sit next to credentials).
    """

    def __init__(self, message: str, key: str | None = None) -> None:
        self.key = key
        super().__init__(f"{key}: {message}" if key else message)


class DbError(VerbatimSyncError):
    """The local state database could not be opened, migrated or written."""


class AuthError(VerbatimSyncError):
    """The key file could not be read or a token could not be signed."""


class ApiError(VerbatimSyncError):
    """The Verbatim API returned an error response."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        api_message: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.api_message = api_message
        super().__init__(message)


class RetryableApiError(ApiError):
    """A transient API failure (429, 5xx, connection reset) worth retrying."""
