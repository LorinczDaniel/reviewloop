"""Collects what the reviewers look at: the diff, full changed files, and check output."""

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

MAX_FILE_BYTES = 200_000
MAX_CHECK_OUTPUT = 8_000
MAX_RULES_BYTES = 20_000
RULE_FILES = [".reviewloop/rules.md", "CLAUDE.md", "CONTRIBUTING.md"]  # most specific first


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout


def repo_root(path: Path) -> Path:
    return Path(git(path, "rev-parse", "--show-toplevel").strip())


def changed_files(repo: Path, base: str) -> list[str]:
    tracked = git(repo, "diff", "--name-only", base).splitlines()
    untracked = git(repo, "ls-files", "--others", "--exclude-standard").splitlines()
    return sorted({f for f in tracked + untracked if (repo / f).is_file()})


def full_diff(repo: Path, base: str, files: list[str]) -> str:
    diff = git(repo, "diff", base, "--", *files) if files else ""
    # `git diff` ignores untracked files; show them as whole-file additions.
    untracked = set(git(repo, "ls-files", "--others", "--exclude-standard").splitlines())
    for f in files:
        if f in untracked:
            result = subprocess.run(
                ["git", "diff", "--no-index", "/dev/null", f],
                cwd=repo, capture_output=True, text=True,
            )
            diff += result.stdout
    return diff


def load_rules(repo: Path, base: str, changed: list[str]) -> tuple[dict[str, str], list[str]]:
    """Read the project's rule files, returning their text and notes on anything left out.

    Tracked files come from the base ref and files added by the change aren't
    loaded, so a change can't rewrite the rules it's reviewed against.
    """
    rules, notes = {}, []
    for rel in RULE_FILES:
        entry = git(repo, "ls-tree", base, "--", rel).split()
        path = repo / rel
        if entry and entry[0] not in ("100644", "100755") or not entry and path.is_symlink():
            notes.append(f"{rel} isn't a regular file, so it wasn't loaded")
            continue
        if entry:
            data = subprocess.run(["git", "show", f"{base}:{rel}"], cwd=repo, capture_output=True, check=True).stdout
            if rel in changed:
                notes.append(f"{rel} has changes under review, so the version from {base} was loaded")
        elif rel in changed:
            notes.append(f"{rel} is part of the change under review, so it wasn't loaded")
            continue
        elif path.is_file():
            data = path.read_bytes()
        else:
            continue
        if len(data) > MAX_RULES_BYTES:
            notes.append(f"{rel} is over {MAX_RULES_BYTES:,} bytes, so only the start was loaded")
        text = data[:MAX_RULES_BYTES].decode("utf-8", errors="replace").strip()
        if text:
            rules[rel] = text
    return rules, notes


@dataclass
class CheckResult:
    command: str
    returncode: int
    output: str

    @property
    def passed(self) -> bool:
        return self.returncode == 0


def run_checks(repo: Path, commands: list[str]) -> list[CheckResult]:
    results = []
    for cmd in commands:
        proc = subprocess.run(
            cmd, shell=True, cwd=repo, capture_output=True, text=True
        )
        output = (proc.stdout + proc.stderr).strip()
        if len(output) > MAX_CHECK_OUTPUT:
            output = "[...earlier output omitted...]\n" + output[-MAX_CHECK_OUTPUT:]
        results.append(CheckResult(cmd, proc.returncode, output))
    return results


@dataclass
class ReviewContext:
    diff: str
    files: dict[str, str]
    skipped: list[str]
    checks: list[CheckResult]
    rules: dict[str, str] = field(default_factory=dict)

    def render(self) -> str:
        parts = []
        if self.rules:
            parts.append("<project_rules>")
            for path, text in self.rules.items():
                parts += [f'<rules path="{path}">', text, "</rules>"]
            parts.append("</project_rules>")
        parts += ["<diff>", self.diff or "(empty diff)", "</diff>"]
        for path, text in self.files.items():
            parts += [f'<file path="{path}">', text, "</file>"]
        if self.skipped:
            parts.append(
                "Not included (too large or binary): " + ", ".join(self.skipped)
            )
        for c in self.checks:
            status = "PASSED" if c.passed else f"FAILED (exit {c.returncode})"
            parts += [f'<check command="{c.command}" status="{status}">', c.output, "</check>"]
        return "\n".join(parts)


def gather(repo: Path, base: str, files: list[str], checks: list[CheckResult],
           rules: dict[str, str] | None = None) -> ReviewContext:
    contents, skipped = {}, []
    for f in files:
        path = repo / f
        if not path.is_file():
            continue  # deleted by a fix
        data = path.read_bytes()
        if len(data) > MAX_FILE_BYTES or b"\0" in data[:8000]:
            skipped.append(f)
            continue
        contents[f] = data.decode("utf-8", errors="replace")
    return ReviewContext(full_diff(repo, base, files), contents, skipped, checks, rules or {})
