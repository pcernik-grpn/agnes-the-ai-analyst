#!/usr/bin/env python3
"""Drive one eval round from a YAML run-config, or ingest an operator-pasted
manual transcript (A1/A2/A3). Fact-graph build-order step 5 -- design spec
Sec 16, EQ0 (Sec 15.3).

Usage:
    .venv/bin/python -m scripts.eval.run_eval run --config path/to/round.yaml
    .venv/bin/python -m scripts.eval.run_eval run --config round.yaml --arm A0 --prompt X1
    .venv/bin/python -m scripts.eval.run_eval import-transcript \\
        --round R0 --arm A1 --prompt X1 --run 1 --persona principal \\
        --transcript-file transcript.txt --input-tokens 1200 --output-tokens 340

Every (arm, prompt, run#) triple runs SEQUENTIALLY and exactly
`runs_per_prompt` times (default 3) -- variance is a result, never
deduped/cached (Framework!B30). Every run writes its own record before the
next one starts, so a crash mid-round loses at most the in-flight run.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.eval.arms import (  # noqa: E402
    AgentSurface,
    AgnesArm,
    AnthropicArm,
    ArmExecutor,
    ChatSurface,
    SlackSurface,
    manual_transcript_record,
)
from scripts.eval.config import RunConfig, load_run_config  # noqa: E402
from scripts.eval.prompts import API_DRIVEN_ARMS, MANUAL_ARMS, load_prompts  # noqa: E402
from scripts.eval.records import TokenCounts, Turn, write_record  # noqa: E402

_SURFACES: dict[str, type[AgentSurface]] = {"chat": ChatSurface, "slack": SlackSurface}


def build_executor(arm: str, arm_config: dict) -> ArmExecutor:
    if arm == "A0":
        return AnthropicArm(**arm_config)
    if arm == "A4":
        cfg = dict(arm_config)
        surface_name = cfg.pop("surface", "chat")
        surface_cls = _SURFACES.get(surface_name)
        if surface_cls is None:
            raise ValueError(f"unknown A4 surface {surface_name!r} -- choose one of {sorted(_SURFACES)}")
        base_url = cfg.pop("base_url")
        agent_slug = cfg.pop("agent_slug")
        persona_tokens = cfg.pop("persona_tokens", {})
        cfg.pop("personas", None)  # consumed by run_round, not the executor
        timeout_s = cfg.pop("timeout_s", 120.0)
        surface = surface_cls(base_url, agent_slug)
        return AgnesArm(surface, persona_tokens=persona_tokens, timeout_s=timeout_s)
    raise ValueError(
        f"arm {arm!r} has no API executor -- A1/A2/A3 are manual-transcript "
        f"import only, see `import-transcript` (arms.py module docstring)"
    )


def run_round(config: RunConfig, *, only_arm: str | None = None, only_prompt: str | None = None) -> list[Path]:
    prompts = load_prompts()
    if config.prompt_ids:
        prompts = [p for p in prompts if p.id in config.prompt_ids]
    if only_prompt:
        prompts = [p for p in prompts if p.id == only_prompt]

    written: list[Path] = []
    for arm in config.arms:
        if only_arm and arm != only_arm:
            continue
        if arm in MANUAL_ARMS:
            print(
                f"[run_eval] skipping {arm}: manual-transcript import only -- use `import-transcript` for this arm",
                file=sys.stderr,
            )
            continue
        if arm not in API_DRIVEN_ARMS:
            print(f"[run_eval] skipping unknown arm {arm!r}", file=sys.stderr)
            continue
        executor = build_executor(arm, config.arm_config.get(arm, {}))
        personas = config.arm_config.get(arm, {}).get("personas") or [None]
        for prompt in prompts:
            for persona in personas:
                for run_index in range(1, config.runs_per_prompt + 1):
                    print(f"[run_eval] {config.round} {arm} {prompt.id} persona={persona} run#{run_index}")
                    record = executor.run(prompt, persona=persona, run_index=run_index, round_id=config.round)
                    path = write_record(config.output_dir, record)
                    written.append(path)
                    if record.errors:
                        print(f"[run_eval]   errors: {record.errors}", file=sys.stderr)
    return written


def _cmd_run(args: argparse.Namespace) -> int:
    config = load_run_config(Path(args.config))
    written = run_round(config, only_arm=args.arm, only_prompt=args.prompt)
    print(f"[run_eval] wrote {len(written)} record(s) under {config.output_dir}")
    return 0


def _cmd_import_transcript(args: argparse.Namespace) -> int:
    prompts = {p.id: p for p in load_prompts()}
    prompt = prompts.get(args.prompt)
    if prompt is None:
        print(f"error: unknown prompt id {args.prompt!r}, expected one of {sorted(prompts)}", file=sys.stderr)
        return 2
    if args.arm not in ("A1", "A2", "A3"):
        print(f"error: import-transcript is for manual arms A1/A2/A3, got {args.arm!r}", file=sys.stderr)
        return 2

    transcript_text = Path(args.transcript_file).read_text(encoding="utf-8")
    transcript = [Turn(role="user", content=prompt.text), Turn(role="assistant", content=transcript_text)]
    tokens = TokenCounts(
        input_tokens=args.input_tokens,
        output_tokens=args.output_tokens,
        cache_read_tokens=args.cache_read_tokens,
        cache_creation_tokens=args.cache_creation_tokens,
    )
    record = manual_transcript_record(
        round_id=args.round,
        arm=args.arm,
        prompt=prompt,
        run_index=args.run,
        persona=args.persona,
        transcript=transcript,
        tokens=tokens,
        started_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        notes=args.notes,
    )
    path = write_record(Path(args.output_dir), record)
    print(f"[run_eval] wrote {path}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Execute an eval round from a YAML run-config")
    run_p.add_argument("--config", required=True, help="Path to the run-config YAML")
    run_p.add_argument("--arm", default=None, help="Restrict to one arm (default: all arms in the config)")
    run_p.add_argument("--prompt", default=None, help="Restrict to one prompt id (default: all)")
    run_p.set_defaults(func=_cmd_run)

    imp_p = sub.add_parser(
        "import-transcript",
        help="Ingest an operator-pasted manual transcript for A1/A2/A3",
    )
    imp_p.add_argument("--round", required=True)
    imp_p.add_argument("--arm", required=True, choices=("A1", "A2", "A3"))
    imp_p.add_argument("--prompt", required=True, help="Prompt id, e.g. X1")
    imp_p.add_argument("--run", required=True, type=int, help="Run index (1..N)")
    imp_p.add_argument("--persona", default=None)
    imp_p.add_argument("--transcript-file", required=True, help="Path to the pasted transcript text")
    imp_p.add_argument("--input-tokens", type=int, default=None)
    imp_p.add_argument("--output-tokens", type=int, default=None)
    imp_p.add_argument("--cache-read-tokens", type=int, default=None)
    imp_p.add_argument("--cache-creation-tokens", type=int, default=None)
    imp_p.add_argument("--output-dir", default="runs")
    imp_p.add_argument("--notes", default=None)
    imp_p.set_defaults(func=_cmd_import_transcript)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
