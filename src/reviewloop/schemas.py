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
            "quote": _STR,
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
    status: str = "open"  # open | resolved | wont_fix | deferred | skipped | false_positive | accepted | suppressed | dropped
    history: list[str] = field(default_factory=list)
    quote: str = ""
    verified: bool = True  # False for a blocker whose quote wasn't found; it stays open for a human

    def is_blocking(self, blocking: set[str]) -> bool:
        return self.status == "open" and self.severity in blocking

    def brief(self) -> str:
        loc = f"{self.file}:{self.line}" if self.line else self.file
        severity = self.severity if self.verified else f"{self.severity}, unverified"
        return f"[{self.id}] ({severity}) {loc} — {self.title}"

    def full(self) -> str:
        code = f"Quoted code: {self.quote}\n" if self.quote else ""
        if not self.verified:
            code += "Unverified: this quote was not found in the file, so the problem and line may be wrong.\n"
        return f"{self.brief()}\n{code}{self.detail}\nSuggested fix: {self.suggestion}"
