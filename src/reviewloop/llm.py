"""Model backends: one call in, one schema-validated JSON object out.

Interchangeable backends with the same `structured()` method:
- ClaudeCodeLLM shells out to `claude -p` and uses your Claude Code login.
- ApiLLM calls the Anthropic API directly and needs ANTHROPIC_API_KEY.
- CodexLLM shells out to `codex exec` (OpenAI Codex CLI) and uses its login.
- GeminiLLM shells out to `gemini -p` (Gemini CLI) and uses its login.
- CopilotLLM shells out to `copilot -p` (GitHub Copilot CLI) and uses its login.
- CommandLLM runs any command you give it: the prompt goes in on stdin,
  and a JSON object is expected back on stdout.

ClaudeCodeLLM and CopilotLLM can also run the agentic check fixer, because
their tool permissions can be limited to the repo and the exact check commands.
"""

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MODEL = "claude-opus-5-5"
CLAUDE_CODE_TIMEOUT = 30 * 60
BASH_TIMEOUT_MS = "1800000"  # Claude Code's two-minute default would kill most test suites


class LLMError(RuntimeError):
    pass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    cost_usd: float = 0.0
    uncosted_calls: int = 0  # calls whose harness reports costs but didn't for this one

    def add(self, input_tokens: int, output_tokens: int, cost_usd: float | None = 0.0) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        if cost_usd is None:
            self.uncosted_calls += 1
        else:
            self.cost_usd += cost_usd
        self.calls += 1


@dataclass
class AgentScope:
    """What the agentic check fixer may do, for each backend to turn into its own permission rules.

    It can read and edit inside `repo` and run exactly `commands`. It can't read or
    edit `private` paths (repo-relative; a trailing slash means a whole directory)
    or .env* files, and it can't edit .git or .reviewloop.
    """

    repo: Path
    commands: list[str]
    private: list[str]


@dataclass
class ClaudeCodeLLM:
    model: str = DEFAULT_MODEL
    effort: str = "high"
    cwd: Path | None = None
    usage: Usage = field(default_factory=Usage)

    def __post_init__(self) -> None:
        if not shutil.which("claude"):
            raise LLMError("`claude` CLI not found on PATH; install Claude Code or use --backend api")

    def structured(self, system: str, user: str, schema: dict, tools: list[str] | None = None) -> dict:
        """Run one headless Claude Code session constrained to `schema`.

        `tools` is a list of built-in tool names (e.g. ["Read", "Grep"]); by
        default the session gets no tools and answers from the prompt alone.
        """
        return self._run(system, user, schema, tools or [])

    def agent(self, system: str, user: str, schema: dict, scope: AgentScope) -> dict:
        """Run a session that can use tools, limited to `scope`.

        Anything else is refused rather than prompted for. Rules are passed one
        argument each, because a check command can contain spaces or commas.
        """
        allowed, denied = self.permission_rules(scope)
        tools = ["Read", "Grep", "Glob", "Edit", "Write", "Bash"]
        return self._run(system, user, schema, tools, [
            "--permission-mode", "dontAsk", "--allowedTools", *allowed, "--disallowedTools", *denied,
        ], {**os.environ, "BASH_DEFAULT_TIMEOUT_MS": BASH_TIMEOUT_MS, "BASH_MAX_TIMEOUT_MS": BASH_TIMEOUT_MS})

    @staticmethod
    def permission_rules(scope: AgentScope) -> tuple[list[str], list[str]]:
        """Claude Code allow and deny rules for `scope`, relative to the repo it runs in."""
        paths = [f"./{p}**" if p.endswith("/") else f"./{p}" for p in scope.private]
        denied = ["Read(./.env*)", "Edit(./.env*)", "Edit(./.git/**)", "Edit(./.reviewloop/**)"]
        denied += [f"{tool}({path})" for path in paths for tool in ("Read", "Edit")]
        allowed = ["Read(./**)", "Grep(./**)", "Glob(./**)", "Edit(./**)", *(f"Bash({c})" for c in scope.commands)]
        return allowed, denied

    def _run(self, system: str, user: str, schema: dict, tools: list[str], flags: list[str] | None = None,
             env: dict[str, str] | None = None) -> dict:
        cmd = [
            "claude", "-p",
            "--output-format", "json",
            "--json-schema", json.dumps(schema),
            "--system-prompt", system,
            "--model", self.model,
            "--effort", self.effort,
            "--tools", ",".join(tools),
            "--no-session-persistence",
            # Project files can be part of the change under review, so don't let
            # Claude Code auto-load them. Project rules arrive in the prompt instead.
            "--setting-sources", "user",
            *(flags or []),
        ]
        try:
            proc = subprocess.run(
                cmd, input=user, capture_output=True, text=True,
                cwd=self.cwd, timeout=CLAUDE_CODE_TIMEOUT, env=env,
            )
        except subprocess.TimeoutExpired as e:
            raise LLMError(f"claude -p timed out after {CLAUDE_CODE_TIMEOUT}s") from e

        try:
            result = json.loads(proc.stdout)
        except json.JSONDecodeError:
            detail = (proc.stderr or proc.stdout).strip()[-500:]
            raise LLMError(f"claude -p failed (exit {proc.returncode}): {detail}") from None

        usage = result.get("usage") or {}
        self.usage.add(
            usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0),
            usage.get("output_tokens", 0),
            result.get("total_cost_usd") or 0.0,
        )

        if result.get("is_error") or result.get("subtype") != "success":
            raise LLMError(f"claude -p error ({result.get('subtype')}): {str(result.get('result'))[:500]}")
        output = result.get("structured_output")
        if not isinstance(output, dict):
            raise LLMError("claude -p returned no structured output")
        return output


@dataclass
class ApiLLM:
    model: str = DEFAULT_MODEL
    effort: str = "high"
    usage: Usage = field(default_factory=Usage)

    def __post_init__(self) -> None:
        import anthropic

        self.client = anthropic.Anthropic()

    def structured(self, system: str, user: str, schema: dict, tools: list[str] | None = None) -> dict:
        """Run one request constrained to `schema`. `tools` is ignored on this backend."""
        with self.client.beta.messages.stream(
            model=self.model,
            max_tokens=64000,
            system=system,
            messages=[{"role": "user", "content": user}],
            thinking={"type": "adaptive"},
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": schema},
            },
            # Security reviews can trip safety classifiers; let the API retry
            # a declined request on a fallback model instead of failing.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            response = stream.get_final_message()

        self.usage.add(response.usage.input_tokens or 0, response.usage.output_tokens or 0)

        if response.stop_reason == "refusal":
            category = response.stop_details.category if response.stop_details else None
            raise LLMError(f"model declined the request (category={category})")
        if response.stop_reason == "max_tokens":
            raise LLMError("response hit max_tokens before finishing")

        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise LLMError(f"no text block in response ({response._request_id})")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(f"invalid JSON from model ({response._request_id}): {e}") from e


# ---- other harnesses -------------------------------------------------------
#
# These harnesses don't take a separate system prompt (or don't enforce a
# schema), so the system prompt, the task and the expected JSON shape go in as
# one prompt, and the reply is parsed and checked against the schema here.


def _harness_prompt(system: str, user: str, schema: dict) -> str:
    return "\n\n".join([
        system.strip(),
        "<task>\n" + user.strip() + "\n</task>",
        "Reply with a single JSON object and nothing else: no prose, no code fence. "
        "It must match this JSON schema exactly:\n" + json.dumps(schema),
    ])


def extract_json(text: str) -> dict:
    """The JSON object in a model's reply: the whole reply, or else the last top-level {...} in it.

    Models without schema enforcement sometimes wrap the object in prose or a code fence.
    """
    decoder = json.JSONDecoder()
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    found, i = None, text.find("{")
    while i != -1:
        try:
            value, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            i = text.find("{", i + 1)
            continue
        if isinstance(value, dict):
            found = value
        i = text.find("{", end)
    if found is None:
        raise LLMError(f"no JSON object in the reply: {text.strip()[-300:]!r}")
    return found


def validate(value, schema: dict, path: str = "$"):
    """Check `value` against the small JSON schema subset in schemas.py, returning a cleaned copy.

    Unknown keys are dropped rather than rejected, and an integral float is
    accepted for an integer; anything else that doesn't fit raises LLMError.
    """
    kind = schema.get("type")
    if "enum" in schema and value not in schema["enum"]:
        raise LLMError(f"{path}: {value!r} is not one of {schema['enum']}")
    if kind == "object":
        if not isinstance(value, dict):
            raise LLMError(f"{path}: expected an object")
        props = schema.get("properties", {})
        missing = [k for k in schema.get("required", []) if k not in value]
        if missing:
            raise LLMError(f"{path}: missing {', '.join(missing)}")
        return {k: validate(v, props[k], f"{path}.{k}") for k, v in value.items() if k in props}
    if kind == "array":
        if not isinstance(value, list):
            raise LLMError(f"{path}: expected an array")
        return [validate(v, schema.get("items", {}), f"{path}[{i}]") for i, v in enumerate(value)]
    if kind == "string":
        if not isinstance(value, str):
            raise LLMError(f"{path}: expected a string")
    elif kind == "integer":
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if not isinstance(value, int) or isinstance(value, bool):
            raise LLMError(f"{path}: expected an integer")
    return value


@dataclass
class _HarnessLLM:
    """Shared plumbing for CLI harnesses that run one prompt and print a reply."""

    model: str | None = None  # None: the harness's own default
    effort: str = "high"
    cwd: Path | None = None
    usage: Usage = field(default_factory=Usage)
    name = "harness"

    def agent(self, system: str, user: str, schema: dict, scope: AgentScope) -> dict:
        raise LLMError(f"the {self.name} backend can't run the agentic check fixer; use claude-code or copilot")

    def _exec(self, cmd: list[str], stdin: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                                  cwd=self.cwd, timeout=CLAUDE_CODE_TIMEOUT)
        except subprocess.TimeoutExpired as e:
            raise LLMError(f"{self.name} timed out after {CLAUDE_CODE_TIMEOUT}s") from e
        except OSError as e:
            raise LLMError(f"couldn't run {cmd[0]}: {e}") from e

    def _failed(self, proc: subprocess.CompletedProcess) -> LLMError:
        detail = (proc.stderr or proc.stdout).strip()[-500:]
        return LLMError(f"{self.name} failed (exit {proc.returncode}): {detail}")


# Codex's reasoning effort tops out at xhigh.
CODEX_EFFORTS = {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "xhigh"}


@dataclass
class CodexLLM(_HarnessLLM):
    """`codex exec` in a read-only sandbox, with the reply constrained by --output-schema."""

    name = "codex"

    def __post_init__(self) -> None:
        if not shutil.which("codex"):
            raise LLMError("`codex` CLI not found on PATH; install the OpenAI Codex CLI or pick another --backend")

    def structured(self, system: str, user: str, schema: dict, tools: list[str] | None = None) -> dict:
        """`tools` is ignored: Codex can always read the repo, but its sandbox is read-only."""
        with tempfile.TemporaryDirectory(prefix="reviewloop-codex-") as tmp:
            schema_file, reply_file = Path(tmp) / "schema.json", Path(tmp) / "reply.txt"
            schema_file.write_text(json.dumps(schema))
            cmd = [
                "codex", "exec",
                "--sandbox", "read-only",
                "--skip-git-repo-check",
                "--json",
                "--output-schema", str(schema_file),
                "--output-last-message", str(reply_file),
                "-c", f"model_reasoning_effort={CODEX_EFFORTS.get(self.effort, self.effort)}",
                # AGENTS.md can be part of the change under review, so don't load it.
                # Project rules arrive in the prompt instead.
                "-c", "project_doc_max_bytes=0",
                *(["--model", self.model] if self.model else []),
                "-",  # read the prompt from stdin
            ]
            proc = self._exec(cmd, _harness_prompt(system, user, schema))
            error = self._read_events(proc.stdout)
            reply = reply_file.read_text() if reply_file.exists() else ""
        if error:
            raise LLMError(f"codex error: {error[:500]}")
        if proc.returncode != 0 or not reply.strip():
            raise self._failed(proc)
        return validate(extract_json(reply), schema)

    def _read_events(self, stdout: str) -> str | None:
        """Add up token usage from `--json` events, and return the last error message, if any."""
        error = None
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "turn.completed":
                usage = event.get("usage") or {}
                self.usage.add(usage.get("input_tokens", 0), usage.get("output_tokens", 0))
            elif kind == "turn.failed":
                error = str((event.get("error") or {}).get("message") or event)
            elif kind == "error":
                error = str(event.get("message") or event)
        return error


@dataclass
class GeminiLLM(_HarnessLLM):
    """`gemini -p` in plan (read-only) mode; the JSON reply is checked here."""

    name = "gemini"

    def __post_init__(self) -> None:
        if not shutil.which("gemini"):
            raise LLMError("`gemini` CLI not found on PATH; install the Gemini CLI or pick another --backend")

    def structured(self, system: str, user: str, schema: dict, tools: list[str] | None = None) -> dict:
        """`tools` is ignored: plan mode lets Gemini read the repo but not change it. `effort` has no flag."""
        cmd = [
            "gemini",
            "--output-format", "json",
            "--approval-mode", "plan",
            *(["--model", self.model] if self.model else []),
            "--prompt", "Answer the task above.",  # headless; appended to the prompt on stdin
        ]
        proc = self._exec(cmd, _harness_prompt(system, user, schema))
        try:
            result = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise self._failed(proc) from None
        if not isinstance(result, dict):
            raise self._failed(proc)
        for stats in ((result.get("stats") or {}).get("models") or {}).values():
            tokens = (stats or {}).get("tokens") or {}
            self.usage.add(tokens.get("prompt", 0), tokens.get("candidates", 0) + tokens.get("thoughts", 0))
        if result.get("error"):
            error = result["error"]
            raise LLMError(f"gemini error: {str(error.get('message') if isinstance(error, dict) else error)[:500]}")
        if not isinstance(result.get("response"), str):
            raise self._failed(proc)
        return validate(extract_json(result["response"]), schema)


COPILOT_USD_PER_AI_CREDIT = 0.01
CHECK_SERVER = "reviewloop"  # the MCP server name, so its tool is "reviewloop-run_check"


def _glob_literal(path: str) -> str:
    """Escape Copilot permission-glob characters, so a path matches only itself."""
    return "".join(f"[{c}]" if c in "*?[" else c for c in path)


@dataclass
class CopilotLLM(_HarnessLLM):
    """`copilot` with the prompt on stdin and only its read-only tools; the JSON reply is checked here."""

    effort: str | None = None  # None: don't pass one. Copilot's default `auto` model rejects any effort
    name = "copilot"

    def __post_init__(self) -> None:
        if not shutil.which("copilot"):
            raise LLMError("`copilot` CLI not found on PATH; install GitHub Copilot CLI or pick another --backend")

    def structured(self, system: str, user: str, schema: dict, tools: list[str] | None = None) -> dict:
        """`tools` is ignored: Copilot can always read the repo, but nothing else."""
        return self._run(_harness_prompt(system, user, schema), schema, [
            "--available-tools=view,grep,glob",
            "--allow-tool=read",
        ])

    def agent(self, system: str, user: str, schema: dict, scope: AgentScope) -> dict:
        """Run a session that can read and edit the repo and run the checks, limited to `scope`.

        Copilot's shell rules allow any arguments, and it approves read-only shell commands
        like `cat` by itself, which path rules don't cover. So the agent gets no shell: it
        runs the checks through reviewloop's own MCP tool, which takes a check's number and
        runs exactly that command.
        """
        with tempfile.TemporaryDirectory(prefix="reviewloop-checks-") as tmp:
            config = Path(tmp) / "checks.json"
            config.write_text(json.dumps({"repo": str(scope.repo), "commands": scope.commands}))
            server = {"mcpServers": {CHECK_SERVER: {
                "type": "local",
                "command": sys.executable,
                "args": ["-I", "-m", "reviewloop.check_server", str(config)],
                "tools": ["*"],
                "timeout": CLAUDE_CODE_TIMEOUT * 1000,  # test suites can be slow
            }}}
            runnable = "\n".join(f"  {i}: {c}" for i, c in enumerate(scope.commands, 1))
            user += ("\n\nIn this environment there's no shell. Run those commands with the "
                     f"{CHECK_SERVER}-run_check tool, by number:\n" + runnable)
            flags = ["--additional-mcp-config", json.dumps(server), *self.permission_flags(scope)]
            return self._run(_harness_prompt(system, user, schema), schema, flags)

    @staticmethod
    def permission_flags(scope: AgentScope) -> list[str]:
        """Copilot tool and permission flags for `scope`. Paths are absolute, because relative
        ones match by trailing components anywhere in the tree."""
        repo = _glob_literal(str(scope.repo.resolve()))
        private = [f"{repo}/{_glob_literal(p)}**" if p.endswith("/") else f"{repo}/{_glob_literal(p)}"
                   for p in scope.private] + [f"{repo}/.env*"]
        return [
            # No shell or web. No grep either: its read rules are checked against the folder
            # searched, not each file it matches, so it would reach gitignored files.
            f"--available-tools=view,glob,edit,create,{CHECK_SERVER}-run_check",
            "--disable-builtin-mcps",
            "--disallow-temp-dir",  # the server's config lives there, among other things
            f"--allow-tool={CHECK_SERVER}(run_check)",
            f"--allow-tool=read({repo}/**)",
            f"--allow-tool=write({repo}/**)",
            *(f"--deny-tool={kind}({path})" for path in private for kind in ("read", "write")),
            f"--deny-tool=write({repo}/.git/**)",
            f"--deny-tool=write({repo}/.reviewloop/**)",
        ]

    def _run(self, prompt: str, schema: dict, flags: list[str]) -> dict:
        with tempfile.TemporaryDirectory(prefix="reviewloop-copilot-") as tmp:
            usage_file = Path(tmp) / "usage.json"
            cmd = [
                "copilot",
                "--silent",  # only the agent's reply, no stats
                "--no-ask-user",
                # AGENTS.md and similar files can be part of the change under review,
                # so don't load them. Project rules arrive in the prompt instead.
                "--no-custom-instructions",
                *flags,
                "--usage-output-file", str(usage_file),
                *(["--model", self.model] if self.model else []),
                *(["--reasoning-effort", self.effort] if self.effort else []),
            ]
            # With no --prompt, copilot reads the prompt from stdin, which has no size limit.
            proc = self._exec(cmd, prompt)
            self._add_usage(usage_file)
        if proc.returncode != 0:
            raise self._failed(proc)
        return validate(extract_json(proc.stdout), schema)

    def _add_usage(self, usage_file: Path) -> None:
        try:
            report = json.loads(usage_file.read_text())
        except (OSError, ValueError):
            report = {}
        report = report if isinstance(report, dict) else {}
        details = report.get("tokenDetails") or {}
        count = lambda kind: (details.get(kind) or {}).get("tokenCount", 0)  # noqa: E731
        # Billed in AI credits, reported in billionths of one; 1 AI credit = $0.01.
        # Older premium-request billing reports no credits, so the cost is unknown.
        nano = report.get("totalNanoAiu")
        cost = nano / 1e9 * COPILOT_USD_PER_AI_CREDIT if isinstance(nano, (int, float)) else None
        self.usage.add(count("input") + count("cache_read") + count("cache_write"), count("output"), cost)


@dataclass
class CommandLLM(_HarnessLLM):
    """Any command: the prompt on stdin, a JSON object on stdout.

    `{model}` and `{effort}` in the command are replaced per call, so
    --model and --reviewer-model work when the command uses them.
    """

    command: str = ""
    name = "command"

    def __post_init__(self) -> None:
        try:
            self.argv = shlex.split(self.command)
        except ValueError as e:
            raise LLMError(f"--agent-cmd can't be parsed: {e}") from e
        if not self.argv:
            raise LLMError("--backend command needs --agent-cmd, e.g. --agent-cmd 'opencode run'")
        if self.model and not any("{model}" in a for a in self.argv):
            raise LLMError(f"--agent-cmd has no {{model}} placeholder, so it can't use model {self.model!r}")
        if not shutil.which(self.argv[0]) and not Path(self.argv[0]).is_file():
            raise LLMError(f"--agent-cmd: {self.argv[0]!r} not found on PATH")

    def structured(self, system: str, user: str, schema: dict, tools: list[str] | None = None) -> dict:
        """`tools` is ignored; whatever the command can do is up to the command."""
        values = {"model": self.model or "", "effort": self.effort}
        cmd = [a.replace("{model}", values["model"]).replace("{effort}", values["effort"]) for a in self.argv]
        proc = self._exec(cmd, _harness_prompt(system, user, schema))
        self.usage.add(0, 0)
        if proc.returncode != 0:
            raise self._failed(proc)
        return validate(extract_json(proc.stdout), schema)
