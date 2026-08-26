"""Starting an agent from a Library Agent Template (AGT-5).

The relationship between the two entities was invisible from the creation
flow: a template is what you build an agent OUT OF, and the only way to act on
that was to open the template, copy its prompt, and paste it into a blank
agent. ``POST /api/v1/agents`` now takes ``template_entity_id`` (originally
added to the builder's own ``/api/agents`` router, folded in by Task C1.1
and the router deleted by Task C1.2).

The load-bearing rule is what the template does NOT bring. It carries
behaviour — role, instructions — and never knowledge, tables or connections,
because a template is portable between users and instances and a data-package
id means something else (or nothing) wherever it lands. The template brings the
role; the person brings their own data. A test that only checked the prompt
copied across would miss the half that matters.
"""

from __future__ import annotations

import io
import zipfile

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# The store's content guardrails are real: an agent description must reach 60
# characters and its body 200, because both carry marketplace tile copy. Test
# fixtures have to clear the same bar as a real submission.
_DESC = (
    "Writes release notes from a changelog diff, grouping entries by audience "
    "impact and dropping internal-only churn. Use when cutting a release."
)


def _agent_zip(name: str, body: str, *, frontmatter_extra: str = "") -> bytes:
    """A minimal Agent Template bundle — one .md at the ZIP root."""
    md = f"---\nname: {name}\ndescription: {_DESC}\n{frontmatter_extra}---\n\n{body}\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{name}.md", md)
    return buf.getvalue()


@pytest.fixture
def template(seeded_app):
    """Publish an Agent Template owned by the analyst, return its entity id."""
    body = (
        "You are a release-notes writer.\n\n"
        "## How you work\n"
        "- Read the changelog diff and group entries by who they affect.\n"
        "- Drop internal churn: refactors, test-only changes, dependency bumps.\n"
        "- Lead with the behaviour a reader would notice, not the commit title.\n"
        "- Keep each entry to one sentence unless the change needs a caveat.\n\n"
        "## What you never do\n"
        "- Invent a version number or a date; ask if either is missing."
    )
    resp = seeded_app["client"].post(
        "/api/store/entities",
        files={"file": ("t.zip", _agent_zip("tpl-writer", body), "application/zip")},
        data={"type": "agent", "description": _DESC},
        headers=_auth(seeded_app["analyst_token"]),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], body


class TestPrefill:
    def test_the_template_body_becomes_the_agents_instructions(self, seeded_app, template):
        entity_id, body = template
        resp = seeded_app["client"].post(
            "/api/v1/agents",
            json={"name": "From Template", "template_entity_id": entity_id},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 201, resp.text
        agent = resp.json()
        assert "release-notes writer" in (agent.get("instructions") or "")
        # The frontmatter block is stripped — it is metadata, not prompt.
        assert "---" not in (agent.get("instructions") or "").split("\n")[0]

    def test_the_caller_still_wins_over_the_template(self, seeded_app, template):
        """A template is a starting point, not an override."""
        entity_id, _ = template
        resp = seeded_app["client"].post(
            "/api/v1/agents",
            json={
                "name": "Mine",
                "template_entity_id": entity_id,
                "instructions": "My own prompt.",
            },
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["instructions"] == "My own prompt."


class TestTemplateNeverCarriesData:
    """The half of this feature that is a rule rather than a convenience."""

    def test_knowledge_stays_empty(self, seeded_app, template):
        entity_id, _ = template
        resp = seeded_app["client"].post(
            "/api/v1/agents",
            json={"name": "No Data", "template_entity_id": entity_id},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 201, resp.text
        agent = resp.json()
        assert agent.get("knowledge") in (None, [], ""), (
            "a template must never bring data with it — its ids are meaningless in another user's instance"
        )

    def test_starting_blank_is_still_supported(self, seeded_app):
        """No template is the ordinary path and must not have regressed."""
        resp = seeded_app["client"].post(
            "/api/v1/agents",
            json={"name": "Blank"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["name"] == "Blank"


class TestAccess:
    def test_an_unknown_template_404s(self, seeded_app):
        resp = seeded_app["client"].post(
            "/api/v1/agents",
            json={"name": "X", "template_entity_id": "does-not-exist"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 404

    def test_a_non_agent_entity_is_not_a_template(self, seeded_app):
        """Pointing at a skill must not silently produce a blank agent."""
        skill_body = (
            "Use this skill when reconciling two ledgers that disagree.\n\n"
            "## Steps\n"
            "- Pull both sides for the same period and normalise the currency.\n"
            "- Match on transaction id first, then on amount plus date.\n"
            "- Report only the residual, with the largest discrepancy first.\n"
            "- Never silently drop a row you could not match.\n"
        )
        z = io.BytesIO()
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("s/SKILL.md", f"---\nname: s\ndescription: {_DESC}\n---\n\n{skill_body}")
        created = seeded_app["client"].post(
            "/api/store/entities",
            files={"file": ("s.zip", z.getvalue(), "application/zip")},
            data={"type": "skill", "description": _DESC},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert created.status_code == 201, created.text
        resp = seeded_app["client"].post(
            "/api/v1/agents",
            json={"name": "X", "template_entity_id": created.json()["id"]},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 404
