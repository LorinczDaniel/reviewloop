"""Command-line entry point: `reviewloop [options]`."""

import argparse
import math
import subprocess
import sys
import time
from pathlib import Path

from . import context, report
from .llm import DEFAULT_MODEL, ApiLLM, ClaudeCodeLLM, CodexLLM, CommandLLM, CopilotLLM, GeminiLLM, LLMError, Usage
from .loop import Config, ReviewLoop, load_reviewers
from .schemas import SEVERITIES

FAST_MODEL = "claude-sonnet-5-5"
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
DEFAULT_REVIEWER_MODELS = {"readability": (FAST_MODEL, "medium")}  # style rarely needs the top model at high effort
CLAUDE_BACKENDS = {"claude-code", "api"}  # where the Claude model defaults apply
BACKENDS = ["claude-code", "api", "codex", "gemini", "copilot", "command"]
EXIT = {"approved": 0, "nothing_to_review": 0, "report_only": 0, "not_converged": 1, "triage_aborted": 1, "over_budget": 1}


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="reviewloop",
        description="Multi-reviewer code review loop: safety, security, readability and tests reviewers "
                    "review the diff, an overseer arbitrates, a fixer applies changes, repeat.",
    )
    p.add_argument("--repo", type=Path, default=Path.cwd(), help="git repository to review (default: cwd)")
    p.add_argument("--base", default="HEAD", help="review changes against this ref (default: HEAD = uncommitted changes)")
    p.add_argument("--max-rounds", type=int, default=4, help="maximum review rounds (default: 4)")
    p.add_argument("--block", default="blocker,major",
                   help=f"comma-separated severities that block approval (from {','.join(SEVERITIES)})")
    p.add_argument("--check", action="append", default=[], metavar="CMD",
                   help="shell command that must pass, e.g. 'pytest -q' (repeatable)")
    p.add_argument("--agentic-fix", action="store_true",
                   help="when checks fail, let an agent run them and edit files until they pass "
                        "(claude-code backend; needs --check). It can run only the exact --check commands")
    p.add_argument("--budget", type=float, metavar="USD",
                   help="stop with a report once the cost reaches this many dollars, e.g. 2.00 "
                        "(claude-code backend only; calls already running finish, so it can go over)")
    p.add_argument("--no-fix", action="store_true", help="review once and report, without changing any files")
    p.add_argument("--triage", action="store_true",
                   help="mark each finding fix, skip or false positive yourself, instead of running the overseer")
    p.add_argument("--reviewers", help="comma-separated subset of reviewers to run (default: all)")
    p.add_argument("--reviewer-dir", type=Path, help="directory of extra reviewer prompts (<name>.md)")
    p.add_argument("--backend", choices=BACKENDS, default="claude-code",
                   help="claude-code: run `claude -p` on your Claude Code login (default); api: Anthropic API, needs "
                        "ANTHROPIC_API_KEY; codex: OpenAI Codex CLI (`codex exec`); gemini: Gemini CLI (`gemini -p`); "
                        "copilot: GitHub Copilot CLI (`copilot -p`); "
                        "command: any command given with --agent-cmd")
    p.add_argument("--agent-cmd", metavar="CMD",
                   help="with --backend command: the command to run for each agent call. It gets the prompt on stdin "
                        "and must print a JSON object. {model} and {effort} are replaced, e.g. 'opencode run -m {model}'")
    p.add_argument("--explore", action="store_true",
                   help="let reviewers read/grep the rest of the repo for context (claude-code backend only; slower)")
    p.add_argument("--model", help=f"model (default: {DEFAULT_MODEL}, or {FAST_MODEL} with --fast; "
                                   "with other backends, the harness's own default)")
    p.add_argument("--effort", choices=EFFORTS,
                   help="reasoning effort (default: high, or medium with --fast)")
    p.add_argument("--fast", action="store_true",
                   help=f"quicker, cheaper reviews: {FAST_MODEL} at medium effort (--model/--effort still override)")
    p.add_argument("--reviewer-model", action="append", default=[], metavar="NAME=MODEL[:EFFORT]",
                   help="model for one reviewer, e.g. readability=claude-sonnet-5-5:medium (repeatable). "
                        f"Default: {FAST_MODEL} at medium for readability, unless --model, --effort or --fast is set")
    p.add_argument("--out", type=Path, help="report directory (default: <repo>/.reviewloop)")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask before letting the fixer edit files")
    return p.parse_args(argv)


def reviewer_models(args: argparse.Namespace, names: set[str], effort: str) -> dict[str, tuple[str, str]]:
    """Which reviewers don't use the run's model: {name: (model, effort)}."""
    use_defaults = args.backend in CLAUDE_BACKENDS and not (args.model or args.effort or args.fast)
    chosen = {n: m for n, m in DEFAULT_REVIEWER_MODELS.items() if n in names} if use_defaults else {}
    for spec in args.reviewer_model:
        name, _, value = spec.partition("=")
        if not name or not value:
            raise ValueError(f"--reviewer-model must look like NAME=MODEL[:EFFORT], got {spec!r}")
        if name not in names:
            raise ValueError(f"--reviewer-model: no reviewer called {name!r} in this run (have: {', '.join(sorted(names))})")
        model, _, suffix = value.rpartition(":")  # model ids can contain ":" themselves
        chosen[name] = (model, suffix) if suffix in EFFORTS else (value, effort)
        if not chosen[name][0]:
            raise ValueError(f"--reviewer-model must look like NAME=MODEL[:EFFORT], got {spec!r}")
    return chosen


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

    if args.agentic_fix:
        problem = ("--agentic-fix needs --backend claude-code, the only harness whose tools can be limited to your "
                   "--check commands" if args.backend != "claude-code"
                   else "--agentic-fix can't be combined with --no-fix, because no fixer runs" if args.no_fix
                   else "--agentic-fix needs at least one --check" if not args.check
                   else None)
        scopable = context.scopable_checks(args.check)
        if not problem and not scopable:
            problem = ("--agentic-fix needs a plain --check command it can be limited to; "
                       "wrap anything using * ( ) & | ; < > $ or backticks in a script")
        if problem:
            print(f"error: {problem}", file=sys.stderr)
            return 2
        for command in set(args.check) - set(scopable):
            print(f"warning: --agentic-fix can't run {command!r} because it can't be limited to it; "
                  "wrap it in a script to include it", file=sys.stderr)

    if args.budget is not None:
        problem = ("--budget must be a positive number of dollars" if not (math.isfinite(args.budget) and args.budget > 0)
                   else f"--budget needs --backend claude-code, because the {args.backend} backend doesn't report a cost"
                   if args.backend != "claude-code"
                   else None)
        if problem:
            print(f"error: {problem}", file=sys.stderr)
            return 2

    if (args.agent_cmd is not None) != (args.backend == "command"):
        message = ("--backend command needs --agent-cmd" if args.backend == "command"
                   else "--agent-cmd only works with --backend command")
        print(f"error: {message}", file=sys.stderr)
        return 2

    if args.triage:
        problem = ("--triage can't be combined with --no-fix, because no fixer runs" if args.no_fix
                   else "--triage needs --max-rounds of at least 2, so a later round can verify the fixes" if args.max_rounds < 2
                   else "--triage needs an interactive terminal" if not sys.stdin.isatty()
                   else None)
        if problem:
            print(f"error: {problem}", file=sys.stderr)
            return 2

    try:
        reviewers = load_reviewers(args.reviewers.split(",") if args.reviewers else None, args.reviewer_dir)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    # Other harnesses don't run Claude by default, so they keep their own default model.
    model = args.model or ((FAST_MODEL if args.fast else DEFAULT_MODEL) if args.backend in CLAUDE_BACKENDS else None)
    effort = args.effort or ("medium" if args.fast else "high")
    try:
        per_reviewer = reviewer_models(args, {r.name for r in reviewers}, effort)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    out_dir = (args.out or repo / ".reviewloop").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / ".gitignore").write_text("*\n")  # keep reports out of the reviewed diff

    if not args.no_fix and not args.yes:
        agent = " An agent may also run your --check commands and edit files directly." if args.agentic_fix else ""
        answer = input(f"The fixer will edit files in {repo} (originals are backed up to {out_dir / 'backup'}).{agent} Continue? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            return 2

    cfg = Config(repo=repo, base=args.base, max_rounds=args.max_rounds, blocking=blocking,
                 checks=args.check, fix=not args.no_fix, out_dir=out_dir, explore=args.explore, triage=args.triage,
                 agentic_fix=args.agentic_fix, budget=args.budget)
    usage = Usage()  # shared, so the report and --budget total every model

    def make_llm(model: str | None, effort: str):
        match args.backend:
            case "claude-code":
                return ClaudeCodeLLM(model=model, effort=effort, cwd=repo, usage=usage)
            case "api":
                return ApiLLM(model=model, effort=effort, usage=usage)
            case "codex":
                return CodexLLM(model=model, effort=effort, cwd=repo, usage=usage)
            case "gemini":
                return GeminiLLM(model=model, effort=effort, cwd=repo, usage=usage)
            case "copilot":
                # Only an effort you chose: Copilot's default `auto` model rejects any effort.
                chosen = args.effort or ("medium" if args.fast else None)
                return CopilotLLM(model=model, effort=effort if chosen else None, cwd=repo, usage=usage)
            case _:
                return CommandLLM(model=model, effort=effort, cwd=repo, usage=usage, command=args.agent_cmd)

    try:
        llm = make_llm(model, effort)
        reviewer_llms = {name: make_llm(*choice) for name, choice in per_reviewer.items()}
    except LLMError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.explore and args.backend in ("api", "command"):
        print(f"warning: --explore doesn't work with --backend {args.backend}; ignoring", file=sys.stderr)
    elif args.backend in ("codex", "gemini", "copilot") and not args.explore:
        print(f"note: {args.backend} agents can always read the repo (read-only), as if --explore were on", file=sys.stderr)
    effort_shown = f" (effort {effort})" if getattr(llm, "effort", effort) else ""
    print(f"Model: {model or 'harness default'}{effort_shown} via {args.backend}")
    for name, (reviewer_model, reviewer_effort) in sorted(per_reviewer.items()):
        print(f"  {name} reviewer: {reviewer_model} (effort {reviewer_effort})")
    try:
        loop = ReviewLoop(cfg, llm, reviewers, reviewer_llms=reviewer_llms)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    start = time.monotonic()
    outcome = loop.run()
    minutes, seconds = divmod(int(time.monotonic() - start), 60)

    path = report.write(outcome, out_dir, usage)
    print(f"\n{report.HEADLINES[outcome.status]}\nTook {minutes}m{seconds:02d}s. Report: {path}")
    return EXIT[outcome.status]


if __name__ == "__main__":
    sys.exit(main())
