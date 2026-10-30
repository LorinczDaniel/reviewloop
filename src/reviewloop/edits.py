"""Applies the fixer's old_str/new_str edits to the working tree, safely."""

import difflib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class EditResult:
    file: str
    ok: bool
    message: str


def _resolve(repo: Path, rel: str) -> Path | None:
    path = (repo / rel).resolve()
    if not path.is_relative_to(repo.resolve()) or ".git" in path.relative_to(repo.resolve()).parts:
        return None
    return path


def apply_edits(repo: Path, edits: list[dict]) -> tuple[list[EditResult], str, dict[str, str | None]]:
    """Apply edits in order.

    Returns per-edit results, a unified diff of what changed, and the original
    text of every touched file (None for files that did not exist).
    """
    before: dict[str, str | None] = {}
    results = []

    for edit in edits:
        rel, old, new = edit["file"], edit["old_str"], edit["new_str"]
        path = _resolve(repo, rel)
        if path is None:
            results.append(EditResult(rel, False, "path is outside the repository"))
            continue

        current = path.read_text() if path.is_file() else None
        before.setdefault(rel, current)

        if old == "":
            if current is not None:
                results.append(EditResult(rel, False, "empty old_str but file already exists"))
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new)
            results.append(EditResult(rel, True, "created file"))
            continue

        if current is None:
            results.append(EditResult(rel, False, "file does not exist"))
            continue
        count = current.count(old)
        if count != 1:
            results.append(EditResult(rel, False, f"old_str found {count} times, expected exactly 1"))
            continue
        path.write_text(current.replace(old, new, 1))
        results.append(EditResult(rel, True, "applied"))

    diff = []
    for rel, old_text in before.items():
        path = repo / rel
        new_text = path.read_text() if path.is_file() else ""
        diff += difflib.unified_diff(
            (old_text or "").splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{rel}", tofile=f"b/{rel}",
        )
    return results, "".join(diff), before
