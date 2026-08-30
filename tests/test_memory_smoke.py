"""Phase O5 desk check. Pure Python — no hardware, no API key.

    python -m tests.test_memory_smoke

Monkeypatches src.memory's module-level MEMORY_PATH/BACKUP_DIR to a temp
dir so nothing under documentation/ is touched. Exits non-zero on failure.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from src import memory

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        _failures.append(label)


def expect_raises(exc_type, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_type as exc:
        return exc
    except Exception as exc:
        return exc  # wrong type, let caller's check() report failure via type name
    return None


def test_ensure_and_remember() -> None:
    print("\n== ensure_memory_file + remember ==")
    memory.ensure_memory_file()
    check("skeleton created", memory.MEMORY_PATH.exists())
    content = memory.load_memory()
    check("skeleton has Lessons section", "## Lessons" in content)
    check("skeleton has World facts section", "## World facts" in content)

    memory.remember("lesson", "when the user says stop, freeze immediately")
    memory.remember("fact", "the couch is on the user's left")
    content = memory.load_memory()

    lessons_idx = content.index("## Lessons")
    facts_idx = content.index("## World facts")
    lesson_entry_idx = content.index("- when the user says stop, freeze immediately")
    fact_entry_idx = content.index("- the couch is on the user's left")
    check(
        "lesson entry lands under Lessons section",
        lessons_idx < lesson_entry_idx < facts_idx,
    )
    check(
        "fact entry lands under World facts section",
        fact_entry_idx > facts_idx,
    )


def test_validation() -> None:
    print("\n== entry validation ==")
    exc = expect_raises(ValueError, memory.remember, "lesson", "x" * 201)
    check("entry too long rejected", isinstance(exc, ValueError), str(exc))

    exc = expect_raises(ValueError, memory.remember, "lesson", "line one\nline two")
    check("newline rejected", isinstance(exc, ValueError), str(exc))

    exc = expect_raises(ValueError, memory.remember, "bogus", "some text")
    check("unknown kind rejected", isinstance(exc, ValueError), str(exc))

    exc = expect_raises(ValueError, memory.remember, "lesson", "   ")
    check("empty entry rejected", isinstance(exc, ValueError), str(exc))


def test_cap_enforcement() -> None:
    print("\n== file size cap ==")
    memory.MEMORY_PATH.write_text(
        "# Goob memory\n\n## Lessons\n\n## World facts\n", encoding="utf-8"
    )
    filler = "y" * 190

    # Measure the exact per-entry cost, then fill until one more entry
    # would just barely cross the cap — deterministic instead of guessing
    # a margin.
    before_len = len(memory.load_memory())
    memory.remember("fact", filler)
    entry_cost = len(memory.load_memory()) - before_len
    check("entry cost measured", entry_cost > 0, f"{entry_cost} chars/entry")

    while len(memory.load_memory()) + entry_cost <= memory._MAX_FILE_CHARS:
        memory.remember("fact", filler)

    before = memory.load_memory()
    exc = expect_raises(memory.MemoryFullError, memory.remember, "fact", filler)
    check(
        "cap enforcement raises MemoryFullError",
        isinstance(exc, memory.MemoryFullError),
        str(exc),
    )
    check(
        "rejected write left file unchanged",
        memory.load_memory() == before,
    )


def test_forget() -> None:
    print("\n== forget ==")
    memory.MEMORY_PATH.write_text(
        "# Goob memory\n\n## Lessons\n\n- alpha lesson\n- beta lesson\n\n"
        "## World facts\n\n- gamma fact\n",
        encoding="utf-8",
    )
    result = memory.forget("beta")
    check("forget confirms removed entry", "beta lesson" in result, result)
    content = memory.load_memory()
    check("matched entry removed", "beta lesson" not in content)
    check("other entries survive", "alpha lesson" in content and "gamma fact" in content)

    exc = expect_raises(memory.MemoryEntryNotFoundError, memory.forget, "no such thing")
    check(
        "forget with no match raises",
        isinstance(exc, memory.MemoryEntryNotFoundError),
        str(exc),
    )


def test_apply_doc_edit(docs_dir: Path) -> None:
    print("\n== apply_doc_edit ==")
    goob = docs_dir / "GOOB.md"
    goob.write_text("You are Goob. Be friendly. Be friendly.\n", encoding="utf-8")

    exc = expect_raises(memory.DocEditError, memory.check_doc_edit, "GOOB.md", "nonexistent text")
    check("zero occurrences rejected", isinstance(exc, memory.DocEditError), str(exc))

    exc = expect_raises(memory.DocEditError, memory.check_doc_edit, "GOOB.md", "Be friendly.")
    check("multiple occurrences rejected", isinstance(exc, memory.DocEditError), str(exc))

    exc = expect_raises(memory.DocEditError, memory.apply_doc_edit, "OTHER.md", "x", "y")
    check("non-editable file rejected", isinstance(exc, memory.DocEditError), str(exc))

    result = memory.apply_doc_edit("GOOB.md", "You are Goob.", "You are Goob v2.")
    check("single occurrence applies", "GOOB.md" in result, result)
    check(
        "file content updated",
        goob.read_text(encoding="utf-8").startswith("You are Goob v2."),
    )


def test_backups() -> None:
    print("\n== backups ==")
    memory.MEMORY_PATH.write_text(
        "# Goob memory\n\n## Lessons\n\n## World facts\n", encoding="utf-8"
    )
    for i in range(25):
        memory.remember("lesson", f"entry number {i}")

    backups = sorted(memory.BACKUP_DIR.glob("LESSONS.md.*.bak"))
    check("backups were created", len(backups) > 0, f"{len(backups)} found")
    check(
        "backups pruned to newest 20",
        len(backups) == 20,
        f"{len(backups)} found",
    )


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="goob_memory_test_"))
    docs_dir = tmp / "documentation"
    docs_dir.mkdir()
    memory.MEMORY_PATH = docs_dir / "LESSONS.md"
    memory.BACKUP_DIR = docs_dir / ".backups"
    memory._DOCS = docs_dir

    try:
        test_ensure_and_remember()
        test_validation()
        test_cap_enforcement()
        test_forget()
        test_apply_doc_edit(docs_dir)
        test_backups()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if _failures:
        print(f"FAILED: {len(_failures)} check(s): {_failures}")
        return 1
    print("all memory checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
