"""End-to-end loop tests against a throwaway git repo, with a scripted fake LLM."""

import io
import subprocess
from pathlib import Path

import pytest

from reviewloop import cli, report
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


class TriageLLM(FakeLLM):
    """FakeLLM that records review prompts and fails if the overseer is ever called."""

    def __init__(self, severity="blocker", **kw):
        super().__init__(**kw)
        self.severity = severity
        self.review_prompts = []

    def structured(self, system, user, schema, tools=None):
        if schema is OVERSEER_SCHEMA:
            raise AssertionError("overseer must not run in triage mode")
        result = super().structured(system, user, schema, tools)
        if schema is REVIEW_SCHEMA and "Your area is security" in system:
            self.review_prompts.append(user)
            for finding in result["findings"]:
                finding["severity"] = self.severity
        return result


def scripted(*answers):
    """An `ask` that replays answers and fails if asked more often than expected."""
    remaining = list(answers)

    def ask(_prompt):
        answer = remaining.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    return ask


def triage(repo, llm, *answers, **kw):
    cfg = Config(repo=repo, out_dir=repo / ".reviewloop", triage=True, **kw)
    return ReviewLoop(cfg, llm, load_reviewers(), log=lambda _: None, ask=scripted(*answers)).run()


def test_triage_fix_sends_finding_to_fixer(repo):
    outcome = triage(repo, TriageLLM(), "f")
    assert outcome.status == "approved"
    assert outcome.findings["SEC-1"].status == "resolved"
    assert (repo / "app.py").read_text() == FIXED
    assert outcome.rounds[0].decisions[0]["action"] == "fix"


@pytest.mark.parametrize("answer, status", [("s", "skipped"), ("p", "false_positive"), ("FP", "false_positive")])
def test_triage_closing_a_blocker_approves_without_fixing(repo, answer, status):
    outcome = triage(repo, TriageLLM(), answer)
    assert outcome.status == "approved"
    assert len(outcome.rounds) == 1
    assert outcome.findings["SEC-1"].status == status
    assert (repo / "app.py").read_text() == BUGGY


def test_triage_reprompts_on_invalid_answer(repo):
    outcome = triage(repo, TriageLLM(), "", "maybe", "s")
    assert outcome.findings["SEC-1"].status == "skipped"


def test_triage_offers_non_blocking_findings(repo):
    outcome = triage(repo, TriageLLM(severity="minor"), "f")
    assert outcome.status == "approved"
    assert len(outcome.rounds) == 2
    assert (repo / "app.py").read_text() == FIXED


def test_triage_retries_fix_without_asking_again(repo):
    # scripted() raises IndexError if the loop prompts a second time.
    outcome = triage(repo, TriageLLM(fixer_edit_old="no such text"), "f", max_rounds=3)
    assert outcome.status == "not_converged"
    assert len(outcome.rounds) == 3
    assert outcome.findings["SEC-1"].status == "open"


def test_triage_asks_about_findings_from_later_rounds(repo):
    class NewFindingLLM(TriageLLM):
        def structured(self, system, user, schema, tools=None):
            result = super().structured(system, user, schema, tools)
            if schema is REVIEW_SCHEMA and "Your area is security" in system and "<your_open_findings>" in user:
                result["findings"] = [{"severity": "major", "file": "app.py", "line": 1, "title": "new issue",
                                       "detail": "d", "suggestion": "s"}]
            return result

    outcome = triage(repo, NewFindingLLM(), "f", "p")
    assert outcome.status == "approved"
    assert outcome.findings["SEC-1"].status == "resolved"
    assert outcome.findings["SEC-2"].status == "false_positive"


def test_failing_check_still_blocks_after_triage_closes_finding(repo):
    outcome = triage(repo, TriageLLM(), "s", checks=["false"], max_rounds=2)
    assert outcome.status == "not_converged"
    assert outcome.findings["SEC-1"].status == "skipped"


@pytest.mark.parametrize("interrupt", [EOFError(), KeyboardInterrupt()])
def test_triage_abort_stops_before_fixer_and_writes_report(repo, interrupt):
    outcome = triage(repo, TriageLLM(), interrupt)
    assert outcome.status == "triage_aborted"
    assert outcome.findings["SEC-1"].status == "open"
    assert (repo / "app.py").read_text() == BUGGY
    path = report.write(outcome, repo / ".reviewloop", Usage())
    assert "Triage aborted" in path.read_text()


def test_closed_findings_reach_reviewers_and_report(repo):
    class TwoFindingsLLM(TriageLLM):
        def structured(self, system, user, schema, tools=None):
            result = super().structured(system, user, schema, tools)
            if schema is REVIEW_SCHEMA and "Your area is security" in system and "<your_open_findings>" not in user:
                result["findings"].append({"severity": "major", "file": "app.py", "line": 1, "title": "not real",
                                           "detail": "d", "suggestion": "s"})
            return result

    llm = TwoFindingsLLM()
    outcome = triage(repo, llm, "f", "p")
    assert outcome.status == "approved"
    assert "<closed_findings>" in llm.review_prompts[1]
    assert "[SEC-2]" in llm.review_prompts[1]
    text = report.write(outcome, repo / ".reviewloop", Usage()).read_text()
    assert "## False positive (you) (1)" in text
    assert "you marked false positive" in text


class FakeTTY(io.StringIO):
    def isatty(self):
        return True


def test_cli_runs_triage_through_real_prompt(repo, monkeypatch):
    monkeypatch.setattr(cli, "ClaudeCodeLLM", lambda **_: TriageLLM())
    monkeypatch.setattr("sys.stdin", FakeTTY("s\n"))
    assert cli.main(["--repo", str(repo), "--triage", "-y"]) == 0
    assert "## Skipped (you) (1)" in (repo / ".reviewloop/report.md").read_text()


@pytest.mark.parametrize("flags, tty, message", [
    (["--no-fix"], True, "--no-fix"),
    (["--max-rounds", "1"], True, "--max-rounds"),
    ([], False, "interactive terminal"),
])
def test_cli_rejects_triage_where_it_cannot_work(repo, monkeypatch, capsys, flags, tty, message):
    monkeypatch.setattr("sys.stdin", FakeTTY() if tty else io.StringIO())
    assert cli.main(["--repo", str(repo), "--triage", "-y", *flags]) == 2
    assert message in capsys.readouterr().err


def test_edits_reject_paths_outside_repo(repo):
    results, _, _ = apply_edits(repo, [{"file": "../evil.py", "old_str": "", "new_str": "x"}])
    assert not results[0].ok
    assert not (repo.parent / "evil.py").exists()


def test_edits_require_unique_match(repo):
    (repo / "dup.py").write_text("a\na\n")
    results, _, _ = apply_edits(repo, [{"file": "dup.py", "old_str": "a", "new_str": "b"}])
    assert not results[0].ok
    assert (repo / "dup.py").read_text() == "a\na\n"
