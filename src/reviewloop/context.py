"""Collects what the reviewers look at: the diff, full changed files, and check output."""

import subprocess
from dataclasses import dataclass
from pathlib import Path

MAX_FILE_BYTES = 200_000
MAX_CHECK_OUTPUT = 8_000


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

    def render(self) -> str:
        parts = ["<diff>", self.diff or "(empty diff)", "</diff>"]
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


def gather(repo: Path, base: str, files: list[str], checks: list[CheckResult]) -> ReviewContext:
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
    return ReviewContext(full_diff(repo, base, files), contents, skipped, checks)
