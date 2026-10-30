"""Model backends: one call in, one schema-validated JSON object out.

Two interchangeable backends with the same `structured()` method:
- ClaudeCodeLLM shells out to `claude -p` and uses your Claude Code login.
- ApiLLM calls the Anthropic API directly and needs ANTHROPIC_API_KEY.
"""

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MODEL = "claude-opus-5-5"
CLAUDE_CODE_TIMEOUT = 30 * 60


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
        cmd = [
            "claude", "-p",
            "--output-format", "json",
            "--json-schema", json.dumps(schema),
            "--system-prompt", system,
            "--model", self.model,
            "--effort", self.effort,
            "--tools", ",".join(tools or []),
            "--no-session-persistence",
        ]
        try:
            proc = subprocess.run(
                cmd, input=user, capture_output=True, text=True,
                cwd=self.cwd, timeout=CLAUDE_CODE_TIMEOUT,
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
