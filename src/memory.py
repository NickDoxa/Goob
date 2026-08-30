"""Persistent memory for Goob: lessons, world facts, and self-editable docs.

LESSONS.md is bot-writable and loaded into the system prompt (src/llm.py)
after GOOB.md and MOVEMENT.md. GOOB.md and MOVEMENT.md are also editable,
but only via a propose/approve round trip (src/bot.py owns the pending
proposal state; this module only validates and applies).

Every write is backed up first (timestamped copy in BACKUP_DIR, pruned to
the newest 20) and done atomic-ish (write to a temp file, then replace).
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

_DOCS = Path(__file__).resolve().parent.parent / "documentation"

MEMORY_PATH = _DOCS / "LESSONS.md"
BACKUP_DIR = _DOCS / ".backups"

_SKELETON = "# Goob memory\n\n## Lessons\n\n## World facts\n"

_MAX_ENTRY_CHARS = 200
_MAX_FILE_CHARS = 6000
_MAX_BACKUPS = 20

_SECTION_HEADERS = {
    "lesson": "## Lessons",
    "fact": "## World facts",
}

_EDITABLE_DOCS = {"GOOB.md", "MOVEMENT.md"}


class MemoryFullError(Exception):
    pass


class MemoryEntryNotFoundError(Exception):
    pass


class DocEditError(Exception):
    pass


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _backup(path: Path) -> None:
    if not path.exists():
        return
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"{path.name}.{stamp}.bak"
    suffix = 1
    while dest.exists():
        dest = BACKUP_DIR / f"{path.name}.{stamp}-{suffix}.bak"
        suffix += 1
    shutil.copy2(path, dest)
    _prune_backups(path.name)


def _prune_backups(name: str) -> None:
    backups = sorted(
        BACKUP_DIR.glob(f"{name}.*.bak"), key=lambda p: p.stat().st_mtime
    )
    excess = len(backups) - _MAX_BACKUPS
    if excess <= 0:
        return
    for old in backups[:excess]:
        old.unlink(missing_ok=True)


def load_memory() -> str:
    if not MEMORY_PATH.exists():
        return ""
    return MEMORY_PATH.read_text(encoding="utf-8")


def ensure_memory_file() -> None:
    # Also rewrite an empty file: forget() of the last entry leaves "",
    # which would otherwise never regain its section headers.
    if MEMORY_PATH.exists() and load_memory().strip():
        return
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(MEMORY_PATH, _SKELETON)


def _validate_entry_text(text: str) -> str:
    text = text.strip()
    if not text:
        raise ValueError("entry text is empty")
    # splitlines(), not a \n/\r check: it also catches unicode separators
    # (U+2028, U+2029, U+0085, ...) that would materialize as real
    # newlines on the next splitlines/join round trip and let an entry
    # forge a section header.
    if len(text.splitlines()) > 1:
        raise ValueError("entry text must be a single line")
    if len(text) > _MAX_ENTRY_CHARS:
        raise ValueError(
            f"entry text too long ({len(text)} > {_MAX_ENTRY_CHARS} chars)"
        )
    return text


def remember(kind: str, text: str) -> str:
    if kind not in _SECTION_HEADERS:
        raise ValueError(f"unknown kind {kind!r}, expected one of {sorted(_SECTION_HEADERS)}")
    text = _validate_entry_text(text)

    ensure_memory_file()
    content = load_memory()
    header = _SECTION_HEADERS[kind]
    entry = f"- {text}"

    lines = content.splitlines()
    if header not in lines:
        # Section missing from an otherwise-existing file: append it fresh.
        if content and not content.endswith("\n"):
            content += "\n"
        content += f"\n{header}\n\n{entry}\n"
    else:
        idx = lines.index(header)
        insert_at = idx + 1
        while insert_at < len(lines) and not lines[insert_at].startswith("## "):
            insert_at += 1
        lines.insert(insert_at, entry)
        content = "\n".join(lines) + "\n"

    if len(content) > _MAX_FILE_CHARS:
        raise MemoryFullError(
            "memory full — use forget() to prune stale entries first"
        )

    _backup(MEMORY_PATH)
    _atomic_write(MEMORY_PATH, content)
    return f"remembered ({kind}): {text}"


def forget(match: str) -> str:
    match = match.strip()
    if not match:
        raise ValueError("match text is empty")

    content = load_memory()
    lines = content.splitlines()
    needle = match.lower()
    for i, line in enumerate(lines):
        if line.startswith("- ") and needle in line.lower():
            removed = line
            del lines[i]
            new_content = "\n".join(lines) + ("\n" if lines else "")
            _backup(MEMORY_PATH)
            _atomic_write(MEMORY_PATH, new_content)
            return f"forgot: {removed[2:]}"

    raise MemoryEntryNotFoundError(f"no memory entry matching {match!r}")


def check_doc_edit(filename: str, find: str) -> None:
    if filename not in _EDITABLE_DOCS:
        raise DocEditError(
            f"cannot edit {filename!r}, only {sorted(_EDITABLE_DOCS)} are editable"
        )
    path = _DOCS / filename
    if not path.exists():
        raise DocEditError(f"{filename} does not exist")
    content = path.read_text(encoding="utf-8")
    count = content.count(find)
    if count != 1:
        raise DocEditError(
            f"find text must occur exactly once in {filename}, found {count}"
        )


def apply_doc_edit(filename: str, find: str, replace: str) -> str:
    check_doc_edit(filename, find)
    path = _DOCS / filename
    content = path.read_text(encoding="utf-8")
    new_content = content.replace(find, replace, 1)
    _backup(path)
    _atomic_write(path, new_content)
    return f"applied edit to {filename}: {find!r} -> {replace!r}"
