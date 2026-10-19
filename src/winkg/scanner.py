from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)

DEFAULT_EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", ".idea", ".vscode", "__pycache__", ".pytest_cache",
    "node_modules", ".venv", "venv", "env", "site-packages", "dist", "build",
    "$recycle.bin", "system volume information", "windows", "program files",
    "program files (x86)", "temp", "tmp",
}

SENSITIVE_NAME_PATTERN = re.compile(
    r"(^|[._-])(credentials?|secrets?|passwords?|passwd|tokens?|private[-_]?key|id_rsa|id_ed25519)([._-]|$)",
    flags=re.IGNORECASE,
)
SENSITIVE_EXTENSIONS = {".pem", ".pfx", ".p12", ".key", ".kdbx"}
SPECIAL_TEXT_NAMES = {
    "dockerfile": ".dockerfile", "containerfile": ".dockerfile",
    "makefile": ".makefile", "gnumakefile": ".makefile", "justfile": ".makefile",
    "jenkinsfile": ".jenkinsfile", "procfile": ".procfile",
    "readme": ".md", "changelog": ".md", "changes": ".md",
    "license": ".txt", "licence": ".txt", "copying": ".txt", "notice": ".txt",
    "authors": ".txt", "contributors": ".txt", "history": ".txt", "todo": ".txt", "news": ".txt",
    "gemfile": ".rb", "rakefile": ".rb", "vagrantfile": ".hcl",
    ".gitignore": ".gitignore", ".dockerignore": ".dockerignore",
    ".gitattributes": ".gitattributes", ".gitmodules": ".gitmodules",
    ".editorconfig": ".editorconfig", ".npmrc": ".npmrc", ".nvmrc": ".nvmrc",
    ".prettierrc": ".prettierrc", ".python-version": ".python-version",
    ".eslintrc": ".eslintrc", ".flake8": ".flake8", ".coveragerc": ".coveragerc",
}


def extension_for_path(path: Path) -> str:
    return SPECIAL_TEXT_NAMES.get(path.name.casefold(), path.suffix.casefold())


@dataclass(frozen=True)
class FileCandidate:
    path: Path
    relative_path: str
    parent_relative_path: str
    name: str
    extension: str
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class ScanSnapshot:
    root: Path
    folders: tuple[str, ...]
    files: tuple[FileCandidate, ...]
    skipped_sensitive: int = 0
    skipped_errors: int = 0


def _is_sensitive(path: Path) -> bool:
    name = path.name.casefold()
    stem = path.stem.casefold()
    if path.suffix.casefold() in SENSITIVE_EXTENSIONS:
        return True
    if re.match(r"^\.env($|[._-])", name):
        return True
    return bool(SENSITIVE_NAME_PATTERN.search(name) or SENSITIVE_NAME_PATTERN.search(stem))


def scan_directory(root: Path, settings: Settings, include_ignored: bool = False,
                   include_sensitive: bool = False) -> ScanSnapshot:
    root = root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError(f"Kein Verzeichnis: {root}")

    excluded = set(DEFAULT_EXCLUDED_DIRS) | set(settings.extra_excluded_dirs)
    folders: set[str] = {""}
    candidates: list[FileCandidate] = []
    skipped_sensitive = 0
    skipped_errors = 0
    stack: list[Path] = [root]

    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        # Never follow symlinks/junctions by default; this avoids loops
                        # and accidental crawling into other drives or network shares.
                        if entry.is_symlink():
                            continue
                        item_path = Path(entry.path)
                        if entry.is_dir(follow_symlinks=False):
                            if not include_ignored and entry.name.casefold() in excluded:
                                continue
                            relative = item_path.relative_to(root).as_posix()
                            folders.add(relative)
                            stack.append(item_path)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        if not include_sensitive and not settings.include_sensitive_files and _is_sensitive(item_path):
                            skipped_sensitive += 1
                            continue
                        stat = entry.stat(follow_symlinks=False)
                        relative = item_path.relative_to(root).as_posix()
                        parent = item_path.parent.relative_to(root).as_posix()
                        if parent == ".":
                            parent = ""
                        candidates.append(FileCandidate(
                            path=item_path,
                            relative_path=relative,
                            parent_relative_path=parent,
                            name=item_path.name,
                            extension=extension_for_path(item_path),
                            size=stat.st_size,
                            mtime_ns=stat.st_mtime_ns,
                        ))
                    except (PermissionError, OSError) as exc:
                        skipped_errors += 1
                        log.debug("Übersprungen (%s): %s", entry.path, exc)
        except (PermissionError, OSError) as exc:
            skipped_errors += 1
            log.warning("Verzeichnis kann nicht gelesen werden: %s (%s)", current, exc)

    return ScanSnapshot(
        root=root,
        folders=tuple(sorted(folders, key=lambda value: (value.count("/"), value.casefold()))),
        files=tuple(sorted(candidates, key=lambda item: item.relative_path.casefold())),
        skipped_sensitive=skipped_sensitive,
        skipped_errors=skipped_errors,
    )
