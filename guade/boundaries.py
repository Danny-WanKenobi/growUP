"""Task-specific workspace change validation and bounded, real diff evidence.

This is a completion gate, not an OS sandbox. Unexpected changes are retained
for the owner and stop execution; they are never silently reverted.
"""
from __future__ import annotations

import difflib
import hashlib
import os
from pathlib import Path, PurePosixPath

from .runtime import GuadeError

MAX_DIFF_BYTES = 48_000
MAX_FILE_BYTES = 2_000_000
_CACHE_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache",
               ".mypy_cache", ".ruff_cache"}


def allowed_paths(workspace: Path, values: list[str]) -> list[str]:
    if not isinstance(values, list) or not values:
        raise GuadeError("task requires a nonempty allowed_files list")
    root = workspace.resolve()
    result = []
    for value in values:
        if not isinstance(value, str) or "\\" in value or ":" in value:
            raise GuadeError("allowed_files must be relative POSIX file paths")
        path = PurePosixPath(value)
        if (path.is_absolute() or ".." in path.parts or not path.parts
                or any(part in _CACHE_DIRS for part in path.parts)
                or path.parts[0].casefold() == "guade"
                or path.name.casefold() in {"agents.md", ".gitignore", "auth.json",
                                            "credentials.json", "secrets.json"}
                or path.name.startswith(".env")
                or path.suffix.lower() in {".key", ".pem"}):
            raise GuadeError("task cannot edit runtime controls, secrets or excluded paths")
        target = root.joinpath(*path.parts)
        if not target.resolve().is_relative_to(root) or target.is_dir():
            raise GuadeError("allowed file escapes workspace or names a directory")
        if target.is_symlink() or any(parent.is_symlink() or parent.is_junction()
                                      for parent in target.parents if parent.is_relative_to(root)):
            raise GuadeError("allowed files cannot use linked paths")
        normalized = path.as_posix()
        if normalized.casefold() in {p.casefold() for p in result}:
            raise GuadeError("duplicate allowed file")
        result.append(normalized)
    return result


class WorkspaceBoundary:
    def __init__(self, workspace: Path, allowed: list[str], state_dir: Path):
        self.root = workspace.resolve()
        self.allowed = allowed_paths(self.root, allowed)
        self.state_dir = state_dir.resolve()
        self.before = self._inventory()
        self.text_before = {p: self._text(p) for p in self.allowed}

    def _text(self, path: str) -> str:
        target = self.root / path
        if target.is_symlink() or not target.resolve().is_relative_to(self.root):
            raise GuadeError("allowed file became a link or escaped workspace")
        if not target.exists():
            return ""
        if target.stat().st_size > MAX_FILE_BYTES:
            raise GuadeError("allowed text file exceeds snapshot memory bound")
        try:
            return target.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GuadeError("allowed files must be UTF-8 text") from exc

    def _inventory(self) -> dict[str, str]:
        inventory = {}
        for directory, dirs, files in os.walk(self.root, followlinks=False):
            base = Path(directory)
            for name in list(dirs):
                child = base / name
                if name in _CACHE_DIRS or child == self.state_dir:
                    dirs.remove(name)
                elif child.is_symlink() or child.is_junction():
                    # Record the link itself without following an external or
                    # cyclic target (pytest creates local '*current' links).
                    inventory[child.relative_to(self.root).as_posix() + "/"] = "link:" + os.readlink(child)
                    dirs.remove(name)
            for name in files:
                target = base / name
                relative = target.relative_to(self.root).as_posix()
                if relative == "guade/mission.lock":
                    continue
                if target.is_symlink():
                    inventory[relative] = "link:" + os.readlink(target)
                    continue
                digest = hashlib.sha256()
                with target.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                inventory[relative] = digest.hexdigest()
        return inventory

    def evidence(self) -> dict:
        after = self._inventory()
        changed = sorted(p for p in self.before.keys() | after.keys()
                         if self.before.get(p) != after.get(p))
        unexpected = [p for p in changed if p not in self.allowed]
        if unexpected:
            # Never send contents of an unexpected file (possibly a secret).
            raise GuadeError("out-of-scope file changes: " + ", ".join(unexpected))
        diff = "".join("".join(difflib.unified_diff(
            self.text_before[p].splitlines(keepends=True),
            self._text(p).splitlines(keepends=True),
            fromfile=f"before/{p}", tofile=f"after/{p}")) for p in changed)
        if len(diff.encode("utf-8")) > MAX_DIFF_BYTES:
            raise GuadeError("complete diff exceeds coach evidence bound; owner review required")
        return {"changed_files": changed, "diff": diff,
                "before": {p: self.before.get(p) for p in changed},
                "after": {p: after.get(p) for p in changed}}
