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

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/getting-started/installation/)
- git (it reviews the diff of a git repository)
- [Claude Code](https://code.claude.com), installed and logged in (`claude` on your PATH). Alternatively, an Anthropic API key with `--backend api`.

## Install

```bash
uv tool install git+https://github.com/LorinczDaniel/reviewloop.git
```

This puts the `reviewloop` command in `~/.local/bin`. If the command isn't found afterwards, run `uv tool update-shell` and open a new terminal.

Update with `uv tool upgrade reviewloop`, and uninstall with `uv tool uninstall reviewloop`.

To hack on it, clone the repo and install it as editable, so changes to the code or prompts take effect right away:

```bash
git clone git@github.com:LorinczDaniel/reviewloop.git
uv tool install --editable ./reviewloop
```

## Usage

Run it from inside the git repository you want reviewed. By default it reviews your uncommitted changes (tracked and new files) against `HEAD`.

```bash
cd /path/to/your/repo

# review only: report findings without touching any files (a good first run)
reviewloop --no-fix

# full loop: review, fix, re-review; tests and lint must pass to approve
reviewloop --check "pytest -q" --check "ruff check ."

# review everything on your branch compared to main
reviewloop --base main

# quicker and cheaper
reviewloop --fast
```

Before the fixer edits anything, it asks for confirmation (skip that with `-y`). The original of every file it touches is saved under `.reviewloop/backup/`, and `git diff` shows what changed.

When it finishes, the report is in `<repo>/.reviewloop/report.md` (with `report.json` next to it). The `.reviewloop` folder ignores itself, so it never shows up in your git status.

By default each agent is a headless `claude -p` call on your Claude Code login, so you don't need an API key. Usage counts against your Claude plan. To use the Anthropic API instead, pass `--backend api` and set `ANTHROPIC_API_KEY`.

### Options

| Option | Default | What it does |
|---|---|---|
| `--base REF` | `HEAD` | Review changes against this git ref, e.g. `main` |
| `--no-fix` | off | One review pass and a report; no files are changed |
| `--check CMD` | none | Command that must pass before approval, e.g. `"pytest -q"`. Repeatable |
| `--max-rounds N` | `4` | Stop and hand back to you after N rounds |
| `--block LIST` | `blocker,major` | Severities that block approval |
| `--fast` | off | Claude Sonnet 5.5 at medium effort instead of Claude Opus 5.5 at high. Quicker; use the default for security-sensitive code |
| `--model`, `--effort` | `claude-opus-5-5`, `high` | Choose the model and effort directly |
| `--explore` | off | Reviewers can read and grep the whole repo, not just the diff. Slower, but catches issues in surrounding code |
| `--reviewers LIST` | all | Run only some reviewers, e.g. `safety,security` |
| `--reviewer-dir DIR` | none | Add your own reviewers (see below) |
| `--backend` | `claude-code` | `claude-code` or `api` |
| `--repo PATH` | current directory | Repository to review |
| `--out DIR` | `<repo>/.reviewloop` | Where reports and backups go |
| `-y`, `--yes` | off | Don't ask before the fixer edits files |

Exit codes: `0` approved, nothing to review, or report-only; `1` not converged (blocking findings remain); `2` usage error. This lets you use it in scripts or CI, e.g. `reviewloop -y --check "pytest -q" && git commit`.

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
