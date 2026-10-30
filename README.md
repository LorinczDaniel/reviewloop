# reviewloop

Multi-reviewer code review loop powered by Claude. Specialist reviewers (safety, security, readability) review your diff in parallel. An overseer decides what to do with each finding, and a fixer applies the changes. The loop repeats until no blocking findings remain and your checks pass, or until it hits the round limit and hands off to you.

```
round N:  safety ─┐
          security ├─► findings ─► all clear? ── yes ─► done
          readability ┘                │ no
                              overseer: fix / wont_fix / defer
                                       │
                              fixer: exact-string edits ─► run checks ─► round N+1
```

## Install

```bash
uv tool install --editable ~/personal/review-loop   # puts `reviewloop` in ~/.local/bin
```

Because the install is editable, changes to the code or the prompts take effect immediately. Uninstall with `uv tool uninstall reviewloop`.

## Usage

Runs on your Claude Code login by default (each agent is a headless `claude -p` call), so no API key is needed. Use `--backend api` to call the Anthropic API directly with `ANTHROPIC_API_KEY` instead.

```bash
cd /path/to/your/repo

# review uncommitted changes, auto-fix, require tests + lint to pass
reviewloop --check "pytest -q" --check "ruff check ."

# review a branch against main, report only
reviewloop --base main --no-fix
```

`--fast` uses Claude Sonnet 5.5 at medium effort instead of Claude Opus 5.5 at high. It's much quicker and is fine for day-to-day changes; for anything security-sensitive, use the default.

`--explore` gives reviewers read-only `Read`/`Grep`/`Glob` access to the whole repo, so they can follow callers and definitions outside the diff. It's slower and costs more, but catches issues that depend on surrounding code.

Exit codes: `0` approved / nothing to review / report-only, `1` not converged, `2` usage error.

The report goes to `<repo>/.reviewloop/report.md` (with `report.json` next to it). Before the fixer first touches a file, the original is saved under `.reviewloop/backup/`.

## How it converges

- **Severity gating.** Only `blocker` and `major` findings block approval (`--block` changes this). Minor issues and nits get reported but never hold up the loop.
- **Re-reviews check earlier findings.** From round 2 on, each reviewer gets its own open findings plus the diff of the latest fixes. It marks each finding resolved or still open, and only adds new issues.
- **The overseer settles conflicts.** Priority is safety > security > readability. Findings it closes as `wont_fix` (each with a reason) are not raised again. It cannot defer a blocking finding.
- **Hard limits.** `--max-rounds` (default 4), and any failing `--check` blocks approval.

## Custom reviewers

Put `<name>.md` files in a directory and pass `--reviewer-dir DIR`. Each file describes that reviewer's area; the shared rules in `src/reviewloop/prompts/reviewer_base.md` are prepended automatically. Use `--reviewers safety,security` to run only some of them.

## Development

```bash
uv run pytest      # offline tests with a scripted fake LLM
```
