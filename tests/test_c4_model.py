"""The C4 model stays sound, and the rendered figures stay tied to it.

`docs/c4/` is the source; `docs/diagrams/agnes-c4-*.svg` is its output, produced
by `scripts/dev/render_c4.sh`. Two things can rot here and neither is visible in
a diff: the model can grow a dangling reference, and a view can be renamed or
added without the figure being re-rendered or embedded. Both are covered.

`scripts/validate_c4.py` is a lint, not the Structurizr compiler — the compiler
runs in render_c4.sh, which needs a container runtime CI does not have.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
C4 = REPO / "docs" / "c4"
DIAGRAMS = REPO / "docs" / "diagrams"

VIEW_RE = re.compile(r'^\s*(?:systemContext|container|component|dynamic)\s+\S+\s+"([^"]+)"', re.M)


def _view_keys() -> list[str]:
    keys = VIEW_RE.findall((C4 / "views" / "views.dsl").read_text(encoding="utf-8"))
    assert keys, "no views parsed from views.dsl — did the DSL syntax change?"
    return keys


def test_model_validates():
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "validate_c4.py"), str(C4 / "workspace.dsl")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"validate_c4.py failed:\n{result.stdout}{result.stderr}"


def test_validator_catches_a_dangling_reference(tmp_path):
    """A gate nobody has watched fail is not known to be a gate."""
    broken = tmp_path / "workspace.dsl"
    broken.write_text(
        'workspace "t" "t" {\n'
        "    model {\n"
        '        a = softwareSystem "A" "a"\n'
        '        a -> ghost "goes nowhere"\n'
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "validate_c4.py"), str(broken)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "ghost" in result.stdout


def test_every_view_has_a_rendered_figure():
    """A view key IS its filename — render_c4.sh strips the structurizr- prefix."""
    missing = [k for k in _view_keys() if not (DIAGRAMS / f"{k}.svg").is_file()]
    assert not missing, f"views with no rendered figure: {missing}. Run scripts/dev/render_c4.sh."


def test_every_rendered_figure_is_embedded_in_architecture_md():
    doc = (REPO / "ARCHITECTURE.md").read_text(encoding="utf-8")
    missing = [k for k in _view_keys() if f"docs/diagrams/{k}.svg" not in doc]
    assert not missing, f"figures rendered but never embedded in ARCHITECTURE.md: {missing}"


def test_c4_figures_are_not_hand_authored():
    """The generator owns the three layered figures; the DSL owns the C4 five.

    Both wrote SVGs into the same directory once, and the failure mode was
    silent: a hand-drawn figure and a rendered one at the same path, with
    whichever ran last winning.
    """
    gen = (REPO / "scripts" / "dev" / "gen_architecture_diagrams.py").read_text(encoding="utf-8")
    assert "agnes-c4-" not in gen, (
        "gen_architecture_diagrams.py names a C4 figure — those are rendered "
        "from docs/c4/ by scripts/dev/render_c4.sh, not hand-laid-out here."
    )
