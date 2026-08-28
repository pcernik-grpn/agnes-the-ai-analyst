"""Every conversational builder says when it is talking to the stand-in.

`app/api/builder_core.py` reports which engine answered each turn precisely so
a scripted stand-in cannot pass for a real model — the failure that motivated
it was someone spending an afternoon judging a string-slicer on a local
instance, because the stub's response was shaped identically to a real turn and
nothing on screen said otherwise.

That guarantee is only as good as its weakest surface. When this guard was
written, two of the four builders rendered the notice and two took `engine` off
the wire and dropped it — so the badge existed and the product still could not
be trusted to admit which engine it was running. This pins all four, and pins
them to ONE implementation (`BuilderShell.engineNotice`) so a fifth builder
gets it by using the shell rather than by remembering.

The linked-apps builder is deliberately absent: it has no conversation, so
there is no turn and no engine to name.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "app" / "web" / "static" / "js" / "components" / "builder_shell.js"

#: label -> the file that renders that builder's conversation pane.
BUILDERS = {
    "skills/plugins/agent templates": ROOT / "app" / "web" / "templates" / "skills.html",
    "agents": ROOT / "app" / "web" / "templates" / "agents.html",
    "data packages": ROOT / "app" / "web" / "static" / "js" / "components" / "package_drawer.js",
    "MCP sources": ROOT / "app" / "web" / "static" / "js" / "components" / "mcp_builder.js",
}

TURN_ENDPOINTS = {
    "entity": ROOT / "app" / "api" / "entity_builder.py",
    "agent": ROOT / "app" / "api" / "agent_builder.py",
    "package": ROOT / "app" / "api" / "package_builder.py",
    "mcp": ROOT / "app" / "api" / "mcp_builder.py",
}


@pytest.fixture(scope="module")
def shell() -> str:
    return SHELL.read_text(encoding="utf-8")


def test_the_shell_owns_the_notice(shell: str) -> None:
    assert "function engineNotice(" in shell
    assert "engineNotice: engineNotice," in shell, "not exported, so no page can use it"
    assert "if (engine !== 'stub') return '';" in shell, (
        "the notice must render for the stub and for nothing else — a badge on "
        "a real turn is noise that trains people to ignore it"
    )
    assert "AGNES_BUILDER_STUB" in shell, "the notice has to name the flag that causes it"


@pytest.mark.parametrize("label", sorted(BUILDERS))
def test_each_builder_renders_the_shared_notice(label: str) -> None:
    src = BUILDERS[label].read_text(encoding="utf-8")
    assert "BuilderShell.engineNotice(" in src, (
        f"the {label} builder does not render the engine notice — it takes "
        "`engine` off the wire and drops it, which is how a stubbed builder "
        f"passes for a real one. Render BuilderShell.engineNotice() in its "
        f"conversation pane ({BUILDERS[label].name})."
    )


@pytest.mark.parametrize("label", sorted(BUILDERS))
def test_each_builder_records_the_engine_from_the_turn(label: str) -> None:
    """Rendering the notice is no use against a variable nothing assigns."""
    src = BUILDERS[label].read_text(encoding="utf-8")
    assert "body.engine" in src or "res.engine" in src or ".engine || null" in src, (
        f"the {label} builder never reads `engine` from a turn response, so its notice can only ever be hidden"
    )


@pytest.mark.parametrize("label", sorted(TURN_ENDPOINTS))
def test_each_turn_endpoint_reports_an_engine(label: str) -> None:
    """The server half. `turn_response` puts `engine` on the wire, so what this
    checks is that every adapter goes through it rather than assembling its own
    payload and quietly omitting the field."""
    src = TURN_ENDPOINTS[label].read_text(encoding="utf-8")
    assert "turn_response(" in src, (
        f"the {label} builder-turn endpoint does not use "
        "builder_core.turn_response, so nothing guarantees its response carries "
        "`engine`, `slots` or the filtered suggestions"
    )
    assert "ENGINE_STUB" in src and "ENGINE_MODEL" in src, f"the {label} endpoint does not name both engines"
