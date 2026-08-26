#!/usr/bin/env python3
"""Render the install prompt (`app.web.setup_instructions.resolve_lines`) to
a file, for manual inspection and for `check_prompt.py` to scan.

Run from the repo root with the project venv:

    .venv/Scripts/python.exe scripts/dev/render_prompt.py
    .venv/Scripts/python.exe scripts/dev/render_prompt.py --ca --out /tmp/ca.txt

Flags:
    --ca            render with a fake CA cert so the TLS trust block (step 0)
                    is included.
    --iwt DIR       resolve seed content from a local directory standing in
                    for an Initial Workspace Template clone, by monkeypatching
                    `src.initial_workspace.get_initial_workspace_dir` /
                    `is_configured` for the duration of the render (there is
                    no env-var hook for this today). The thin prompt inlines
                    nothing from the seed, so this changes the output only if
                    a step is ever re-sourced from it.
    --out PATH      write to PATH instead of stdout.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

FAKE_CA_PEM = "-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ca", action="store_true", help="include the TLS trust block")
    parser.add_argument("--iwt", metavar="DIR", default=None, help="directory standing in for an IWT clone")
    parser.add_argument("--out", metavar="PATH", default=None, help="write output here instead of stdout")
    args = parser.parse_args()

    from app.web.setup_instructions import resolve_lines

    ca_pem = FAKE_CA_PEM if args.ca else None

    def _render() -> str:
        lines = resolve_lines(
            "agnes_the_ai_analyst-0.0.0-py3-none-any.whl",
            server_host="example.com",
            ca_pem=ca_pem,
        )
        return "\n".join(lines)

    if args.iwt:
        iwt_dir = Path(args.iwt).expanduser().resolve()
        if not iwt_dir.is_dir():
            print(f"error: --iwt directory not found: {iwt_dir}", file=sys.stderr)
            return 1
        import src.initial_workspace as iw

        with (
            patch.object(iw, "get_initial_workspace_dir", return_value=iwt_dir),
            patch.object(iw, "is_configured", return_value=True),
        ):
            text = _render()
    else:
        text = _render()

    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
