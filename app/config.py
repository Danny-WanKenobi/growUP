"""Environment-based configuration for Grow Up.

Reads PostgreSQL connectivity from environment variables (optionally
provided via a local ``.env`` file during development)::

    DATABASE_HOST
    DATABASE_PORT
    DATABASE_NAME
    DATABASE_USER
    DATABASE_PASSWORD

Secrets are never logged or included in API responses. Use
``.env.example`` as a placeholder template; the real ``.env`` file
must never be committed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "growup"

DEFAULT_DATABASE_HOST = "localhost"
DEFAULT_DATABASE_PORT = 5432
DATABASE_CONNECT_TIMEOUT_SECONDS = 3


@dataclass(frozen=True)
class DatabaseConfig:
    """PostgreSQL connection settings (password kept out of repr/logs)."""

    host: str
    port: int
    name: str
    user: str
    password: str

    @property
    def is_configured(self) -> bool:
        """True only when every required value is present and plausible."""
        return (
            bool(self.host.strip())
            and 1 <= self.port <= 65535
            and bool(self.name.strip())
            and bool(self.user)
            and bool(self.password)
        )

    def connection_kwargs(self) -> dict:
        """Keyword arguments for ``psycopg.connect`` (short timeout)."""
        return {
            "host": self.host,
            "port": self.port,
            "dbname": self.name,
            "user": self.user,
            "password": self.password,
            "connect_timeout": DATABASE_CONNECT_TIMEOUT_SECONDS,
        }

    def __repr__(self) -> str:  # pragma: no cover - defensive redaction
        return (
            f"DatabaseConfig(host={self.host!r}, port={self.port!r}, "
            f"name={self.name!r}, user={self.user!r}, password='***')"
        )


def _maybe_load_dotenv() -> None:
    """Load ``.env`` key=value pairs into the environment (no overrides).

    Minimal stdlib-only loader so local development can use a ``.env``
    file without an extra dependency. Existing environment variables
    always win. Missing or unreadable files are silently ignored.
    """
    candidates = (
        Path.cwd() / ".env",
        Path(__file__).resolve().parent.parent / ".env",
    )
    seen: set[Path] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export ") :].strip()
            if not key or not key.replace("_", "").isalnum():
                continue
            value = value.strip().strip("'\"")
            if key not in os.environ:
                os.environ[key] = value


def _parse_port(raw: str) -> int:
    try:
        return int(raw.strip())
    except (TypeError, ValueError, AttributeError):
        return 0


def get_database_config() -> DatabaseConfig:
    """Build database config from the current environment."""
    host = os.getenv("DATABASE_HOST", DEFAULT_DATABASE_HOST).strip()
    port = _parse_port(os.getenv("DATABASE_PORT", str(DEFAULT_DATABASE_PORT)))
    name = os.getenv("DATABASE_NAME", "").strip()
    user = os.getenv("DATABASE_USER", "")
    password = os.getenv("DATABASE_PASSWORD", "")
    return DatabaseConfig(host=host, port=port, name=name, user=user, password=password)


_maybe_load_dotenv()
