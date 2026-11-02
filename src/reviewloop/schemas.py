"""JSON schemas for structured outputs, plus the in-memory finding model."""

from dataclasses import dataclass, field

SEVERITIES = ["blocker", "major", "minor", "nit"]


def _obj(properties: dict) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


_STR = {"type": "string"}

REVIEW_SCHEMA = _obj({
    "summary": _STR,
    "previous": {
        "type": "array",
        "items": _obj({
            "id": _STR,
            "status": {"type": "string", "enum": ["resolved", "open"]},
            "note": _STR,
        }),
    },
    "findings": {
        "type": "array",
        "items": _obj({
            "severity": {"type": "string", "enum": SEVERITIES},
            "file": _STR,
            "line": {"type": "integer"},
            "title": _STR,
            "detail": _STR,
            "suggestion": _STR,
        }),
    },
})

OVERSEER_SCHEMA = _obj({
    "summary": _STR,
    "decisions": {
        "type": "array",
        "items": _obj({
            "id": _STR,
            "action": {"type": "string", "enum": ["fix", "wont_fix", "defer"]},
            "reason": _STR,
            "guidance": _STR,
        }),
    },
})

FIXER_SCHEMA = _obj({
    "notes": _STR,
    "edits": {
        "type": "array",
        "items": _obj({
            "file": _STR,
            "old_str": _STR,
            "new_str": _STR,
            "finding_ids": {"type": "array", "items": _STR},
        }),
    },
})


@dataclass
class Finding:
    id: str
    reviewer: str
    severity: str
    file: str
    line: int
    title: str
    detail: str
    suggestion: str
    round_found: int
    status: str = "open"  # open | resolved | wont_fix | deferred | skipped | false_positive | accepted | suppressed
    history: list[str] = field(default_factory=list)

    def is_blocking(self, blocking: set[str]) -> bool:
        return self.status == "open" and self.severity in blocking

    def brief(self) -> str:
        loc = f"{self.file}:{self.line}" if self.line else self.file
        return f"[{self.id}] ({self.severity}) {loc} — {self.title}"

    def full(self) -> str:
        return f"{self.brief()}\n{self.detail}\nSuggested fix: {self.suggestion}"
