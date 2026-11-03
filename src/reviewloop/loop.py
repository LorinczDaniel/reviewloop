"""The overseeing loop: review in parallel, arbitrate, fix, repeat until everyone signs off."""

import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib.resources import files as package_files
from pathlib import Path
from typing import Callable, Protocol

from . import context, suppressions
from .edits import EditResult, apply_edits
from .llm import LLMError, Usage
from .schemas import FIXER_SCHEMA, OVERSEER_SCHEMA, REVIEW_SCHEMA, SEVERITIES, Finding
from .suppressions import Suppression

PROMPTS = package_files("reviewloop") / "prompts"
EXPLORE_TOOLS = ["Read", "Grep", "Glob"]
HEARTBEAT_SECONDS = 30
TRIAGE_ANSWERS = {
    "f": "fix", "fix": "fix",
    "s": "skip", "skip": "skip",
    "p": "false_positive", "fp": "false_positive", "false positive": "false_positive",
    "w": "wont_fix", "wont fix": "wont_fix", "won't fix": "wont_fix",
}
TRIAGE_STATUS = {"skip": "skipped", "false_positive": "false_positive", "wont_fix": "accepted"}
CLOSED_STATUSES = ("wont_fix", "deferred", *TRIAGE_STATUS.values())


class TriageAborted(Exception):
    """The user ended triage (Ctrl-C or Ctrl-D) before deciding on every finding."""


def _elapsed(start: float) -> str:
    secs = int(time.monotonic() - start)
    return f"{secs // 60}m{secs % 60:02d}s" if secs >= 60 else f"{secs}s"


class LLM(Protocol):
    usage: Usage

    def structured(self, system: str, user: str, schema: dict, tools: list[str] | None = None) -> dict: ...


@dataclass(frozen=True)
class Reviewer:
    name: str
    prefix: str
    system: str


def load_reviewers(names: list[str] | None = None, extra_dir: Path | None = None) -> list[Reviewer]:
    base = (PROMPTS / "reviewer_base.md").read_text()
    sources = {p.name.removesuffix(".md"): p for p in (PROMPTS / "reviewers").iterdir() if p.name.endswith(".md")}
    if extra_dir:
        sources |= {p.stem: p for p in extra_dir.glob("*.md")}
    if names:
        missing = set(names) - set(sources)
        if missing:
            raise ValueError(f"unknown reviewers: {', '.join(sorted(missing))} (have: {', '.join(sorted(sources))})")
        sources = {n: sources[n] for n in names}

    reviewers, prefixes = [], set()
    for name, path in sorted(sources.items()):
        prefix = name[:3].upper()
        while prefix in prefixes:
            prefix += "X"
        prefixes.add(prefix)
        reviewers.append(Reviewer(name, prefix, f"{base}\n\n{path.read_text()}"))
    return reviewers


@dataclass
class Config:
    repo: Path
    base: str = "HEAD"
    max_rounds: int = 4
    blocking: set[str] = field(default_factory=lambda: {"blocker", "major"})
    checks: list[str] = field(default_factory=list)
    fix: bool = True
    out_dir: Path | None = None
    explore: bool = False  # give reviewers read-only repo tools (claude-code backend)
    triage: bool = False  # the user decides on each finding; the overseer doesn't run


@dataclass
class RoundLog:
    number: int
    summaries: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    new: list[str] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    overseer_summary: str = ""
    decisions: list[dict] = field(default_factory=list)
    fixer_notes: str = ""
    edit_results: list[EditResult] = field(default_factory=list)
    fix_diff: str = ""
    checks: list[context.CheckResult] = field(default_factory=list)


@dataclass
class Outcome:
    status: str  # approved | not_converged | report_only | nothing_to_review | triage_aborted
    rounds: list[RoundLog]
    findings: dict[str, Finding]


class ReviewLoop:
    def __init__(self, cfg: Config, llm: LLM, reviewers: list[Reviewer], log: Callable[[str], None] = print,
                 ask: Callable[[str], str] = input):
        self.cfg, self.llm, self.reviewers, self.log, self.ask = cfg, llm, reviewers, log, ask
        self.findings: dict[str, Finding] = {}
        self.marked_fix: set[str] = set()  # finding ids the user sent to the fixer in triage
        self.counters = {r.prefix: 0 for r in reviewers}
        self.backup_dir = cfg.out_dir / "backup" if cfg.out_dir else None
        self.suppressions_path = cfg.out_dir / suppressions.FILENAME if cfg.out_dir else None
        self.suppressions = suppressions.load(self.suppressions_path) if self.suppressions_path else []

    # ---- main loop -------------------------------------------------------

    def run(self) -> Outcome:
        cfg = self.cfg
        files = context.changed_files(cfg.repo, cfg.base)
        if not files:
            return Outcome("nothing_to_review", [], self.findings)
        self.log(f"Reviewing {len(files)} file(s) against {cfg.base} with: {', '.join(r.name for r in self.reviewers)}")
        rules, notes = context.load_rules(cfg.repo, cfg.base, files)
        self.log(f"Project rules: {', '.join(rules) or 'none found'}")
        for note in notes:
            self.log(f"  {note}")

        rounds: list[RoundLog] = []
        checks = self._run_checks()
        fix_diff, failed_edits = "", []

        for n in range(1, cfg.max_rounds + 1):
            rl = RoundLog(n, checks=checks)
            rounds.append(rl)
            self.log(f"\n── Round {n}/{cfg.max_rounds} " + "─" * 40)
            ctx = context.gather(cfg.repo, cfg.base, files, checks, rules)

            self._review(n, ctx, fix_diff, rl)

            # Triage comes before the approval check so non-blocking findings get a say too.
            # The last round has no review left to verify a fix, so there is nothing to triage.
            triaging = cfg.triage and cfg.fix and n < cfg.max_rounds
            try:
                to_fix = self._triage(n, ctx, rl) if triaging else []
            except TriageAborted:
                self.log("Triage aborted; the fixer did not run this round.")
                return Outcome("triage_aborted", rounds, self.findings)

            if self._approved(rl) and not to_fix:
                self.log("No blocking findings remain and checks pass.")
                return Outcome("approved", rounds, self.findings)
            if not cfg.fix:
                return Outcome("report_only", rounds, self.findings)
            if n == cfg.max_rounds:
                break  # no review left to verify another fix

            if not triaging:
                to_fix = self._oversee(n, ctx, rl)
                if self._approved(rl):
                    self.log("Overseer closed the remaining blocking findings.")
                    return Outcome("approved", rounds, self.findings)

            fix_diff, failed_edits = self._fix(n, ctx, to_fix, failed_edits, rl)
            checks = self._run_checks()
            files = sorted(set(files) | set(context.changed_files(cfg.repo, cfg.base)))

        self.log("Stopped at the round limit with blocking findings still open.")
        return Outcome("not_converged", rounds, self.findings)

    def _run_checks(self) -> list[context.CheckResult]:
        if not self.cfg.checks:
            return []
        start = time.monotonic()
        results = context.run_checks(self.cfg.repo, self.cfg.checks)
        status = ", ".join(f"{c.command}: {'pass' if c.passed else 'FAIL'}" for c in results)
        self.log(f"  checks: {status} ({_elapsed(start)})")
        return results

    def _approved(self, rl: RoundLog) -> bool:
        return (
            not rl.errors
            and all(c.passed for c in rl.checks)
            and not any(f.is_blocking(self.cfg.blocking) for f in self.findings.values())
        )

    # ---- reviewers -------------------------------------------------------

    def _review(self, n: int, ctx: context.ReviewContext, fix_diff: str, rl: RoundLog) -> None:
        rendered = ctx.render()
        start = time.monotonic()
        self.log(f"  reviewing in parallel: {', '.join(r.name for r in self.reviewers)}")
        with ThreadPoolExecutor(max_workers=len(self.reviewers)) as pool:
            pending = {
                pool.submit(self.llm.structured, r.system, self._review_prompt(r, n, rendered, fix_diff, ctx.files),
                            REVIEW_SCHEMA, EXPLORE_TOOLS if self.cfg.explore else None): r
                for r in self.reviewers
            }
            while pending:
                done, _ = wait(pending, timeout=HEARTBEAT_SECONDS, return_when=FIRST_COMPLETED)
                if not done:
                    waiting = ", ".join(r.name for r in pending.values())
                    self.log(f"  ... still waiting on {waiting} ({_elapsed(start)})")
                for fut in done:
                    r = pending.pop(fut)
                    try:
                        result = fut.result()
                    except LLMError as e:
                        rl.errors[r.name] = str(e)
                        self.log(f"  {r.name}: ERROR {e} ({_elapsed(start)})")
                        continue
                    self._record_review(r, n, result, rl, _elapsed(start), ctx.files)

    def _review_prompt(self, r: Reviewer, n: int, rendered: str, fix_diff: str, files: dict[str, str]) -> str:
        mine = [f for f in self.findings.values() if f.reviewer == r.name]
        open_ = [f for f in mine if f.status == "open"]
        closed = [f for f in mine if f.status in CLOSED_STATUSES]
        parts = [rendered, f"\nThis is review round {n}."]
        dismissed = [s for s in self.suppressions
                     if s.reviewer == r.name and s.file in files and s.covers(s.severity, self.cfg.blocking)]
        if dismissed:
            parts.append("<suppressed_findings>")
            parts += [s.prompt_line() + "\n" for s in dismissed]
            parts.append("</suppressed_findings>")
        if n == 1:
            parts.append("Review the change in your area.")
            return "\n".join(parts)
        parts.append("<your_open_findings>")
        parts += [f.full() + "\nHistory: " + "; ".join(f.history) for f in open_] or ["(none)"]
        parts.append("</your_open_findings>")
        if closed:
            parts.append("<closed_findings>")
            parts += [f"{f.brief()} — {f.status}: {f.history[-1]}" for f in closed]
            parts.append("</closed_findings>")
        parts += ["<fixes_since_last_review>", fix_diff or "(no changes)", "</fixes_since_last_review>"]
        parts.append("Report the status of each open finding, then any new findings.")
        return "\n".join(parts)

    def _record_review(self, r: Reviewer, n: int, result: dict, rl: RoundLog, took: str, files: dict[str, str]) -> None:
        rl.summaries[r.name] = result["summary"]
        resolved = suppressed = dropped = unverified = 0
        for prev in result["previous"]:
            f = self.findings.get(prev["id"])
            if not f or f.reviewer != r.name or f.status != "open":
                continue
            f.history.append(f"r{n}: reviewer says {prev['status']} — {prev['note']}")
            if prev["status"] == "resolved":
                f.status = "resolved"
                rl.resolved.append(f.id)
                resolved += 1
        for item in result["findings"]:
            self.counters[r.prefix] += 1
            fid = f"{r.prefix}-{self.counters[r.prefix]}"
            finding = Finding(id=fid, reviewer=r.name, round_found=n, history=[f"r{n}: found"], **item)
            self.findings[fid] = finding
            located = context.locate_quote(self.cfg.repo, self.cfg.base, files, finding.file, finding.quote, finding.line)
            shown = " ".join(finding.quote.split())[:200] or "(empty)"
            if located is None and finding.severity not in self.cfg.blocking | {"blocker"}:
                finding.status = "dropped"
                finding.history.append(f"r{n}: dropped, quote not found in {finding.file}: {shown}")
                self.log(f"  {r.name}: dropped {finding.brief()}: quote not found")
                dropped += 1
                continue
            if located is None:  # a failed text match never removes something that blocks approval
                finding.verified = False
                finding.history.append(f"r{n}: unverified, quote not found in {finding.file}: {shown}")
                self.log(f"  {r.name}: kept {finding.brief()}: quote not found")
                unverified += 1
            else:
                finding.line = located
            code = suppressions.code_at(files, finding.file, finding.line)
            match = next((s for s in self.suppressions if s.covers(finding.severity, self.cfg.blocking)
                          and s.matches(r.name, finding.file, finding.title, code)), None)
            if match:
                finding.status = "suppressed"
                finding.history.append(f"r{n}: suppressed, dismissed earlier by {match.decided_by} — {match.reason}")
                suppressed += 1
            else:
                rl.new.append(fid)

        still_open = [f for f in self.findings.values() if f.reviewer == r.name and f.status == "open"]
        blocking = sum(f.severity in self.cfg.blocking for f in still_open)
        verdict = "approves" if not blocking else f"requests changes ({blocking} blocking)"
        counts = {"suppressed": suppressed, "dropped": dropped, "unverified": unverified}
        note = "".join(f", {count} {label}" for label, count in counts.items() if count)
        self.log(f"  {r.name}: {verdict}; {len(still_open)} open, {resolved} resolved this round{note} ({took})")

    # ---- suppressions ----------------------------------------------------

    def _remember(self, f: Finding, files: dict[str, str], decided_by: str, reason: str) -> None:
        """Save a dismissal right away, so an interrupted run keeps it."""
        code = suppressions.code_at(files, f.file, f.line)
        known = any(s.reviewer == f.reviewer and s.file == f.file and s.title == f.title and s.code == code
                    for s in self.suppressions)
        if known:
            return
        self.suppressions.append(Suppression(
            reviewer=f.reviewer, severity=f.severity, file=f.file, title=f.title, detail=f.detail, code=code,
            decided_by=decided_by, reason=reason, decided_at=datetime.now(timezone.utc).date().isoformat(),
        ))
        if not self.suppressions_path:
            return
        try:
            suppressions.save(self.suppressions_path, self.suppressions)
        except OSError as e:
            self.log(f"  warning: couldn't save {self.suppressions_path}: {e}")

    # ---- triage ----------------------------------------------------------

    def _triage(self, n: int, ctx: context.ReviewContext, rl: RoundLog) -> list[tuple[Finding, str]]:
        """Ask about each undecided finding; return every open finding the user wants fixed."""
        undecided = [f for f in self.findings.values() if f.status == "open" and f.id not in self.marked_fix]
        undecided.sort(key=lambda f: (SEVERITIES.index(f.severity), f.id))
        if undecided:
            self.log(f"\n  triage: {len(undecided)} finding(s) to decide on")
        for i, f in enumerate(undecided, 1):
            self.log(f"\n  ({i}/{len(undecided)}) {f.full()}")
            action = self._ask_action()
            reason = self._ask_reason() if action == "wont_fix" else ""
            label = "won't fix" if action == "wont_fix" else action.replace("_", " ")
            note = f"marked {label} in triage" + (f": {reason}" if reason else "")
            rl.decisions.append({"id": f.id, "action": action, "reason": note, "guidance": ""})
            f.history.append(f"r{n}: you {note}")
            if action == "fix":
                self.marked_fix.add(f.id)
            else:
                f.status = TRIAGE_STATUS[action]
            if action in ("false_positive", "wont_fix"):
                self._remember(f, ctx.files, "human", note)
        return [(f, "") for f in self.findings.values() if f.status == "open" and f.id in self.marked_fix]

    def _read(self, prompt: str) -> str:
        try:
            return self.ask(prompt).strip()
        except (EOFError, KeyboardInterrupt) as e:
            raise TriageAborted from e

    def _ask_action(self) -> str:
        while True:
            action = TRIAGE_ANSWERS.get(self._read("  [f]ix, [s]kip, false [p]ositive, [w]on't fix? ").lower())
            if action:
                return action
            self.log("  Please answer f, s, p or w.")

    def _ask_reason(self) -> str:
        while True:
            reason = self._read("  Why won't you fix it? ")
            if reason:
                return reason
            self.log("  Please give a one-line reason.")

    # ---- overseer --------------------------------------------------------

    def _oversee(self, n: int, ctx: context.ReviewContext, rl: RoundLog) -> list[tuple[Finding, str]]:
        open_ = [f for f in self.findings.values() if f.status == "open"]
        failing = [c for c in rl.checks if not c.passed]
        if not open_:
            return []

        prompt = "\n".join([
            ctx.render(),
            f"\nRound {n} of at most {self.cfg.max_rounds}. Blocking severities: {', '.join(sorted(self.cfg.blocking))}.",
            "Failing checks: " + (", ".join(c.command for c in failing) or "none"),
            "<open_findings>",
            *(f"{f.full()}\nReviewer: {f.reviewer}\nHistory: {'; '.join(f.history)}\n" for f in open_),
            "</open_findings>",
        ])
        system = (PROMPTS / "overseer.md").read_text()
        self.log(f"  overseer: deciding on {len(open_)} open finding(s)...")
        start = time.monotonic()
        try:
            result = self.llm.structured(system, prompt, OVERSEER_SCHEMA)
        except LLMError as e:
            self.log(f"  overseer: ERROR {e} — fixing all blocking findings by default")
            result = {"summary": f"overseer failed: {e}", "decisions": []}

        rl.overseer_summary = result["summary"]
        decided = {d["id"]: d for d in result["decisions"] if d["id"] in self.findings}
        to_fix = []
        for f in open_:
            blocking = f.severity in self.cfg.blocking
            d = decided.get(f.id) or {"id": f.id, "action": "fix" if blocking else "defer", "reason": "no decision returned", "guidance": ""}
            if d["action"] == "defer" and blocking:
                d = {**d, "action": "fix", "reason": f"cannot defer a {f.severity}; {d['reason']}"}
            if d["action"] == "wont_fix" and f.severity == "blocker":
                d = {**d, "action": "fix", "reason": f"only a human can close a blocker; {d['reason']}"}
            rl.decisions.append(d)
            if d["action"] == "fix":
                f.history.append(f"r{n}: overseer fix — {d['guidance'] or d['reason']}")
                to_fix.append((f, d["guidance"]))
            else:
                f.status = "wont_fix" if d["action"] == "wont_fix" else "deferred"
                f.history.append(f"r{n}: overseer {d['action']} — {d['reason']}")
                if d["action"] == "wont_fix" and not blocking:
                    self._remember(f, ctx.files, "overseer", d["reason"])

        counts = {a: sum(d["action"] == a for d in rl.decisions) for a in ("fix", "wont_fix", "defer")}
        self.log(f"  overseer: {counts['fix']} to fix, {counts['wont_fix']} won't fix, {counts['defer']} deferred ({_elapsed(start)})")
        return to_fix

    # ---- fixer -----------------------------------------------------------

    def _fix(self, n, ctx, to_fix, failed_edits, rl) -> tuple[str, list[EditResult]]:
        failing = [c for c in rl.checks if not c.passed]
        if not to_fix and not failing:
            return "", []

        parts = [ctx.render(), "\n<findings_to_fix>"]
        for f, guidance in to_fix:
            parts.append(f.full() + (f"\nOverseer guidance: {guidance}" if guidance else "") + "\n")
        parts.append("</findings_to_fix>")
        if failing:
            parts.append("These checks are failing and must pass: " + ", ".join(c.command for c in failing))
        if failed_edits:
            parts.append("<rejected_edits_last_round>")
            parts += [f"{e.file}: {e.message}" for e in failed_edits]
            parts.append("</rejected_edits_last_round>")

        system = (PROMPTS / "fixer.md").read_text()
        self.log(f"  fixer: working on {len(to_fix)} finding(s)" + (" and failing checks" if failing else "") + "...")
        start = time.monotonic()
        try:
            result = self.llm.structured(system, "\n".join(parts), FIXER_SCHEMA)
        except LLMError as e:
            self.log(f"  fixer: ERROR {e}")
            rl.fixer_notes = f"fixer failed: {e}"
            return "", []

        rl.fixer_notes = result["notes"]
        results, diff, originals = apply_edits(self.cfg.repo, result["edits"])
        self._backup(originals)
        rl.edit_results, rl.fix_diff = results, diff
        failed = [r for r in results if not r.ok]
        self.log(f"  fixer: {len(results) - len(failed)}/{len(results)} edit(s) applied ({_elapsed(start)})")
        for r in failed:
            self.log(f"    rejected {r.file}: {r.message}")
        return diff, failed

    def _backup(self, originals: dict[str, str | None]) -> None:
        """Keep the pre-review version of every file the fixer touches (first touch wins)."""
        if not self.backup_dir:
            return
        for rel, text in originals.items():
            dest = self.backup_dir / rel
            marker = dest.with_name(dest.name + ".did-not-exist")
            if dest.exists() or marker.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            if text is None:
                marker.touch()
            else:
                dest.write_text(text)
