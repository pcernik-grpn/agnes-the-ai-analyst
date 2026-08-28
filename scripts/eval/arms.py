"""Arm executors -- one per workbook arm (Framework sheet, design spec Sec
14.1).

  A0  Claude, no connectors, no tools -- the frozen prompt string verbatim
      through the Anthropic Messages API. The hallucination floor.
  A4  Agnes -- run as a given persona (a bearer token per persona, read from
      an env var named in the run-config) through a PLUGGABLE surface. Two
      surfaces are named in the design: `chat` (implemented here, against
      the same one-shot request/response endpoint `agnes agent ask` uses --
      `POST /api/v1/agents/{slug}/responses`, polling `GET /api/v1/jobs/{id}`
      on a 202 -- see cli/commands/agent.py) and `slack` (the interface is
      defined below; not implemented yet -- needed later for the "Slack
      must answer identically" parity run, design spec Sec 13.2/15.5).
  A1/A2/A3  Claude+M365/SharePoint, ChatGPT+SharePoint, and Claude+M365+seed
      pack. These run in external product UIs (Claude.ai / ChatGPT
      connectors, or a browser-driven M365 connector session) this harness
      cannot drive headlessly -- there is no executor class for them. An
      operator runs the prompt there by hand and pastes the transcript back
      in via `run_eval.py import-transcript` (see `manual_transcript_record`
      below), which normalizes it into the exact same `RunRecord` shape an
      API-driven arm produces, so grading and token counting stay uniform
      across all five arms (workbook-compatible method).

Every executor runs SEQUENTIALLY, one call at a time -- three runs per
prompt per arm, never deduped or cached (Framework!B30: "variance is a
result, not noise").
"""

from __future__ import annotations

import abc
import os
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from scripts.eval.prompts import Prompt
from scripts.eval.records import RunRecord, TokenCounts, Turn

_ANTHROPIC_TIMEOUT_S = 120.0


class ArmExecutionError(RuntimeError):
    """A run could not be completed at all (transport failure, auth
    failure, exhausted poll budget, ...). The caller records this as an
    `errors` entry on a still-written `RunRecord` rather than losing the
    attempt -- "the run is the artifact" applies to failed runs too."""


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class ArmExecutor(abc.ABC):
    arm: str

    @abc.abstractmethod
    def run(
        self,
        prompt: Prompt,
        *,
        persona: str | None,
        run_index: int,
        round_id: str,
    ) -> RunRecord: ...


# ---------------------------------------------------------------------------
# A0 -- bare Anthropic API, no tools
# ---------------------------------------------------------------------------


class AnthropicArm(ArmExecutor):
    """A0: `claude-sonnet-5` (configurable) via the Anthropic Messages API,
    no tool schema, the prompt string verbatim as the sole user turn."""

    arm = "A0"

    def __init__(
        self,
        *,
        model: str = "claude-sonnet-5",
        api_key_env: str = "ANTHROPIC_API_KEY",
        max_tokens: int = 4096,
    ) -> None:
        self.model = model
        self.api_key_env = api_key_env
        self.max_tokens = max_tokens

    def run(self, prompt: Prompt, *, persona: str | None, run_index: int, round_id: str) -> RunRecord:
        started_at = _now_iso()
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            return RunRecord(
                round=round_id,
                arm=self.arm,
                prompt_id=prompt.id,
                run_index=run_index,
                persona=persona,
                source="api",
                transcript=[],
                started_at=started_at,
                completed_at=_now_iso(),
                tokens=TokenCounts(),
                turns=0,
                errors=[f"missing env var {self.api_key_env}"],
            )
        try:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key, timeout=_ANTHROPIC_TIMEOUT_S)
            response = client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                messages=[{"role": "user", "content": prompt.text}],
            )
        except Exception as exc:  # noqa: BLE001 -- any SDK/transport failure is a run failure, not a crash
            return RunRecord(
                round=round_id,
                arm=self.arm,
                prompt_id=prompt.id,
                run_index=run_index,
                persona=persona,
                source="api",
                transcript=[Turn(role="user", content=prompt.text, ts=started_at)],
                started_at=started_at,
                completed_at=_now_iso(),
                tokens=TokenCounts(),
                turns=1,
                errors=[f"{type(exc).__name__}: {exc}"],
            )
        answer = "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
        usage = response.usage
        return RunRecord(
            round=round_id,
            arm=self.arm,
            prompt_id=prompt.id,
            run_index=run_index,
            persona=persona,
            source="api",
            transcript=[
                Turn(role="user", content=prompt.text, ts=started_at),
                Turn(role="assistant", content=answer, ts=_now_iso()),
            ],
            started_at=started_at,
            completed_at=_now_iso(),
            tokens=TokenCounts(
                input_tokens=getattr(usage, "input_tokens", None),
                output_tokens=getattr(usage, "output_tokens", None),
                cache_read_tokens=getattr(usage, "cache_read_input_tokens", None),
                cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", None),
            ),
            turns=1,
            answer=answer,
            raw={"model": self.model, "stop_reason": getattr(response, "stop_reason", None)},
        )


# ---------------------------------------------------------------------------
# A4 -- Agnes, via a pluggable surface
# ---------------------------------------------------------------------------


class AgentSurface(abc.ABC):
    """The pluggable transport A4 runs over. `chat` is implemented; `slack`
    is reserved -- design spec Sec 13.2/15.5 requires the ten prompts to
    also run through Slack for the surface-parity check, but that is a
    later step (the parity check itself compares two already-recorded
    rounds, it doesn't need a new surface class until Slack driving is
    built)."""

    @abc.abstractmethod
    def ask(self, prompt_text: str, *, token: str, timeout_s: float) -> dict[str, Any]:
        """Return `{"answer": str, "raw": dict}` on success. Raise
        `ArmExecutionError` on any failure (never returns a partial/None
        answer silently)."""


class ChatSurface(AgentSurface):
    """One-shot request/response over `POST /api/v1/agents/{slug}/
    responses`, polling `GET /api/v1/jobs/{id}` on a `202` -- the same two
    endpoints `agnes agent ask` uses (cli/commands/agent.py). Implemented
    directly against `httpx` rather than importing the `cli` package: this
    harness has no other dependency on the CLI's Typer app, config
    resolution, or credential store (a persona's token comes from an env
    var named in the run-config, not `agnes login`)."""

    _POLL_INTERVAL_S = 2.0
    _TERMINAL_STATUSES = ("completed", "failed")

    def __init__(self, base_url: str, agent_slug: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.agent_slug = agent_slug
        #: Test-only hook -- `httpx.MockTransport` lets tests exercise the
        #: 200/202-poll logic without a live server. `None` (default) uses
        #: httpx's normal network transport.
        self._transport = transport

    def ask(self, prompt_text: str, *, token: str, timeout_s: float) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {token}"}
        with httpx.Client(
            base_url=self.base_url, headers=headers, timeout=timeout_s + 10.0, transport=self._transport
        ) as client:
            resp = client.post(
                f"/api/v1/agents/{self.agent_slug}/responses",
                json={"input": prompt_text, "timeout_s": timeout_s},
            )
            if resp.status_code == 200:
                body = resp.json()
                return {"answer": body.get("answer"), "raw": body}
            if resp.status_code == 202:
                job_id = resp.json()["job_id"]
                return self._poll_job(client, job_id, timeout_s)
            raise ArmExecutionError(f"POST /responses -> {resp.status_code}: {resp.text[:500]}")

    def _poll_job(self, client: httpx.Client, job_id: str, timeout_s: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        job: dict[str, Any] = {}
        while time.monotonic() < deadline:
            resp = client.get(f"/api/v1/jobs/{job_id}")
            if resp.status_code != 200:
                raise ArmExecutionError(f"GET /jobs/{job_id} -> {resp.status_code}: {resp.text[:500]}")
            job = resp.json()
            if job.get("status") in self._TERMINAL_STATUSES:
                break
            time.sleep(self._POLL_INTERVAL_S)
        if job.get("status") != "completed":
            raise ArmExecutionError(f"job {job_id} did not complete in {timeout_s:.0f}s (status={job.get('status')})")
        result = job.get("result") or {}
        return {"answer": result.get("answer"), "raw": job}


class SlackSurface(AgentSurface):
    """Reserved -- driving A4 through Slack instead of chat is needed for
    the surface-parity run (design spec Sec 13.2: "Slack must answer
    identically") but is not implemented in this build-order step."""

    def ask(self, prompt_text: str, *, token: str, timeout_s: float) -> dict[str, Any]:
        raise NotImplementedError("SlackSurface is not implemented yet -- use ChatSurface")


class AgnesArm(ArmExecutor):
    """A4: Agnes + seed pack + knowledge graph, run as a persona via a
    pluggable `AgentSurface`. `persona_tokens` maps a persona name (e.g.
    "principal", "associate") to the env var holding that persona's bearer
    token -- never the token value itself, so a run-config committed by
    accident carries no secret."""

    arm = "A4"

    def __init__(
        self,
        surface: AgentSurface,
        *,
        persona_tokens: dict[str, str],
        timeout_s: float = 120.0,
    ) -> None:
        self.surface = surface
        self.persona_tokens = persona_tokens
        self.timeout_s = timeout_s

    def run(self, prompt: Prompt, *, persona: str | None, run_index: int, round_id: str) -> RunRecord:
        started_at = _now_iso()
        if persona is None:
            errors = ["A4 requires a persona (principal/associate) -- see run-config `personas`"]
            token_env = None
        else:
            token_env = self.persona_tokens.get(persona)
            errors = [f"no persona_tokens entry for persona {persona!r}"] if token_env is None else []
        token = os.environ.get(token_env, "") if token_env else ""
        if not errors and not token:
            errors = [f"missing env var {token_env} for persona {persona!r}"]
        if errors:
            return RunRecord(
                round=round_id,
                arm=self.arm,
                prompt_id=prompt.id,
                run_index=run_index,
                persona=persona,
                source="api",
                transcript=[],
                started_at=started_at,
                completed_at=_now_iso(),
                tokens=TokenCounts(),
                turns=0,
                errors=errors,
            )
        try:
            result = self.surface.ask(prompt.text, token=token, timeout_s=self.timeout_s)
        except ArmExecutionError as exc:
            return RunRecord(
                round=round_id,
                arm=self.arm,
                prompt_id=prompt.id,
                run_index=run_index,
                persona=persona,
                source="api",
                transcript=[Turn(role="user", content=prompt.text, ts=started_at)],
                started_at=started_at,
                completed_at=_now_iso(),
                tokens=TokenCounts(),
                turns=1,
                errors=[str(exc)],
            )
        answer = result.get("answer")
        return RunRecord(
            round=round_id,
            arm=self.arm,
            prompt_id=prompt.id,
            run_index=run_index,
            persona=persona,
            source="api",
            transcript=[
                Turn(role="user", content=prompt.text, ts=started_at),
                Turn(role="assistant", content=answer or "", ts=_now_iso()),
            ],
            started_at=started_at,
            completed_at=_now_iso(),
            tokens=TokenCounts(),  # Agnes token visibility per README!C24 -- filled by the caller/OTel export, not this HTTP client
            turns=1,
            answer=answer,
            raw=result.get("raw"),
        )


# ---------------------------------------------------------------------------
# A1/A2/A3 -- manual-transcript import
# ---------------------------------------------------------------------------


def manual_transcript_record(
    *,
    round_id: str,
    arm: str,
    prompt: Prompt,
    run_index: int,
    persona: str | None,
    transcript: list[Turn],
    tokens: TokenCounts,
    started_at: str,
    completed_at: str | None = None,
    notes: str | None = None,
) -> RunRecord:
    """Build a `RunRecord` for an operator-pasted transcript (A1/A2/A3).
    `source="manual"` so grading/reporting can distinguish an
    API-automated run from a human-supplied one without inspecting the
    transcript. Token counts are supplied by the operator, following the
    per-arm method in `tests/fixtures/eval/workbook_v0_2/token_methods.md`
    -- this function does not (and cannot) count them itself."""
    answer = next((t.content for t in reversed(transcript) if t.role == "assistant"), None)
    return RunRecord(
        round=round_id,
        arm=arm,
        prompt_id=prompt.id,
        run_index=run_index,
        persona=persona,
        source="manual",
        transcript=transcript,
        started_at=started_at,
        completed_at=completed_at or _now_iso(),
        tokens=tokens,
        turns=len(transcript),
        answer=answer,
        notes=notes,
    )
