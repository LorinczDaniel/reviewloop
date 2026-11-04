"""Collects what the reviewers look at: the diff, full changed files, and check output."""

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .edits import resolve_in_repo

MAX_FILE_BYTES = 200_000
MAX_CHECK_OUTPUT = 8_000
MAX_RULES_BYTES = 20_000
UNSCOPABLE_COMMAND = re.compile(r"[*()&|;<>$`\n]")  # wildcards, and syntax that makes one line several commands
MAX_DENY_RULES = 1000
MAX_RELATED_TEST_BYTES = 100_000
TEST_PATH = re.compile(r"(^|/)(tests?|specs?|__tests__)/|(^|/)test_[^/]+$|_test\.[^/]+$|\.(test|spec)\.[^/]+$")
GENERIC_STEMS = {"__init__", "__main__", "index", "main", "mod"}  # these names say nothing about what a test covers
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


def scopable_checks(commands: list[str]) -> list[str]:
    """Commands that can be allowed exactly in a Claude Code Bash permission rule."""
    return [c for c in commands if not UNSCOPABLE_COMMAND.search(c)]


def ignored_paths(repo: Path) -> list[str]:
    """Gitignored files, and directories that are wholly ignored (with a trailing slash)."""
    return git(repo, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory").splitlines()


def snapshot(repo: Path, base: str) -> dict[str, bytes]:
    """Current contents of every file that differs from the base."""
    return {rel: (repo / rel).read_bytes() for rel in changed_files(repo, base)}


def base_version(repo: Path, base: str, rel: str) -> bytes | None:
    result = subprocess.run(["git", "show", f"{base}:{rel}"], cwd=repo, capture_output=True)
    return result.stdout if result.returncode == 0 else None


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


def related_tests(repo: Path, changed: list[str]) -> str:
    """Render tracked test files that aren't changed but mention a changed file's name.

    Reviewers only see changed files, so without these a test in an untouched file
    looks missing. Files named after the changed code come first when over the limit.
    """
    stems = {Path(f).stem for f in changed if not TEST_PATH.search(f)} - GENERIC_STEMS
    if not stems:
        return ""
    name = re.compile(r"\b(" + "|".join(sorted(map(re.escape, stems))) + r")\b")
    found = []
    for rel in git(repo, "ls-files").splitlines():
        path = repo / rel
        if rel in changed or not TEST_PATH.search(rel) or path.is_symlink() or not path.is_file():
            continue
        data = path.read_bytes()
        if len(data) > MAX_FILE_BYTES or b"\0" in data[:8000]:
            continue
        text = data.decode("utf-8", errors="replace")
        if name.search(text) or any(stem in Path(rel).name for stem in stems):
            found.append((not any(stem in Path(rel).name for stem in stems), rel, text))

    parts, omitted, used = [], [], 0
    for _, rel, text in sorted(found):
        if used + len(text) > MAX_RELATED_TEST_BYTES:
            omitted.append(rel)
            continue
        used += len(text)
        parts += [f'<file path="{rel}">', text, "</file>"]
    if omitted:
        parts.append("Also related, not included (over the size limit): " + ", ".join(omitted))
    return "\n".join(["<related_tests>", *parts, "</related_tests>"]) if parts or omitted else ""


def _quote_lines(quote: str, text: str) -> list[int]:
    """Line numbers where the quote starts, ignoring indentation and blank lines.

    The quote can start partway through its first line and end partway through its last.
    """
    want = [line.strip() for line in quote.splitlines() if line.strip()]
    if not want:
        return []
    numbered = [(i, s) for i, line in enumerate(text.splitlines(), 1) if (s := line.strip())]
    hits = []
    for start in range(len(numbered) - len(want) + 1):
        window = [s for _, s in numbered[start:start + len(want)]]
        if len(want) == 1:
            found = want[0] in window[0]
        else:
            found = (window[0].endswith(want[0]) and window[-1].startswith(want[-1])
                     and window[1:-1] == want[1:-1])
        if found:
            hits.append(numbered[start][0])
    return hits


def locate_quote(repo: Path, base: str, files: dict[str, str], file: str, quote: str, line: int) -> int | None:
    """Find where a finding's quote is.

    Returns the closest line in the current file, 0 if the quote is only in the
    base version (code the change removed), or None if it isn't in either.
    """
    unmarked = "\n".join(l[1:] if l.startswith(("+", "-")) else l for l in quote.splitlines())
    candidates = [quote, unmarked]  # reviewers sometimes copy the markers from the diff

    current = files.get(file)
    if current is None:  # not sent to the reviewer: too large, or found through --explore
        path = resolve_in_repo(repo, file)
        ignored = subprocess.run(["git", "check-ignore", "-q", "--", file], cwd=repo).returncode == 0
        if path is not None and path.is_file() and not ignored:  # never open secrets like .env
            current = path.read_bytes().decode("utf-8", errors="replace")
    for candidate in candidates:
        if hits := _quote_lines(candidate, current or ""):
            return min(hits, key=lambda hit: abs(hit - line))

    if resolve_in_repo(repo, file) is None:
        return None
    result = subprocess.run(["git", "show", f"{base}:{file}"], cwd=repo, capture_output=True)
    if result.returncode == 0:
        before = result.stdout.decode("utf-8", errors="replace")
        if any(_quote_lines(candidate, before) for candidate in candidates):
            return 0
    return None


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
