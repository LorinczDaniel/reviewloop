"""Command-line entry point: `reviewloop [options]`."""

import argparse
import subprocess
import sys
import time
from pathlib import Path

from . import context, report
from .llm import DEFAULT_MODEL, ApiLLM, ClaudeCodeLLM, LLMError
from .loop import Config, ReviewLoop, load_reviewers
from .schemas import SEVERITIES

FAST_MODEL = "claude-sonnet-5-5"
EXIT = {"approved": 0, "nothing_to_review": 0, "report_only": 0, "not_converged": 1}


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="reviewloop",
        description="Multi-reviewer code review loop: safety, security and readability reviewers "
                    "review the diff, an overseer arbitrates, a fixer applies changes, repeat.",
    )
    p.add_argument("--repo", type=Path, default=Path.cwd(), help="git repository to review (default: cwd)")
    p.add_argument("--base", default="HEAD", help="review changes against this ref (default: HEAD = uncommitted changes)")
    p.add_argument("--max-rounds", type=int, default=4, help="maximum review rounds (default: 4)")
    p.add_argument("--block", default="blocker,major",
                   help=f"comma-separated severities that block approval (from {','.join(SEVERITIES)})")
    p.add_argument("--check", action="append", default=[], metavar="CMD",
                   help="shell command that must pass, e.g. 'pytest -q' (repeatable)")
    p.add_argument("--no-fix", action="store_true", help="review once and report, without changing any files")
    p.add_argument("--reviewers", help="comma-separated subset of reviewers to run (default: all)")
    p.add_argument("--reviewer-dir", type=Path, help="directory of extra reviewer prompts (<name>.md)")
    p.add_argument("--backend", choices=["claude-code", "api"], default="claude-code",
                   help="claude-code: run `claude -p` on your Claude Code login (default); api: Anthropic API, needs ANTHROPIC_API_KEY")
    p.add_argument("--explore", action="store_true",
                   help="let reviewers read/grep the rest of the repo for context (claude-code backend only; slower)")
    p.add_argument("--model", help=f"Claude model (default: {DEFAULT_MODEL}, or {FAST_MODEL} with --fast)")
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"],
                   help="reasoning effort (default: high, or medium with --fast)")
    p.add_argument("--fast", action="store_true",
                   help=f"quicker, cheaper reviews: {FAST_MODEL} at medium effort (--model/--effort still override)")
    p.add_argument("--out", type=Path, help="report directory (default: <repo>/.reviewloop)")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask before letting the fixer edit files")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        repo = context.repo_root(args.repo)
    except subprocess.CalledProcessError:
        print(f"error: {args.repo} is not inside a git repository", file=sys.stderr)
        return 2

    blocking = {s.strip() for s in args.block.split(",") if s.strip()}
    if not blocking <= set(SEVERITIES):
        print(f"error: --block must be a subset of {SEVERITIES}", file=sys.stderr)
        return 2

    try:
        reviewers = load_reviewers(args.reviewers.split(",") if args.reviewers else None, args.reviewer_dir)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    out_dir = (args.out or repo / ".reviewloop").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / ".gitignore").write_text("*\n")  # keep reports out of the reviewed diff

    if not args.no_fix and not args.yes:
        answer = input(f"The fixer will edit files in {repo} (originals are backed up to {out_dir / 'backup'}). Continue? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            return 2

    cfg = Config(repo=repo, base=args.base, max_rounds=args.max_rounds, blocking=blocking,
                 checks=args.check, fix=not args.no_fix, out_dir=out_dir, explore=args.explore)
    model = args.model or (FAST_MODEL if args.fast else DEFAULT_MODEL)
    effort = args.effort or ("medium" if args.fast else "high")
    try:
        llm = (ClaudeCodeLLM(model=model, effort=effort, cwd=repo) if args.backend == "claude-code"
               else ApiLLM(model=model, effort=effort))
    except LLMError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.explore and args.backend != "claude-code":
        print("warning: --explore only works with --backend claude-code; ignoring", file=sys.stderr)
    print(f"Model: {model} (effort {effort}) via {args.backend}")
    start = time.monotonic()
    outcome = ReviewLoop(cfg, llm, reviewers).run()
    minutes, seconds = divmod(int(time.monotonic() - start), 60)

    path = report.write(outcome, out_dir, llm.usage)
    print(f"\n{report.HEADLINES[outcome.status]}\nTook {minutes}m{seconds:02d}s. Report: {path}")
    return EXIT[outcome.status]


if __name__ == "__main__":
    sys.exit(main())
