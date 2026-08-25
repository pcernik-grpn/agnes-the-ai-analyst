"""Guard: dev-agent-kit agents/commands are well-formed and cross-consistent.

Agents/commands are prose, but their frontmatter and cross-references are
structural — a renamed agent or a command pointing at a missing agent is a real
bug this catches.
"""

from __future__ import annotations

import pathlib
import re
import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / ".claude" / "agents"
COMMANDS_DIR = REPO_ROOT / ".claude" / "commands"


def read_frontmatter(path: pathlib.Path) -> dict[str, str]:
    """Return single-line `key: value` pairs from the leading --- block.

    Multi-line YAML values (e.g. `description: >`) record the key with an empty
    value — presence is what we assert, not the full value.
    """
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    block = text[3:end]
    out: dict[str, str] = {}
    for line in block.splitlines():
        if line[:1].isspace() or ":" not in line:
            continue  # nested/continuation line
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


def agent_names() -> set[str]:
    return {p.stem for p in AGENTS_DIR.glob("*.md")}


def test_parity_reviewer_has_valid_frontmatter():
    path = AGENTS_DIR / "agnes-reviewer-parity.md"
    assert path.exists(), "agnes-reviewer-parity.md must exist"
    fm = read_frontmatter(path)
    assert fm.get("name") == "agnes-reviewer-parity", "name must match filename"
    assert "description" in fm, "agent must declare a description"
    assert "tools" in fm, "agent must declare its tools"


def test_consolidator_has_valid_frontmatter():
    path = AGENTS_DIR / "agnes-review-consolidator.md"
    assert path.exists(), "agnes-review-consolidator.md must exist"
    fm = read_frontmatter(path)
    assert fm.get("name") == "agnes-review-consolidator", "name must match filename"
    assert "description" in fm, "agent must declare a description"
    assert "tools" in fm, "agent must declare its tools"


def test_agnes_review_command_references_only_existing_agents():
    path = COMMANDS_DIR / "agnes-review.md"
    assert path.exists(), "agnes-review.md command must exist"
    fm = read_frontmatter(path)
    assert "description" in fm, "command must declare a description"
    assert "allowed-tools" in fm, "command must declare allowed-tools"

    text = path.read_text(encoding="utf-8")
    # Every agnes-review* agent token in the command must be a real agent file.
    # Discard the bare command/team name "agnes-review" (not an agent).
    referenced = set(re.findall(r"agnes-review[\w-]*", text))
    referenced.discard("agnes-review")
    known = agent_names()
    unknown = sorted(referenced - known)
    assert not unknown, f"command references unknown agents: {unknown}"


def test_required_reviewers_present_for_command():
    # The command's roster must all exist as agents.
    roster = {
        "agnes-reviewer-rules",
        "agnes-reviewer-adversarial",
        "agnes-reviewer-architecture",
        "agnes-reviewer-rbac",
        "agnes-reviewer-parity",
        "agnes-review-consolidator",
    }
    missing = sorted(roster - agent_names())
    assert not missing, f"missing roster agents: {missing}"


@pytest.mark.parametrize(
    "agent",
    [
        "agnes-reviewer-rules",
        "agnes-reviewer-adversarial",
        "agnes-reviewer-architecture",
        "agnes-reviewer-rbac",
        "agnes-reviewer-parity",
    ],
)
def test_reviewers_reference_sync_map(agent):
    text = (AGENTS_DIR / f"{agent}.md").read_text(encoding="utf-8")
    assert "CONTRIBUTING.md" in text, f"{agent} must point at the CONTRIBUTING.md sync-map"


SKILLS_DIR = REPO_ROOT / ".claude" / "skills"
CONVENTIONS = SKILLS_DIR / "agnes-conventions"
PLAYBOOKS = ["connector", "repo-parity", "migration", "endpoint-rbac", "web-page"]


def test_builder_agent_has_valid_frontmatter():
    path = AGENTS_DIR / "agnes-builder.md"
    assert path.exists(), "agnes-builder.md must exist"
    fm = read_frontmatter(path)
    assert fm.get("name") == "agnes-builder", "name must match filename"
    assert "description" in fm, "agent must declare a description"
    assert "tools" in fm, "agent must declare its tools"


def test_builder_references_conventions_and_sync_map():
    text = (AGENTS_DIR / "agnes-builder.md").read_text(encoding="utf-8")
    assert "agnes-conventions" in text, "builder must route to agnes-conventions"
    assert "CONTRIBUTING.md" in text, "builder must point at the sync-map"


def test_conventions_skill_exists_and_lists_playbooks():
    skill = CONVENTIONS / "SKILL.md"
    assert skill.exists(), "agnes-conventions/SKILL.md must exist"
    fm = read_frontmatter(skill)
    assert fm.get("name") == "agnes-conventions", "skill name must match dir"
    text = skill.read_text(encoding="utf-8")
    for pb in PLAYBOOKS:
        assert f"references/{pb}.md" in text, f"SKILL.md must list references/{pb}.md"


def test_wayfinder_skill_exists_and_hands_off():
    skill = SKILLS_DIR / "agnes-wayfinder" / "SKILL.md"
    assert skill.exists(), "agnes-wayfinder/SKILL.md must exist"
    fm = read_frontmatter(skill)
    assert fm.get("name") == "agnes-wayfinder", "skill name must match dir"
    assert "description" in fm, "skill must declare a description"
    text = skill.read_text(encoding="utf-8")
    # The map is a pre-plan artifact: it must name where it lives and where it
    # hands off, or it becomes a parallel build process competing with the kit.
    assert "docs/superpowers/maps/" in text, "wayfinder must name the map location"
    assert "superpowers:writing-plans" in text, "wayfinder must hand off to writing-plans"
    assert "/agnes-build" in text, "wayfinder must hand off to /agnes-build"


@pytest.mark.parametrize("pb", ["connector", "repo-parity", "migration"])
def test_backend_playbooks_exist(pb):
    path = CONVENTIONS / "references" / f"{pb}.md"
    assert path.exists(), f"references/{pb}.md must exist"
    assert path.read_text(encoding="utf-8").strip(), f"{pb}.md must not be empty"


@pytest.mark.parametrize("pb", ["endpoint-rbac", "web-page"])
def test_app_playbooks_exist(pb):
    path = CONVENTIONS / "references" / f"{pb}.md"
    assert path.exists(), f"references/{pb}.md must exist"
    assert path.read_text(encoding="utf-8").strip(), f"{pb}.md must not be empty"


def test_claude_md_router_lists_kit_components():
    text = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    required = [
        "/agnes-review",
        "agnes-builder",
        "agnes-conventions",
        "agnes-reviewer-parity",
        "agnes-review-consolidator",
        "CONTRIBUTING.md",
        "post-edit-quality.sh",
    ]
    missing = [t for t in required if t not in text]
    assert not missing, f"CLAUDE.md router must mention kit components: {missing}"


@pytest.mark.parametrize("agent", ["agnes-decomposer", "agnes-integrator"])
def test_buildteam_agents_have_valid_frontmatter(agent):
    path = AGENTS_DIR / f"{agent}.md"
    assert path.exists(), f"{agent}.md must exist"
    fm = read_frontmatter(path)
    assert fm.get("name") == agent, "name must match filename"
    assert "description" in fm, "agent must declare a description"
    assert "tools" in fm, "agent must declare its tools"


def test_decomposer_uses_sync_map_coupling():
    text = (AGENTS_DIR / "agnes-decomposer.md").read_text(encoding="utf-8")
    assert "CONTRIBUTING.md" in text, "decomposer must read the sync-map"
    assert "parity" in text.lower() and "migration" in text.lower(), (
        "decomposer must keep parity siblings + migration steps coupled"
    )


def test_integrator_serializes_migrations():
    text = (AGENTS_DIR / "agnes-integrator.md").read_text(encoding="utf-8")
    assert "migration" in text.lower(), "integrator must handle the migration task"
    assert "worktree" in text.lower(), "integrator must collect worktree diffs"


def test_agnes_build_command_references_only_existing_agents():
    path = COMMANDS_DIR / "agnes-build.md"
    assert path.exists(), "agnes-build.md command must exist"
    fm = read_frontmatter(path)
    assert "description" in fm, "command must declare a description"
    assert "allowed-tools" in fm, "command must declare allowed-tools"
    text = path.read_text(encoding="utf-8")
    referenced = set(re.findall(r"agnes-[\w-]+", text))
    # command + skill names that are not agents:
    referenced -= {"agnes-build", "agnes-review", "agnes-conventions"}
    unknown = sorted(referenced - agent_names())
    assert not unknown, f"agnes-build references unknown agents: {unknown}"
