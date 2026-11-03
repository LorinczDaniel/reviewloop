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

# decide on each finding yourself instead of using the overseer
reviewloop --triage
```

Before the fixer edits anything, it asks for confirmation (skip that with `-y`). The original of every file it touches is saved under `.reviewloop/backup/`, and `git diff` shows what changed.

When it finishes, the report is in `<repo>/.reviewloop/report.md` (with `report.json` next to it). The `.reviewloop` folder ignores itself, so it never shows up in your git status.

By default each agent is a headless `claude -p` call on your Claude Code login, so you don't need an API key. Usage counts against your Claude plan. To use the Anthropic API instead, pass `--backend api` and set `ANTHROPIC_API_KEY`.

### Options

| Option | Default | What it does |
|---|---|---|
| `--base REF` | `HEAD` | Review changes against this git ref, e.g. `main` |
| `--no-fix` | off | One review pass and a report; no files are changed |
| `--triage` | off | Mark each finding fix, skip or false positive yourself; the overseer doesn't run. Needs an interactive terminal |
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

Exit codes: `0` approved, nothing to review, or report-only; `1` not converged (blocking findings remain) or triage aborted; `2` usage error. This lets you use it in scripts or CI, e.g. `reviewloop -y --check "pytest -q" && git commit`.

## How it converges

- **Findings must quote the code.** Each finding includes the exact code it's about, and its line number is corrected to where that code actually is. A finding whose quote isn't in the file, or in the base version for code the change removed, is dropped: it doesn't block approval, prompt you or reach the fixer. Dropped findings are logged and listed in the report. Blockers are never dropped: one with an unmatched quote stays open, marked `unverified`, so it still blocks approval until it's resolved or you close it with `--triage`. Indentation, blank lines and leading diff markers (`+`/`-`) are ignored when matching.
- **Severity gating.** Only `blocker` and `major` findings block approval (`--block` changes this). Minor issues and nits get reported but never hold up the loop.
- **Re-reviews check earlier findings.** From round 2 on, each reviewer gets its own open findings plus the diff of the latest fixes. It marks each finding resolved or still open, and only adds new issues.
- **The overseer settles conflicts.** Priority is safety > security > readability. Findings it closes as `wont_fix` (each with a reason) are not raised again. It cannot defer a blocking finding or close a blocker as `wont_fix`. A blocker the fixer can't resolve stops the run at the round limit, and you can close it yourself with `--triage`.
- **Hard limits.** `--max-rounds` (default 4), and any failing `--check` blocks approval.

## Triage

With `--triage`, you make the overseer's decisions yourself. After round 1, the loop lists every finding, most severe first, and you mark each one:

- **Fix:** the fixer works on it. It stays open until a reviewer confirms it's resolved, and you aren't asked about it again.
- **Skip:** closes it without changing the code.
- **False positive:** closes it and records that it wasn't a real problem.
- **Won't fix:** closes it and records that it's real but you accept it. You're asked for a one-line reason.

Findings closed in any of these ways stop blocking approval, even blockers, and reviewers won't raise them again during the run unless the fixes make them materially worse. The report lists them under their own headings, so an approved run can still include blockers you chose to close.

The overseer never runs in this mode. If a later round finds something new, you're asked about those findings too, except in the final round, because no review would be left to verify a fix. Failing `--check` commands still block approval and go to the fixer.

Pressing Ctrl-C or Ctrl-D at a prompt stops the run with exit code `1`. The fixer doesn't run that round, and the report keeps the findings and the decisions you've made.

## Project rules

Every agent gets your project's conventions in its prompt, so reviewers don't have to guess at your style. These files are loaded from the repo root if they exist, most specific first:

1. `.reviewloop/rules.md`
2. `CLAUDE.md`
3. `CONTRIBUTING.md`

The log shows which files were loaded and why any were left out. Each file is capped at 20,000 bytes.

**A change can't rewrite its own rules.** Tracked files are read from `--base`, so with the default base, uncommitted edits to `CLAUDE.md` don't count until you commit them. A rule file added by the change under review isn't loaded at all. Symlinks aren't followed, and neither are `@` imports inside `CLAUDE.md`.

`.reviewloop/rules.md` is the place for rules meant only for reviewloop. `.reviewloop` ignores itself in git, so the file stays local to your clone and works without committing it. To share it, run `git add -f .reviewloop/rules.md`; it's then treated like any other tracked file.

Rules inform the agents, but they can't narrow a review, lower a severity or change this process.

With the `claude-code` backend, agents run with `--setting-sources user`. Your own `~/.claude` settings still apply, including your personal `CLAUDE.md`, but project-level Claude Code settings, hooks and auto-loaded `CLAUDE.md` files don't. With `--explore`, reviewers can still read any file in the working tree, including an edited `CLAUDE.md`.

## Suppressions

When you mark a finding as a false positive or won't fix in triage, the decision is saved to `.reviewloop/suppressions.json`, so later runs don't raise it again. Skipped findings aren't saved, because skipping only means not now. The overseer's `wont_fix` is saved only for severities that don't block approval (see `--block`), so only your decisions can suppress anything blocking. If you pass `--out`, the file lives in that directory instead.

Later runs use the file in two ways:

- **Reviewers are told.** Each reviewer gets its own earlier dismissals for the files under review, and is asked not to report them again unless the code has changed so that the reason no longer holds.
- **Exact repeats are filtered.** If a reviewer still reports a finding it reported before, with the same title on the same file and the flagged line unchanged, the finding is marked suppressed. It doesn't block approval or prompt you, but the report still lists it.

An overseer dismissal never covers anything that blocks approval: if a finding it closed comes back at a blocking severity, or `--block` now includes that severity, it isn't filtered. Your own decisions hold at any severity. Each entry records `decided_by`, `severity` and `reason`, so you can check them. To bring a finding back, delete its entry; to start over, delete the file. If the file is malformed, the run stops with exit code `2` before any review starts, and the file isn't changed.

The file is per clone, because `.reviewloop` ignores itself in git.

## Custom reviewers

Put `<name>.md` files in a directory and pass `--reviewer-dir DIR`. Each file describes that reviewer's area; the shared rules in `src/reviewloop/prompts/reviewer_base.md` are prepended automatically. Use `--reviewers safety,security` to run only some of them.

## Development

```bash
uv run pytest      # offline tests with a scripted fake LLM
```
