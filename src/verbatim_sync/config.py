"""Load and validate the TOML configuration file that scopes a sync run.

The job is driven entirely by this file: which tree to walk, which corpus to
push into, what to accept, where to keep state and where to log. It is passed
as the sole argument of the cron entry point, so every relative path inside it
is resolved against the *config file's own directory* — never against the
process working directory, which cron does not control.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from verbatim_sync.errors import ConfigError

DEFAULT_BASE_URL = "https://api.verbatim-ai.com"
STAGING_BASE_URL = "https://staging-api.verbatim-ai.com"

VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
VALID_LOG_FORMATS = ("text", "json")

# SI suffixes are powers of 10, IEC suffixes powers of 2. Both are accepted so
# "50MB" and "50MiB" can be written explicitly rather than guessed at.
_SIZE_UNITS = {
    "B": 1,
    "KB": 10**3,
    "MB": 10**6,
    "GB": 10**9,
    "TB": 10**12,
    "KIB": 2**10,
    "MIB": 2**20,
    "GIB": 2**30,
    "TIB": 2**40,
}
_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([A-Za-z]*)\s*$")


def parse_size(value: Any, key: str) -> int:
    """Turn ``50MB``/``50MiB``/``52428800`` into a byte count."""
    if isinstance(value, bool):  # bool is an int subclass; reject it explicitly
        raise ConfigError(f"expected a size, got {value!r}", key)
    if isinstance(value, int):
        size = value
    elif isinstance(value, str):
        match = _SIZE_RE.match(value)
        if not match:
            raise ConfigError(f"cannot parse size {value!r}", key)
        number, unit = match.groups()
        unit = unit.upper() or "B"
        if unit not in _SIZE_UNITS:
            raise ConfigError(
                f"unknown size unit {unit!r}; use one of "
                f"{', '.join(sorted(_SIZE_UNITS))}",
                key,
            )
        size = int(float(number) * _SIZE_UNITS[unit])
    else:
        raise ConfigError(f"expected a size, got {type(value).__name__}", key)

    if size < 0:
        raise ConfigError("size cannot be negative", key)
    return size


@dataclass(frozen=True)
class SourceConfig:
    root_dir: Path
    follow_symlinks: bool = False
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class CorpusConfig:
    id: str
    lang: str = "en"
    provider: str = "file-directory-sync"


@dataclass(frozen=True)
class FiltersConfig:
    content_types: tuple[str, ...] = ()
    max_file_size: int = 50 * 2**20
    min_file_size: int = 1


@dataclass(frozen=True)
class ApiConfig:
    """API endpoint, credentials and transport settings.

    The credential is an RSA private key held in a keystore directory outside
    the project. ``key_filename`` names that file and ``key_id`` is the UUID the
    platform issued for the key, sent as the JWT ``kid`` header. The two are
    independent: ``build_keys.py`` happens to name the file after the UUID, but
    a keystore built by hand is free to call it ``staging`` or ``prod.pem``.
    """

    organization_id: str
    keys_dir: Path
    key_filename: str
    key_id: str
    base_url: str = DEFAULT_BASE_URL
    timeout_ms: int = 5000
    max_retries: int = 5
    token_ttl_minutes: int = 30

    @property
    def private_key_path(self) -> Path:
        return self.keys_dir / self.key_filename

    @property
    def public_key_path(self) -> Path:
        """Where the public half is expected: beside the private key.

        Both ``<name>.pub`` and ``<stem>.pub`` are accepted, so a key called
        ``prod.pem`` finds ``prod.pub`` as readily as ``prod.pem.pub``.
        """
        suffixed = self.keys_dir / f"{self.key_filename}.pub"
        if suffixed.is_file():
            return suffixed
        stemmed = self.keys_dir / f"{Path(self.key_filename).stem}.pub"
        return stemmed if stemmed.is_file() else suffixed

    @property
    def timeout_seconds(self) -> float:
        return self.timeout_ms / 1000.0


@dataclass(frozen=True)
class DatabaseConfig:
    path: Path = Path("state/sync.db")


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    file: Path | None = None
    console: bool = True
    format: str = "text"
    rotate_max_bytes: int = 10 * 2**20
    backup_count: int = 7


#: More than this many concurrent uploads is far more likely to be a typo than
#: an intention, and the platform rate-limits anyway.
MAX_THREADS = 32


@dataclass(frozen=True)
class SyncConfig:
    dry_run: bool = False
    delete_remote_when_missing: bool = True
    poll_status: bool = True
    poll_timeout_seconds: int = 300
    threads: int = 5


@dataclass(frozen=True)
class Config:
    source: SourceConfig
    corpus: CorpusConfig
    api: ApiConfig
    filters: FiltersConfig = field(default_factory=FiltersConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    path: Path | None = None


def _section(data: dict[str, Any], name: str, required: bool = False) -> dict[str, Any]:
    value = data.get(name)
    if value is None:
        if required:
            raise ConfigError("required section is missing", name)
        return {}
    if not isinstance(value, dict):
        raise ConfigError("expected a table", name)
    return value


def _reject_unknown(section: dict[str, Any], known: set[str], name: str) -> None:
    """A typo in a key silently disabling a filter is the worst failure mode
    for a job nobody watches, so unknown keys are an error rather than a warn."""
    unknown = sorted(set(section) - known)
    if unknown:
        raise ConfigError(f"unknown key(s): {', '.join(unknown)}", name)


def _get_str(section: dict[str, Any], key: str, path: str, default: Any = ...) -> str:
    if key not in section:
        if default is ...:
            raise ConfigError("required key is missing", f"{path}.{key}")
        return default
    value = section[key]
    if not isinstance(value, str):
        raise ConfigError(f"expected a string, got {type(value).__name__}", f"{path}.{key}")
    return value


def _get_bool(section: dict[str, Any], key: str, path: str, default: bool) -> bool:
    if key not in section:
        return default
    value = section[key]
    if not isinstance(value, bool):
        raise ConfigError(f"expected true or false, got {value!r}", f"{path}.{key}")
    return value


def _get_int(
    section: dict[str, Any],
    key: str,
    path: str,
    default: int,
    minimum: int | None = None,
) -> int:
    if key not in section:
        return default
    value = section[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"expected an integer, got {value!r}", f"{path}.{key}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"must be >= {minimum}", f"{path}.{key}")
    return value


def _get_str_list(
    section: dict[str, Any], key: str, path: str, default: tuple[str, ...] = ()
) -> tuple[str, ...]:
    if key not in section:
        return default
    value = section[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError("expected a list of strings", f"{path}.{key}")
    return tuple(value)


def load_config(path: str | Path) -> Config:
    """Read, validate and normalise the configuration file at ``path``."""
    config_path = Path(path).expanduser().resolve()
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        raise ConfigError(f"configuration file not found: {config_path}") from None
    except IsADirectoryError:
        raise ConfigError(f"configuration path is a directory: {config_path}") from None
    except OSError as exc:
        raise ConfigError(f"cannot read {config_path}: {exc.strerror}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc

    base_dir = config_path.parent

    def resolve(value: str) -> Path:
        candidate = Path(value).expanduser()
        return candidate if candidate.is_absolute() else (base_dir / candidate).resolve()

    _reject_unknown(
        data,
        {"source", "corpus", "filters", "api", "database", "logging", "sync"},
        "<root>",
    )

    source = _load_source(_section(data, "source", required=True), resolve)
    corpus = _load_corpus(_section(data, "corpus", required=True))
    filters = _load_filters(_section(data, "filters"))
    api = _load_api(_section(data, "api", required=True), resolve)
    database = _load_database(_section(data, "database"), resolve)
    logging_cfg = _load_logging(_section(data, "logging"), resolve)
    sync = _load_sync(_section(data, "sync"))

    return Config(
        source=source,
        corpus=corpus,
        filters=filters,
        api=api,
        database=database,
        logging=logging_cfg,
        sync=sync,
        path=config_path,
    )


def _load_source(section: dict[str, Any], resolve: Any) -> SourceConfig:
    _reject_unknown(
        section, {"root_dir", "follow_symlinks", "include", "exclude"}, "source"
    )
    root_dir = resolve(_get_str(section, "root_dir", "source"))
    if not root_dir.exists():
        raise ConfigError(f"directory does not exist: {root_dir}", "source.root_dir")
    if not root_dir.is_dir():
        raise ConfigError(f"not a directory: {root_dir}", "source.root_dir")
    return SourceConfig(
        root_dir=root_dir,
        follow_symlinks=_get_bool(section, "follow_symlinks", "source", False),
        include=_get_str_list(section, "include", "source"),
        exclude=_get_str_list(section, "exclude", "source"),
    )


def _load_corpus(section: dict[str, Any]) -> CorpusConfig:
    _reject_unknown(section, {"id", "lang", "provider"}, "corpus")
    corpus_id = _get_str(section, "id", "corpus")
    try:
        UUID(corpus_id)
    except ValueError:
        raise ConfigError(f"not a valid UUID: {corpus_id!r}", "corpus.id") from None
    return CorpusConfig(
        id=corpus_id,
        lang=_get_str(section, "lang", "corpus", "en"),
        provider=_get_str(section, "provider", "corpus", "file-directory-sync"),
    )


def _load_filters(section: dict[str, Any]) -> FiltersConfig:
    _reject_unknown(
        section, {"content_types", "max_file_size", "min_file_size"}, "filters"
    )
    defaults = FiltersConfig()
    max_size = (
        parse_size(section["max_file_size"], "filters.max_file_size")
        if "max_file_size" in section
        else defaults.max_file_size
    )
    min_size = (
        parse_size(section["min_file_size"], "filters.min_file_size")
        if "min_file_size" in section
        else defaults.min_file_size
    )
    if max_size <= min_size:
        raise ConfigError(
            f"must be greater than min_file_size ({min_size} bytes)",
            "filters.max_file_size",
        )
    content_types = tuple(
        ct.strip().lower() for ct in _get_str_list(section, "content_types", "filters")
    )
    return FiltersConfig(
        content_types=content_types, max_file_size=max_size, min_file_size=min_size
    )


def _load_api(section: dict[str, Any], resolve: Any) -> ApiConfig:
    known = {
        "base_url",
        "organization_id",
        "keys_dir",
        "key_filename",
        "key_id",
        "timeout_ms",
        "max_retries",
        "token_ttl_minutes",
    }
    _reject_unknown(section, known, "api")

    base_url = _get_str(section, "base_url", "api", DEFAULT_BASE_URL).rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError(f"must be an http(s) URL, got {base_url!r}", "api.base_url")

    organization_id = _get_str(section, "organization_id", "api")
    try:
        UUID(organization_id)
    except ValueError:
        raise ConfigError(
            f"not a valid UUID: {organization_id!r}", "api.organization_id"
        ) from None

    keys_dir = resolve(_get_str(section, "keys_dir", "api"))
    if not keys_dir.exists():
        raise ConfigError(f"directory does not exist: {keys_dir}", "api.keys_dir")
    if not keys_dir.is_dir():
        raise ConfigError(f"not a directory: {keys_dir}", "api.keys_dir")

    key_filename = _get_str(section, "key_filename", "api")
    if "/" in key_filename or "\\" in key_filename or key_filename in (".", ".."):
        raise ConfigError(
            "must be a bare filename inside api.keys_dir, not a path",
            "api.key_filename",
        )

    private_key_path = keys_dir / key_filename
    if not private_key_path.is_file():
        raise ConfigError(
            f"private key not found: {private_key_path}", "api.key_filename"
        )

    # The UUID the platform issued for this key, sent as the JWT `kid` header
    # so the server knows which public key to verify with. Independent of the
    # filename — validated here so a bad value fails at load rather than as an
    # opaque 403 on the first call.
    key_id = _get_str(section, "key_id", "api")
    try:
        UUID(key_id)
    except ValueError:
        raise ConfigError(f"not a valid UUID: {key_id!r}", "api.key_id") from None

    defaults = ApiConfig(
        organization_id=organization_id,
        keys_dir=keys_dir,
        key_filename=key_filename,
        key_id=key_id,
    )

    # Tokens are bearer credentials; the docs advise minutes to an hour.
    ttl = _get_int(section, "token_ttl_minutes", "api", defaults.token_ttl_minutes, 1)
    if ttl > 24 * 60:
        raise ConfigError("must be <= 1440 (24 hours)", "api.token_ttl_minutes")

    return ApiConfig(
        organization_id=organization_id,
        keys_dir=keys_dir,
        key_filename=key_filename,
        key_id=key_id,
        base_url=base_url,
        timeout_ms=_get_int(section, "timeout_ms", "api", defaults.timeout_ms, 1),
        max_retries=_get_int(section, "max_retries", "api", defaults.max_retries, 0),
        token_ttl_minutes=ttl,
    )


def _load_database(section: dict[str, Any], resolve: Any) -> DatabaseConfig:
    _reject_unknown(section, {"path"}, "database")
    defaults = DatabaseConfig()
    raw = _get_str(section, "path", "database", str(defaults.path))
    return DatabaseConfig(path=resolve(raw))


def _load_logging(section: dict[str, Any], resolve: Any) -> LoggingConfig:
    known = {
        "level",
        "file",
        "console",
        "format",
        "rotate_max_bytes",
        "backup_count",
    }
    _reject_unknown(section, known, "logging")
    defaults = LoggingConfig()

    level = _get_str(section, "level", "logging", defaults.level).upper()
    if level not in VALID_LOG_LEVELS:
        raise ConfigError(
            f"must be one of {', '.join(VALID_LOG_LEVELS)}", "logging.level"
        )

    log_format = _get_str(section, "format", "logging", defaults.format).lower()
    if log_format not in VALID_LOG_FORMATS:
        raise ConfigError(
            f"must be one of {', '.join(VALID_LOG_FORMATS)}", "logging.format"
        )

    log_file = resolve(section["file"]) if section.get("file") else None
    console = _get_bool(section, "console", "logging", defaults.console)
    if log_file is None and not console:
        raise ConfigError(
            "logging is fully disabled: set logging.file or logging.console",
            "logging",
        )

    rotate = (
        parse_size(section["rotate_max_bytes"], "logging.rotate_max_bytes")
        if "rotate_max_bytes" in section
        else defaults.rotate_max_bytes
    )
    return LoggingConfig(
        level=level,
        file=log_file,
        console=console,
        format=log_format,
        rotate_max_bytes=rotate,
        backup_count=_get_int(
            section, "backup_count", "logging", defaults.backup_count, 0
        ),
    )


def _load_sync(section: dict[str, Any]) -> SyncConfig:
    known = {
        "dry_run",
        "delete_remote_when_missing",
        "poll_status",
        "poll_timeout_seconds",
        "threads",
    }
    _reject_unknown(section, known, "sync")
    defaults = SyncConfig()

    threads = _get_int(section, "threads", "sync", defaults.threads, 1)
    if threads > MAX_THREADS:
        raise ConfigError(f"must be <= {MAX_THREADS}", "sync.threads")

    return SyncConfig(
        dry_run=_get_bool(section, "dry_run", "sync", defaults.dry_run),
        delete_remote_when_missing=_get_bool(
            section,
            "delete_remote_when_missing",
            "sync",
            defaults.delete_remote_when_missing,
        ),
        poll_status=_get_bool(section, "poll_status", "sync", defaults.poll_status),
        poll_timeout_seconds=_get_int(
            section, "poll_timeout_seconds", "sync", defaults.poll_timeout_seconds, 1
        ),
        threads=threads,
    )
