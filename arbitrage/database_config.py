"""Explicit paired database overrides; legacy project-root defaults are unchanged."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BUSINESS_DATABASE = PROJECT_ROOT / "arbitrage.db"
DEFAULT_SESSION_DATABASE = PROJECT_ROOT / "sessions.db"
BUSINESS_DATABASE_ENV = "ARBITRAGE_DATABASE_PATH"
SESSION_DATABASE_ENV = "ARBITRAGE_SESSIONS_DATABASE_PATH"
SQLITE_SUFFIXES = ("", "-wal", "-shm", "-journal")


@dataclass(frozen=True)
class DatabasePaths:
    business: Path
    sessions: Path


def _file_path(value: str | Path) -> Path:
    text = str(value)
    if not text.strip() or text != text.strip() or "\x00" in text:
        raise ValueError("Database paths must be nonempty filesystem paths without surrounding whitespace.")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ValueError("Explicit database paths must be absolute, not SQLite URIs or relative paths.")
    try:
        path = path.resolve()
        if path.exists() and not path.is_file():
            raise ValueError("Database path must identify a regular file.")
        if any(parent.exists() and not parent.is_dir() for parent in path.parents):
            raise ValueError("Database parent must be a directory.")
    except (OSError, RuntimeError) as error:
        raise ValueError("Database path cannot be safely resolved.") from error
    return path


def _aliases(left: Path, right: Path) -> bool:
    return left.resolve() == right.resolve() or (
        left.exists() and right.exists() and left.samefile(right)
    )


def isolated_database_paths(business: str | Path, sessions: str | Path) -> DatabasePaths:
    """Validate before opening or creating anything; protect either DB in either role."""
    paths = DatabasePaths(_file_path(business), _file_path(sessions))
    protected = [Path(str(path) + suffix)
                 for path in (DEFAULT_BUSINESS_DATABASE, DEFAULT_SESSION_DATABASE)
                 for suffix in SQLITE_SUFFIXES]
    families = [[Path(str(path) + suffix) for suffix in SQLITE_SUFFIXES]
                for path in (paths.business, paths.sessions)]
    try:
        if any(_aliases(path, old) for family in families for path in family for old in protected):
            raise ValueError("Development database paths must not alias protected production databases or sidecars.")
        if any(_aliases(left, right) for left in families[0] for right in families[1]):
            raise ValueError("Business and session databases and sidecars must be distinct.")
    except OSError as error:
        raise ValueError("Database aliases could not be safely checked.") from error
    return paths


def get_database_paths(environ: Mapping[str, str] | None = None) -> DatabasePaths:
    env = os.environ if environ is None else environ
    if BUSINESS_DATABASE_ENV not in env and SESSION_DATABASE_ENV not in env:
        return DatabasePaths(DEFAULT_BUSINESS_DATABASE, DEFAULT_SESSION_DATABASE)
    if BUSINESS_DATABASE_ENV not in env or SESSION_DATABASE_ENV not in env:
        raise ValueError("Both database path overrides are required; partial configuration is refused.")
    return isolated_database_paths(env[BUSINESS_DATABASE_ENV], env[SESSION_DATABASE_ENV])
