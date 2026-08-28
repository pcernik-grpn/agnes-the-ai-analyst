"""YAML run-config for `run_eval.py`.

Example (see `scripts/eval/example_run_config.yaml` for a fuller,
commented copy):

```yaml
round: R1
arms: [A0, A4]
runs_per_prompt: 3          # optional, default 3 -- workbook protocol #3
output_dir: runs            # optional, default "runs"
arm_config:
  A0:
    model: claude-sonnet-5
    api_key_env: ANTHROPIC_API_KEY
  A4:
    surface: chat            # chat | slack
    base_url: https://agnes.example.com
    agent_slug: eval-agent
    timeout_s: 120
    personas: [principal, associate]
    persona_tokens:
      principal: AGNES_EVAL_TOKEN_PRINCIPAL
      associate: AGNES_EVAL_TOKEN_ASSOCIATE
```

No credential VALUE is ever stored in the config -- only the env var name
that holds it (`api_key_env`, `persona_tokens`), so a committed run-config
carries no secret.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from scripts.eval.prompts import RUNS_PER_PROMPT


class RunConfigError(ValueError):
    """The run-config is missing a required key or has an invalid value."""


@dataclass
class RunConfig:
    round: str
    arms: list[str]
    runs_per_prompt: int
    output_dir: Path
    arm_config: dict[str, dict[str, Any]]
    prompt_ids: list[str] | None = None  # None -> all ten

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, base_dir: Path | None = None) -> "RunConfig":
        if "round" not in data:
            raise RunConfigError("run-config missing required key: round")
        if "arms" not in data or not data["arms"]:
            raise RunConfigError("run-config missing required key: arms (non-empty list)")
        output_dir = Path(data.get("output_dir", "runs"))
        if base_dir is not None and not output_dir.is_absolute():
            output_dir = base_dir / output_dir
        return cls(
            round=data["round"],
            arms=list(data["arms"]),
            runs_per_prompt=int(data.get("runs_per_prompt", RUNS_PER_PROMPT)),
            output_dir=output_dir,
            arm_config=dict(data.get("arm_config", {})),
            prompt_ids=list(data["prompt_ids"]) if data.get("prompt_ids") else None,
        )


def load_run_config(path: Path) -> RunConfig:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return RunConfig.from_dict(data, base_dir=path.resolve().parent)
