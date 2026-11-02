"""Permanent Baseten credential loading for GUADE (development only).

Root cause this module repairs: the Super-Coach key was provisioned
in persistent per-user stores that no GUADE process ever read.

* Windows: the key lives in the user environment
  (``HKCU\\Environment``). A process started *before* the variable was
  installed (a long-lived shell, editor, or agent host) keeps a stale
  environment block forever, so reading only ``os.environ`` reports
  the key absent until that parent restarts.
* Ubuntu: the key lives in the owner-only file
  ``~/.config/growup/guade.env`` (mode 0600). Interactive shells do
  not load it automatically, and the application service's
  ``EnvironmentFile`` (``/var/www/growup/shared/growup.env``, owned by
  the ``growup`` service account) is a different file that GUADE
  never read either.

Resolution order (first non-empty value wins; the value itself is
never logged, printed, or included in any status output):

1. An explicit constructor argument (tests and embeddings).
2. The process environment (``BASETEN_API_KEY``).
3. Windows only: the persistent user environment in the registry,
   which recovers processes whose parent predates installation.
4. Restricted per-user env files: ``$GUADE_ENV_FILE``, then
   ``~/.config/growup/guade.env``, then the Ubuntu shared app file
   ``/var/www/growup/shared/growup.env`` when readable. Files accept
   ``BASETEN_API_KEY`` and fall back to ``META_API_KEY`` (the same
   Baseten inference credential the application uses; see
   ``.env.example``). On POSIX a file readable by group/other is
   refused: secrets must be owner-only.

Standard library only. No network access. Nothing here creates,
rotates, or replaces a key: when every source is absent the caller
gets an actionable message naming the exact owner step required.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ENV_VAR = "BASETEN_API_KEY"
ALIAS_VAR = "META_API_KEY"
ENV_FILE_VAR = "GUADE_ENV_FILE"


@dataclass(frozen=True)
class ResolvedKey:
    """A resolved credential. The value is excluded from repr."""

    value: str = field(default="", repr=False)
    source: str = "absent"  # process_env | windows_user_env | env_file:<name> | absent


def _from_environ(environ: dict) -> str:
    return str(environ.get(ENV_VAR, "") or "").strip()


def _from_windows_registry() -> str:
    """Read the persistent user environment (Windows only)."""
    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _kind = winreg.QueryValueEx(key, ENV_VAR)
        return str(value or "").strip()
    except (OSError, ImportError):
        return ""


def _candidate_env_files(environ: dict) -> list[Path]:
    candidates: list[Path] = []
    override = str(environ.get(ENV_FILE_VAR, "") or "").strip()
    if override:
        candidates.append(Path(override))
    candidates.append(Path.home() / ".config" / "growup" / "guade.env")
    candidates.append(Path("/var/www/growup/shared/growup.env"))
    return candidates


def _env_file_problem(path: Path) -> str | None:
    """Refuse secret files that group/other can read (POSIX only)."""
    if os.name == "nt":
        return None
    try:
        mode = path.stat().st_mode
    except OSError:
        return "unreadable"
    if mode & 0o077:
        return "permissions too open (expected owner-only, e.g. chmod 600)"
    return None


BASETEN_HOST = "inference.baseten.co"


def _alias_is_baseten_bound(values: dict[str, str], environ: dict) -> bool:
    """META_API_KEY is the Baseten credential only when its endpoint is.

    The application pairs META_API_KEY with the operator-configured
    META_API_URL (``.env.example`` binds it to the Baseten transport).
    GUADE always calls Baseten, so the alias is accepted only when no
    non-Baseten endpoint is configured in the same file or process.
    """
    url = (values.get("META_API_URL", "")
           or str(environ.get("META_API_URL", "") or "")).strip().lower()
    return not url or BASETEN_HOST in url


def _read_env_file(path: Path, environ: dict) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, raw = line.partition("=")
        name = name.strip()
        if name.startswith("export "):
            name = name[len("export "):].strip()
        values[name] = raw.strip().strip("'\"")
    direct = values.get(ENV_VAR, "").strip()
    if direct:
        return direct
    if _alias_is_baseten_bound(values, environ):
        return values.get(ALIAS_VAR, "").strip()
    return ""


def resolve_baseten_api_key(environ: dict | None = None) -> ResolvedKey:
    """Resolve the Baseten key without ever exposing it in output."""
    env = os.environ if environ is None else environ
    value = _from_environ(env)
    if value:
        return ResolvedKey(value, "process_env")
    value = _from_windows_registry()
    if value:
        return ResolvedKey(value, "windows_user_env")
    for path in _candidate_env_files(env):
        if not path.is_file():
            continue
        if _env_file_problem(path) is not None:
            continue
        value = _read_env_file(path, env)
        if value:
            return ResolvedKey(value, f"env_file:{path.name}")
    return ResolvedKey("", "absent")


def credential_status(environ: dict | None = None) -> dict:
    """Presence + provenance only. Never contains credential material."""
    env = os.environ if environ is None else environ
    resolved = resolve_baseten_api_key(env)
    problems: list[str] = []
    for path in _candidate_env_files(env):
        if path.is_file():
            problem = _env_file_problem(path)
            if problem is not None:
                problems.append(f"{path}: {problem}")
    return {
        "present": bool(resolved.value),
        "source": resolved.source,
        "problems": problems,
    }


def missing_key_guidance() -> str:
    """The exact owner action when no source yields the key."""
    return (
        "BASETEN_API_KEY was not found in the process environment, the "
        "Windows user environment, or a restricted GUADE env file. "
        "Owner action (do not paste the key into chat, code, or Git): "
        "Windows — set the user variable BASETEN_API_KEY (Settings > "
        "Environment Variables, or Windows Credential Manager) and "
        "start a fresh GUADE process; Ubuntu — store "
        "BASETEN_API_KEY=<key> in ~/.config/growup/guade.env with "
        "mode 600 (install -m 600). GUADE never creates or rotates keys."
    )
