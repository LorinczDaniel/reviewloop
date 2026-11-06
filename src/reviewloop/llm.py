"""Model backends: one call in, one schema-validated JSON object out.

Interchangeable backends with the same `structured()` method:
- ClaudeCodeLLM shells out to `claude -p` and uses your Claude Code login.
- ApiLLM calls the Anthropic API directly and needs ANTHROPIC_API_KEY.
- CodexLLM shells out to `codex exec` (OpenAI Codex CLI) and uses its login.
- GeminiLLM shells out to `gemini -p` (Gemini CLI) and uses its login.
- CopilotLLM shells out to `copilot -p` (GitHub Copilot CLI) and uses its login.
- CommandLLM runs any command you give it: the prompt goes in on stdin,
  and a JSON object is expected back on stdout.

Only ClaudeCodeLLM can run the agentic check fixer, because it's the only
harness whose tool permissions can be limited to exact commands.
"""

import json
import os
import shlex
import shutil
import subprocess
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

    def add(self, input_tokens: int, output_tokens: int, cost_usd: float = 0.0) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cost_usd += cost_usd
        self.calls += 1


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

    def agent(self, system: str, user: str, schema: dict, allowed: list[str], denied: list[str]) -> dict:
        """Run a session that can use tools, limited to the `allowed` permission rules.

        Anything else is refused rather than prompted for. Rules are passed one
        argument each, because a check command can contain spaces or commas.
        """
        tools = ["Read", "Grep", "Glob", "Edit", "Write", "Bash"]
        return self._run(system, user, schema, tools, [
            "--permission-mode", "dontAsk", "--allowedTools", *allowed, "--disallowedTools", *denied,
        ], {**os.environ, "BASH_DEFAULT_TIMEOUT_MS": BASH_TIMEOUT_MS, "BASH_MAX_TIMEOUT_MS": BASH_TIMEOUT_MS})

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

    def agent(self, system: str, user: str, schema: dict, allowed: list[str], denied: list[str]) -> dict:
        raise LLMError(f"the {self.name} backend can't run the agentic check fixer; use --backend claude-code")

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


# Linux caps a single command-line argument at 128 KiB, and Copilot CLI takes the
# prompt as an argument rather than on stdin.
COPILOT_MAX_PROMPT_BYTES = 120_000


@dataclass
class CopilotLLM(_HarnessLLM):
    """`copilot -p` with only its read tool available; the JSON reply is checked here."""

    name = "copilot"

    def __post_init__(self) -> None:
        if not shutil.which("copilot"):
            raise LLMError("`copilot` CLI not found on PATH; install GitHub Copilot CLI or pick another --backend")

    def structured(self, system: str, user: str, schema: dict, tools: list[str] | None = None) -> dict:
        """`tools` is ignored: Copilot can always read the repo, but nothing else. `effort` has no flag."""
        prompt = _harness_prompt(system, user, schema)
        size = len(prompt.encode())
        if size > COPILOT_MAX_PROMPT_BYTES:
            raise LLMError(f"prompt is {size:,} bytes, over the {COPILOT_MAX_PROMPT_BYTES:,} copilot can take as an "
                           "argument; review fewer files at once or pick another --backend")
        cmd = [
            "copilot",
            "--prompt", prompt,
            "--silent",  # only the agent's reply, no stats
            "--no-ask-user",
            "--available-tools=read",
            "--allow-tool=read",
            *(["--model", self.model] if self.model else []),
        ]
        proc = self._exec(cmd, "")
        self.usage.add(0, 0)  # copilot doesn't report tokens in this mode
        if proc.returncode != 0:
            raise self._failed(proc)
        return validate(extract_json(proc.stdout), schema)


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
