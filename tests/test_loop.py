"""End-to-end loop tests against a throwaway git repo, with a scripted fake LLM."""

import io
import json
import subprocess
from pathlib import Path

import pytest

from reviewloop import cli, report, suppressions
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


def suppressions_file(repo):
    return repo / ".reviewloop" / suppressions.FILENAME


def saved(repo):
    return json.loads(suppressions_file(repo).read_text())["suppressions"]


def suppressed_block(prompt):
    if "<suppressed_findings>" not in prompt:
        return ""
    return prompt.split("<suppressed_findings>")[1].split("</suppressed_findings>")[0]


class WontFixLLM(TriageLLM):
    """Overseer closes SEC-1 as wont_fix, instead of failing the test like TriageLLM."""

    def structured(self, system, user, schema, tools=None):
        if schema is OVERSEER_SCHEMA:
            return {"summary": "", "decisions": [
                {"id": "SEC-1", "action": "wont_fix", "reason": "ORM escapes it", "guidance": ""}]}
        return super().structured(system, user, schema, tools)


def test_false_positive_is_not_raised_again_on_next_run(repo):
    assert triage(repo, TriageLLM(severity="major"), "p").findings["SEC-1"].status == "false_positive"
    entry = saved(repo)[0]
    assert (entry["reviewer"], entry["file"], entry["decided_by"]) == ("security", "app.py", "human")

    # scripted() raises IndexError if the second run prompts.
    llm = TriageLLM(severity="major")
    outcome = triage(repo, llm)
    assert outcome.status == "approved"
    assert outcome.findings["SEC-1"].status == "suppressed"
    assert "SQL injection via name" in suppressed_block(llm.review_prompts[0])
    text = report.write(outcome, repo / ".reviewloop", Usage()).read_text()
    assert "## Suppressed (dismissed in an earlier decision) (1)" in text
    assert "marked false positive in triage" in text


def test_overseer_wont_fix_on_a_minor_is_suppressed_on_next_run(repo):
    # A failing check makes the overseer run, since a minor alone would be approved.
    run(repo, WontFixLLM(severity="minor", fixer_edit_old="no such text"), checks=["false"], max_rounds=2)
    assert saved(repo)[0]["decided_by"] == "overseer"
    assert saved(repo)[0]["reason"] == "ORM escapes it"

    outcome = run(repo, TriageLLM(severity="minor"))
    assert outcome.status == "approved"
    assert outcome.findings["SEC-1"].status == "suppressed"


def test_overseer_wont_fix_on_a_major_closes_it_but_is_not_saved(repo):
    assert run(repo, WontFixLLM(severity="major")).findings["SEC-1"].status == "wont_fix"
    assert not suppressions_file(repo).exists()

    outcome = triage(repo, TriageLLM(severity="major"), "s")  # prompts again
    assert outcome.findings["SEC-1"].status == "skipped"


@pytest.mark.parametrize("answer", ["w", "W", "wont fix", "won't fix"])
def test_human_wont_fix_is_saved_with_reason_and_suppressed(repo, answer):
    outcome = triage(repo, TriageLLM(), answer, "accepted risk, internal tool")
    assert outcome.status == "approved"
    assert outcome.findings["SEC-1"].status == "accepted"
    assert outcome.rounds[0].decisions[0]["action"] == "wont_fix"
    assert (repo / "app.py").read_text() == BUGGY
    assert saved(repo)[0]["decided_by"] == "human"
    assert "accepted risk, internal tool" in saved(repo)[0]["reason"]
    text = report.write(outcome, repo / ".reviewloop", Usage()).read_text()
    assert "## Won't fix (you) (1)" in text
    assert "accepted risk, internal tool" in text

    llm = TriageLLM()
    next_run = triage(repo, llm)
    assert next_run.findings["SEC-1"].status == "suppressed"
    assert "accepted risk, internal tool" in suppressed_block(llm.review_prompts[0])


def test_human_wont_fix_needs_a_reason(repo):
    outcome = triage(repo, TriageLLM(), "w", "", "   ", "accepted risk")
    assert outcome.findings["SEC-1"].status == "accepted"
    assert "accepted risk" in saved(repo)[0]["reason"]


def test_abort_at_reason_prompt_saves_nothing(repo):
    outcome = triage(repo, TriageLLM(), "w", EOFError())
    assert outcome.status == "triage_aborted"
    assert outcome.findings["SEC-1"].status == "open"
    assert not suppressions_file(repo).exists()


def test_approved_headline_does_not_claim_reviewers_are_satisfied(repo):
    outcome = triage(repo, TriageLLM(), "s")
    text = report.write(outcome, repo / ".reviewloop", Usage()).read_text()
    assert "no blocking findings remain" in text
    assert "every reviewer is satisfied" not in text


def test_skip_is_not_saved(repo):
    triage(repo, TriageLLM(), "s")
    assert not suppressions_file(repo).exists()


def test_changed_code_is_not_filtered(repo):
    triage(repo, TriageLLM(severity="major"), "p")
    (repo / "app.py").write_text(BUGGY.replace("users", "accounts"))

    llm = TriageLLM(severity="major")
    outcome = triage(repo, llm, "s")  # prompts again
    assert outcome.findings["SEC-1"].status == "skipped"
    assert "SQL injection via name" in suppressed_block(llm.review_prompts[0])


def test_reworded_title_is_not_filtered(repo):
    class RewordLLM(TriageLLM):
        def structured(self, system, user, schema, tools=None):
            result = super().structured(system, user, schema, tools)
            for finding in result.get("findings", []):
                finding["title"] = "Unsafe string interpolation"
            return result

    triage(repo, TriageLLM(severity="major"), "p")
    assert triage(repo, RewordLLM(severity="major"), "s").findings["SEC-1"].status == "skipped"


def test_title_case_and_spacing_still_match(repo):
    class RecaseLLM(TriageLLM):
        def structured(self, system, user, schema, tools=None):
            result = super().structured(system, user, schema, tools)
            for finding in result.get("findings", []):
                finding["title"] = "sql  INJECTION via name"
            return result

    triage(repo, TriageLLM(severity="major"), "p")
    assert triage(repo, RecaseLLM(severity="major")).findings["SEC-1"].status == "suppressed"


class NoLineLLM(TriageLLM):
    def structured(self, system, user, schema, tools=None):
        result = super().structured(system, user, schema, tools)
        for finding in result.get("findings", []):
            finding["line"] = 0
        return result


def test_finding_without_a_line_is_not_filtered_or_saved_twice(repo):
    triage(repo, NoLineLLM(severity="major"), "p")
    assert triage(repo, NoLineLLM(severity="major"), "p").findings["SEC-1"].status == "false_positive"
    assert len(saved(repo)) == 1


def test_human_false_positive_on_a_blocker_is_suppressed_on_next_run(repo):
    assert triage(repo, TriageLLM(), "p").status == "approved"
    assert saved(repo)[0]["severity"] == "blocker"

    llm = TriageLLM()
    outcome = triage(repo, llm)
    assert outcome.status == "approved"
    assert outcome.findings["SEC-1"].status == "suppressed"
    assert "SQL injection via name" in suppressed_block(llm.review_prompts[0])


def test_overseer_cannot_close_a_blocker(repo):
    outcome = run(repo, WontFixLLM())
    assert outcome.rounds[0].decisions[0]["action"] == "fix"
    assert outcome.findings["SEC-1"].status == "resolved"
    assert (repo / "app.py").read_text() == FIXED
    assert not suppressions_file(repo).exists()


def test_blocker_the_fixer_cannot_resolve_goes_to_a_human(repo):
    outcome = run(repo, WontFixLLM(fixer_edit_old="no such text"), max_rounds=2)
    assert outcome.status == "not_converged"
    assert outcome.findings["SEC-1"].status == "open"


def write_overseer_entry(repo, severity):
    suppressions_file(repo).parent.mkdir()
    suppressions_file(repo).write_text(json.dumps({"version": 1, "suppressions": [{
        "reviewer": "security", "severity": severity, "file": "app.py", "title": "SQL injection via name",
        "detail": "d", "code": BUGGY.splitlines()[1].strip(), "decided_by": "overseer", "reason": "r",
        "decided_at": "2026-11-02"}]}))


@pytest.mark.parametrize("reported", ["major", "blocker"])
def test_overseer_dismissal_of_a_minor_does_not_cover_a_blocking_finding(repo, reported):
    write_overseer_entry(repo, "minor")
    assert triage(repo, TriageLLM(severity=reported), "s").findings["SEC-1"].status == "skipped"


def test_overseer_dismissal_stops_applying_when_block_includes_its_severity(repo):
    write_overseer_entry(repo, "minor")
    llm = TriageLLM(severity="minor")
    outcome = triage(repo, llm, "s", blocking={"blocker", "major", "minor"})
    assert outcome.findings["SEC-1"].status == "skipped"
    assert suppressed_block(llm.review_prompts[0]) == ""


def test_human_dismissal_of_a_major_covers_a_blocker(repo):
    triage(repo, TriageLLM(severity="major"), "p")
    assert triage(repo, TriageLLM(severity="blocker")).findings["SEC-1"].status == "suppressed"


def test_overseer_blocker_entry_added_to_the_file_is_ignored(repo):
    code = BUGGY.splitlines()[1].strip()
    suppressions_file(repo).parent.mkdir()
    suppressions_file(repo).write_text(json.dumps({"version": 1, "suppressions": [{
        "reviewer": "security", "severity": "blocker", "file": "app.py", "title": "SQL injection via name",
        "detail": "d", "code": code, "decided_by": "overseer", "reason": "r", "decided_at": "2026-11-02"}]}))

    # Reported at a lower severity, so only the entry's own severity can stop the match.
    llm = TriageLLM(severity="major")
    outcome = triage(repo, llm, "s")
    assert outcome.findings["SEC-1"].status == "skipped"
    assert suppressed_block(llm.review_prompts[0]) == ""


def test_reviewers_only_see_their_own_suppressions_for_files_under_review(repo):
    def entry(reviewer, file, title):
        return {"reviewer": reviewer, "severity": "major", "file": file, "title": title, "detail": "d", "code": "x",
                "decided_by": "human", "reason": "r", "decided_at": "2026-11-02"}

    suppressions_file(repo).parent.mkdir()
    suppressions_file(repo).write_text(json.dumps({"version": 1, "suppressions": [
        entry("security", "app.py", "wanted"),
        entry("security", "other.py", "other file"),
        entry("safety", "app.py", "other reviewer"),
    ]}))
    llm = TriageLLM()
    triage(repo, llm, "s")
    block = suppressed_block(llm.review_prompts[0])
    assert "wanted" in block
    assert "other file" not in block
    assert "other reviewer" not in block


def test_abort_keeps_decisions_already_made(repo):
    class TwoFindingsLLM(TriageLLM):
        def structured(self, system, user, schema, tools=None):
            result = super().structured(system, user, schema, tools)
            if schema is REVIEW_SCHEMA and "Your area is security" in system:
                result["findings"].append({"severity": "major", "file": "app.py", "line": 1, "title": "second",
                                           "detail": "d", "suggestion": "s"})
            return result

    assert triage(repo, TwoFindingsLLM(severity="major"), "p", EOFError()).status == "triage_aborted"
    assert len(saved(repo)) == 1


@pytest.mark.parametrize("content", ["{not json", '{"version": 1}', '{"suppressions": [{"file": "a"}]}'])
def test_malformed_file_stops_the_run_and_is_kept(repo, monkeypatch, capsys, content):
    class NoCallsLLM:
        usage = Usage()

        def structured(self, *_):
            raise AssertionError("no review should run")

    suppressions_file(repo).parent.mkdir()
    suppressions_file(repo).write_text(content)
    monkeypatch.setattr(cli, "ClaudeCodeLLM", lambda **_: NoCallsLLM())
    assert cli.main(["--repo", str(repo), "-y"]) == 2
    assert suppressions.FILENAME in capsys.readouterr().err
    assert suppressions_file(repo).read_text() == content


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
