"""End-to-end loop tests against a throwaway git repo, with a scripted fake LLM."""

import subprocess
from pathlib import Path

import pytest

from reviewloop.edits import apply_edits
from reviewloop.llm import Usage
from reviewloop.loop import Config, ReviewLoop, load_reviewers
from reviewloop.schemas import FIXER_SCHEMA, OVERSEER_SCHEMA, REVIEW_SCHEMA

BUGGY = 'def get_user(db, name):\n    return db.execute(f"SELECT * FROM users WHERE name = \'{name}\'")\n'
FIXED = 'def get_user(db, name):\n    return db.execute("SELECT * FROM users WHERE name = ?", (name,))\n'


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "app.py").write_text("")
    git("add", ".")
    git("commit", "-qm", "init")
    (tmp_path / "app.py").write_text(BUGGY)
    return tmp_path


def _empty_review(summary="looks fine"):
    return {"summary": summary, "previous": [], "findings": []}


class FakeLLM:
    """Security flags SQL injection in round 1, confirms the fix in round 2."""

    def __init__(self, fixer_edit_old=BUGGY.splitlines()[1]):
        self.usage = Usage()
        self.fixer_edit_old = fixer_edit_old
        self.round = 0

    def structured(self, system, user, schema, tools=None):
        if schema is REVIEW_SCHEMA:
            if "Your area is security" not in system:
                return _empty_review()
            if "<your_open_findings>" not in user:
                return {"summary": "SQL injection", "previous": [], "findings": [{
                    "severity": "blocker", "file": "app.py", "line": 2,
                    "title": "SQL injection via name", "detail": "f-string query",
                    "suggestion": "use a parameterized query",
                }]}
            resolved = FIXED.splitlines()[1] in user
            return {"summary": "rechecked", "findings": [],
                    "previous": [{"id": "SEC-1", "status": "resolved" if resolved else "open", "note": ""}]}
        if schema is OVERSEER_SCHEMA:
            return {"summary": "fix it", "decisions": [
                {"id": "SEC-1", "action": "fix", "reason": "real", "guidance": ""}]}
        if schema is FIXER_SCHEMA:
            return {"notes": "", "edits": [{
                "file": "app.py", "old_str": self.fixer_edit_old,
                "new_str": FIXED.splitlines()[1], "finding_ids": ["SEC-1"]}]}
        raise AssertionError("unexpected schema")


def run(repo, llm, **kw):
    cfg = Config(repo=repo, out_dir=repo / ".reviewloop", **kw)
    return ReviewLoop(cfg, llm, load_reviewers(), log=lambda _: None).run()


def test_converges_after_fix(repo):
    outcome = run(repo, FakeLLM())
    assert outcome.status == "approved"
    assert len(outcome.rounds) == 2
    assert outcome.findings["SEC-1"].status == "resolved"
    assert (repo / "app.py").read_text() == FIXED
    assert (repo / ".reviewloop/backup/app.py").read_text() == BUGGY


def test_gives_up_when_fix_never_lands(repo):
    outcome = run(repo, FakeLLM(fixer_edit_old="no such text"), max_rounds=3)
    assert outcome.status == "not_converged"
    assert len(outcome.rounds) == 3
    assert (repo / "app.py").read_text() == BUGGY


def test_report_only_does_not_touch_files(repo):
    outcome = run(repo, FakeLLM(), fix=False)
    assert outcome.status == "report_only"
    assert (repo / "app.py").read_text() == BUGGY


def test_failing_check_blocks_approval(repo):
    outcome = run(repo, FakeLLM(), checks=["false"], max_rounds=2)
    assert outcome.status == "not_converged"


def test_edits_reject_paths_outside_repo(repo):
    results, _, _ = apply_edits(repo, [{"file": "../evil.py", "old_str": "", "new_str": "x"}])
    assert not results[0].ok
    assert not (repo.parent / "evil.py").exists()


def test_edits_require_unique_match(repo):
    (repo / "dup.py").write_text("a\na\n")
    results, _, _ = apply_edits(repo, [{"file": "dup.py", "old_str": "a", "new_str": "b"}])
    assert not results[0].ok
    assert (repo / "dup.py").read_text() == "a\na\n"
