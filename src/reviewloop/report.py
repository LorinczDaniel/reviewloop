"""Writes the final review report as Markdown (for people) and JSON (for tooling)."""

import json
from dataclasses import asdict
from pathlib import Path

from .loop import Outcome
from .schemas import SEVERITIES

HEADLINES = {
    "approved": "✅ Approved — every reviewer is satisfied and all checks pass.",
    "not_converged": "⚠️ Not converged — blocking findings remain after the round limit. Needs a human.",
    "report_only": "📝 Report only — findings were not fixed (--no-fix).",
    "nothing_to_review": "Nothing to review — no changes against the base.",
    "triage_aborted": "🛑 Triage aborted — the fixer did not run this round. Decisions made so far are in this report.",
}


def write(outcome: Outcome, out_dir: Path, usage) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    findings = sorted(outcome.findings.values(), key=lambda f: (SEVERITIES.index(f.severity), f.id))

    md = ["# Code review report", "", HEADLINES[outcome.status], ""]
    md.append(f"Rounds: {len(outcome.rounds)} · API calls: {usage.calls} · "
              f"tokens in/out: {usage.input_tokens:,}/{usage.output_tokens:,}"
              + (f" · cost: ${usage.cost_usd:.2f}" if usage.cost_usd else ""))

    for title, statuses in [("Open", {"open"}), ("Won't fix (overseer)", {"wont_fix"}),
                            ("Skipped (you)", {"skipped"}), ("False positive (you)", {"false_positive"}),
                            ("Deferred", {"deferred"}), ("Resolved", {"resolved"})]:
        group = [f for f in findings if f.status in statuses]
        if not group:
            continue
        md += ["", f"## {title} ({len(group)})", ""]
        for f in group:
            md.append(f"- **{f.brief()}** _(by {f.reviewer})_")
            if f.status != "resolved":
                md.append(f"  - {f.detail}")
            md += [f"  - {h}" for h in f.history[1:]]

    for rl in outcome.rounds:
        md += ["", f"## Round {rl.number}", ""]
        for name, summary in rl.summaries.items():
            md.append(f"- **{name}:** {summary}")
        for name, err in rl.errors.items():
            md.append(f"- **{name}:** ERROR — {err}")
        for c in rl.checks:
            md.append(f"- check `{c.command}`: {'passed' if c.passed else 'FAILED'}")
        if rl.overseer_summary:
            md.append(f"- **overseer:** {rl.overseer_summary}")
        if rl.fixer_notes:
            md.append(f"- **fixer:** {rl.fixer_notes}")
        if rl.fix_diff:
            md += ["", "<details><summary>Fixes applied</summary>", "", "```diff", rl.fix_diff.rstrip(), "```", "", "</details>"]

    md_path = out_dir / "report.md"
    md_path.write_text("\n".join(md) + "\n")
    (out_dir / "report.json").write_text(json.dumps({
        "status": outcome.status,
        "findings": [asdict(f) for f in findings],
        "rounds": [asdict(r) for r in outcome.rounds],
        "usage": asdict(usage),
    }, indent=2))
    return md_path
