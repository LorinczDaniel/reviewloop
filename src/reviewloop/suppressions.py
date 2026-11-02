"""Remembers dismissed findings on disk, so later runs don't raise them again."""

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

FILENAME = "suppressions.json"
VERSION = 1


@dataclass(frozen=True)
class Suppression:
    reviewer: str
    severity: str
    file: str
    title: str
    detail: str
    code: str  # the flagged line when the decision was made; "" if there wasn't one
    decided_by: str  # overseer | human
    reason: str
    decided_at: str

    def covers(self, severity: str) -> bool:
        """Whether this entry can dismiss a finding of this severity: only a human can dismiss a blocker."""
        return self.decided_by == "human" or "blocker" not in (self.severity, severity)

    def matches(self, reviewer: str, file: str, title: str, code: str) -> bool:
        """True for the same finding on the same line of code.

        A finding with no flagged line never matches: without code to compare,
        a new problem with a similar title could be hidden.
        """
        return (
            bool(code)
            and code == self.code
            and reviewer == self.reviewer
            and file == self.file
            and _normalize(title) == _normalize(self.title)
        )

    def prompt_line(self) -> str:
        code = f"\nCode: {self.code}" if self.code else ""
        return f"{self.file} — {self.title}\n{self.detail}{code}\nDismissed by {self.decided_by}: {self.reason}"


def _normalize(title: str) -> str:
    return " ".join(title.lower().split())


def code_at(files: dict[str, str], file: str, line: int) -> str:
    """The flagged line from the files under review, or "" if it can't be found."""
    lines = files.get(file, "").splitlines()
    return lines[line - 1].strip() if 0 < line <= len(lines) else ""


def load(path: Path) -> list[Suppression]:
    """Read the file, raising ValueError if it's malformed so it never gets overwritten."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return [Suppression(**item) for item in data["suppressions"]]
    except (ValueError, KeyError, TypeError) as e:
        raise ValueError(f"can't read {path}: {e!r}. Fix or delete the file.") from e


def save(path: Path, suppressions: list[Suppression]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"version": VERSION, "suppressions": [asdict(s) for s in suppressions]}, indent=2) + "\n")
    os.replace(tmp, path)
