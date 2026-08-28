"""Run P ablation driver (design spec §15.5) — spec-side diagnostic.

Drives the planted-corpus question set through the Agnes agent surface,
once per persona, and is invoked twice by the operator: once with the
`facts` flag ON and once with it OFF (Collections retrieval only) — the
within-platform control that isolates what the fact layer adds. Every
answer lands as one machine-readable JSON record.

Usage:
    python scripts/eval/run_p_ablation.py \
        --questions data/eval/planted_questions.yaml \
        --base-url https://<instance> \
        --label facts-on \
        --agent-map principal=eval-facts-principal,associate=eval-facts-associate \
        --out data/eval/runs/RUNP

Persona tokens come from env: AGNES_EVAL_TOKEN_<PERSONA>.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.eval.arms import ChatSurface  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", required=True)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--label", required=True, help="facts-on | facts-off")
    ap.add_argument("--agent-map", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--timeout-s", type=float, default=180.0)
    args = ap.parse_args()

    agent_map = dict(pair.split("=", 1) for pair in args.agent_map.split(","))
    questions = yaml.safe_load(Path(args.questions).read_text())["questions"]
    out_dir = Path(args.out) / args.label
    out_dir.mkdir(parents=True, exist_ok=True)

    for persona, slug in agent_map.items():
        token = os.environ.get(f"AGNES_EVAL_TOKEN_{persona.upper()}")
        if not token:
            raise SystemExit(f"missing env AGNES_EVAL_TOKEN_{persona.upper()}")
        surface = ChatSurface(args.base_url, slug)
        for q in questions:
            rec_path = out_dir / f"{q['id']}_{persona}.json"
            if rec_path.exists():
                print(f"[ablation] skip existing {rec_path.name}")
                continue
            started = time.time()
            try:
                result = surface.ask(q["prompt"], token=token, timeout_s=args.timeout_s)
                error = None
            except Exception as exc:  # noqa: BLE001 — recorded, run continues
                result, error = None, f"{type(exc).__name__}: {exc}"
            record = {
                "run": "RUNP-ablation",
                "label": args.label,
                "question_id": q["id"],
                "shape": q.get("shape"),
                "persona": persona,
                "agent_slug": slug,
                "prompt": q["prompt"],
                "expected": q.get("expected"),
                "traps": q.get("traps"),
                "result": result,
                "error": error,
                "elapsed_s": round(time.time() - started, 1),
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            rec_path.write_text(json.dumps(record, indent=1, default=str))
            status = "ok" if error is None else f"ERROR {error[:80]}"
            print(f"[ablation] {args.label} {q['id']} {persona}: {status} ({record['elapsed_s']}s)")


if __name__ == "__main__":
    main()
