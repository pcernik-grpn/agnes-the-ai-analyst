"""A builder-created agent gets a real slug once it is named.

The `/agents` builder used to create the row on "New agent" — before the
user had typed anything — so its own (now-deleted, Task C1.2) `/api/agents`
adapter router's `create_agent` fell back to the literal slug `"agent"`
(`_auto_slug(name or "agent")`). Its `PATCH` then wrote every field the
builder sent EXCEPT the slug, so the agent kept `"agent"` forever while its
display name said something else entirely.

That slug is not cosmetic: it is the address in
`POST /api/v1/agents/{slug}/responses` and `agnes chat <slug>`. An agent
shown as "Revenue Analyst" answered on `/agent`, the second such agent on
`/agent-2`, and the builder shows the slug nowhere — so the only way to
find it was to enumerate the API.

The slug follows the name while BOTH hold: the agent is still a draft, and
its slug still tracks that name. Marking it ready is what publishes the
address, so that freezes it; a slug set by any other means has stopped
being a function of the name, so a rename must not relocate it.

Re-deriving on every draft rename, rather than once off the placeholder,
is forced by how the builder saves: it PATCHes (now PUTs, Task C1.1/C1.2)
each field edit behind a debounce, so a pause mid-word flushes a PARTIAL
name. A once-only rule latched onto that fragment and gave the finished
agent the address `rev` — worse than the placeholder it replaced, and
uncorrectable from the UI. Caught by Devin Review on #1225, along with the
uniqueness search being scoped to the caller rather than the agent's owner.

`POST`/`PUT /api/v1/agents*` (Task C1.1) absorbed this rename rule
unchanged (`app.api.agents_builder_shared._draft_slug_rename`) but, unlike
the builder's old router, `POST` requires a non-blank `name` (400
`invalid_name`) — a blank-name draft can no longer be minted through the
API. `_create_blank_draft` below writes that precondition directly through
the repo, exactly the shape the old create endpoint produced, so the
rename rule itself stays covered end to end.
"""

from __future__ import annotations

import uuid

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create(seeded_app, token: str, **fields) -> dict:
    r = seeded_app["client"].post("/api/v1/agents", json=fields, headers=_auth(token))
    assert r.status_code == 201, r.text
    return r.json()


def _patch(seeded_app, agent_id: str, token: str, **fields) -> dict:
    r = seeded_app["client"].put(f"/api/v1/agents/{agent_id}", json=fields, headers=_auth(token))
    assert r.status_code == 200, r.text
    return r.json()


def _create_blank_draft(owner_user_id: str) -> dict:
    """A placeholder-name draft — exactly what "New agent" used to POST
    through the builder's own (now-deleted) `/api/agents` router. Mints the
    SAME placeholder slug `create_agent`'s old fallback derived
    (`_unique_slug(_auto_slug("agent"), owner_user_id)`), so this stays
    pinned to the one rename contract regardless of how the row was born.
    """
    from app.api.agents_builder_shared import _auto_slug, _unique_slug
    from src.repositories import agents_repo

    agent_id = "agt_" + uuid.uuid4().hex
    slug = _unique_slug(_auto_slug("agent"), owner_user_id)
    agents_repo().create(
        id=agent_id,
        owner_user_id=owner_user_id,
        name="",
        slug=slug,
        status="draft",
        plugins_mode="selected",
        connections_mode="selected",
        tables_mode="selected",
        memory_mode="selected",
    )
    return agents_repo().get_by_id(agent_id)


def test_unnamed_draft_gets_a_real_slug_when_named(seeded_app):
    tok = seeded_app["admin_token"]
    agent = _create_blank_draft("admin1")
    assert agent["slug"] == "agent", "precondition: the placeholder slug"

    renamed = _patch(seeded_app, agent["id"], tok, name="Revenue Analyst")
    assert renamed["slug"] == "revenue-analyst"


def test_slug_follows_the_name_through_a_keystroke_debounce(seeded_app):
    """The builder saves while you type, so partial names arrive first.

    `/agents` saves every field edit behind a short debounce, so pausing
    mid-word flushes a PUT carrying e.g. "Rev". A rule that re-derives
    once and then freezes would hand the finished agent the address `rev`
    — worse than the placeholder it replaced, and uncorrectable from the
    UI. While the agent is a draft whose slug still tracks its name, the
    slug keeps following.
    """
    tok = seeded_app["admin_token"]
    agent = _create_blank_draft("admin1")
    assert _patch(seeded_app, agent["id"], tok, name="Rev")["slug"] == "rev"
    assert _patch(seeded_app, agent["id"], tok, name="Revenue An")["slug"] == "revenue-an"
    assert _patch(seeded_app, agent["id"], tok, name="Revenue Analyst")["slug"] == "revenue-analyst"


def test_named_at_creation_still_follows_a_draft_rename(seeded_app):
    """Same rule regardless of where the first name came from.

    `status="draft"` is explicit here: `/api/v1/agents` defaults an
    otherwise-unopinionated create to `'ready'` (its own pre-existing
    contract), whereas the builder's create always opted into `'draft'` —
    the shape this rule exists for.
    """
    tok = seeded_app["admin_token"]
    agent = _create(seeded_app, tok, name="Finance Bot", status="draft")
    assert agent["slug"] == "finance-bot"

    renamed = _patch(seeded_app, agent["id"], tok, name="Renamed Bot")
    assert renamed["slug"] == "renamed-bot"


def test_marking_ready_freezes_the_address(seeded_app):
    """Publishing is what makes the slug an address other things may hold."""
    tok = seeded_app["admin_token"]
    agent = _create_blank_draft("admin1")
    _patch(seeded_app, agent["id"], tok, name="Revenue Analyst")
    _patch(seeded_app, agent["id"], tok, status="ready")

    renamed = _patch(seeded_app, agent["id"], tok, name="Something Else")
    assert renamed["slug"] == "revenue-analyst"


def test_a_slug_that_stopped_tracking_its_name_is_left_alone(seeded_app):
    """Only a slug still derived from the current name may move.

    Guards the rule against a slug set by any other means (a future
    user-chosen slug, a migration): it no longer matches the name, so the
    rename must not silently relocate it.
    """
    from src.repositories import agents_repo

    tok = seeded_app["admin_token"]
    agent = _create(seeded_app, tok, name="Finance Bot", status="draft")
    agents_repo().update(agent["id"], slug="hand-picked")

    renamed = _patch(seeded_app, agent["id"], tok, name="Renamed Bot")
    assert renamed["slug"] == "hand-picked"


def test_renaming_the_seeded_default_agent_keeps_its_reserved_slug(seeded_app):
    """`default` is a reserved address, and the seeded agent never leaves draft.

    `get_or_create_default` seeds `name="Default"`, `slug="default"` and no
    status (COALESCEd to `draft`), and the builder lets the owner rename it —
    only DELETE is blocked. So the draft rule would relocate the one slug
    `POST /api/v1/agents/default/responses` and every web chat's attribution
    resolve through, and nothing would ever freeze it again.
    """
    from src.repositories import agents_repo

    tok = seeded_app["admin_token"]
    default = agents_repo().get_or_create_default("admin1")
    assert default["slug"] == "default", "precondition: holds the reserved slug"

    renamed = _patch(seeded_app, default["id"], tok, name="My Assistant")
    assert renamed["slug"] == "default"


def test_repeated_saves_do_not_walk_the_slug_up_a_suffix_each_time(seeded_app):
    """The builder saves the whole payload — including an unchanged name.

    When a sibling agent holds the base slug, this draft legitimately sits on
    `-2`. Re-deriving from the unchanged name then finds the base taken AND
    its own `-2` taken (the uniqueness search has no notion of the row being
    updated), so it returns `-3` — and the next ordinary save `-4`, up to the
    999 cap. A slug that already tracks the new name is left alone.
    """
    tok = seeded_app["admin_token"]
    _create(seeded_app, tok, name="Revenue Analyst")  # sibling holds the base
    draft = _create_blank_draft("admin1")
    first = _patch(seeded_app, draft["id"], tok, name="Revenue Analyst")
    assert first["slug"] == "revenue-analyst-2"

    for _ in range(3):  # ordinary edits re-send the same name
        again = _patch(seeded_app, draft["id"], tok, name="Revenue Analyst")
        assert again["slug"] == "revenue-analyst-2", "slug drifted on an unchanged name"


def test_an_unrelated_field_edit_never_moves_the_slug(seeded_app):
    """The payload always carries `name`, so every save enters the rename path."""
    tok = seeded_app["admin_token"]
    draft = _create_blank_draft("admin1")
    _patch(seeded_app, draft["id"], tok, name="Revenue Analyst")

    edited = _patch(seeded_app, draft["id"], tok, name="Revenue Analyst", greeting="Hi there")
    assert edited["slug"] == "revenue-analyst"


def test_a_foreign_drafts_slug_uniqueness_is_scoped_to_its_owner(seeded_app):
    """Uniqueness is per owner — searching the CALLER's agents is wrong.

    `/api/v1/agents` (unlike the builder's old router) refuses an admin's
    mutation of another user's agent outright (403 `agent_not_owned`,
    `agents_admin.py::_load_agent`'s documented auth matrix), so this can no
    longer be driven end to end through an admin PUT. The underlying
    property is unrelated to who is allowed to call the rename — it is
    `_draft_slug_rename`'s own uniqueness search that must key on the ROW'S
    owner, never the caller — so it is exercised directly.
    """
    from app.api.agents_builder_shared import _draft_slug_rename
    from src.repositories import agents_repo

    owner = "other-user-1"
    agents_repo().create(id="agt_owned_taken", owner_user_id=owner, name="Revenue Analyst", slug="revenue-analyst")
    agents_repo().create(id="agt_owned_draft", owner_user_id=owner, name="", slug="agent")
    before = agents_repo().get_by_id("agt_owned_draft")

    new_slug = _draft_slug_rename(before, "Revenue Analyst", owner)
    assert new_slug is not None
    assert new_slug != "revenue-analyst", "collided with the owner's existing agent"
    assert new_slug.startswith("revenue-analyst")


def test_ready_agent_keeps_its_placeholder_slug(seeded_app):
    """A published agent's address must not move under its callers.

    Even the placeholder is load-bearing once the agent is ready: someone
    may have scripted `agnes chat agent` against it.
    """
    tok = seeded_app["admin_token"]
    agent = _create_blank_draft("admin1")
    _patch(seeded_app, agent["id"], tok, status="ready")

    renamed = _patch(seeded_app, agent["id"], tok, name="Revenue Analyst")
    assert renamed["slug"] == "agent"


def test_second_unnamed_draft_named_the_same_gets_a_unique_slug(seeded_app):
    """Re-derivation goes through the same per-owner uniqueness search."""
    tok = seeded_app["admin_token"]
    a = _patch(seeded_app, _create_blank_draft("admin1")["id"], tok, name="Revenue Analyst")
    b = _patch(seeded_app, _create_blank_draft("admin1")["id"], tok, name="Revenue Analyst")
    assert a["slug"] == "revenue-analyst"
    assert b["slug"] != a["slug"]
    assert b["slug"].startswith("revenue-analyst")


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_rename_does_not_move_the_slug(seeded_app, blank):
    tok = seeded_app["admin_token"]
    agent = _create_blank_draft("admin1")
    renamed = _patch(seeded_app, agent["id"], tok, name=blank)
    assert renamed["slug"] == "agent"


def test_unsluggable_rename_does_not_produce_a_duplicate_placeholder(seeded_app):
    """A name with no alphanumerics re-derives to the placeholder base.

    It must not collide with the agent's own current slug and get suffixed
    to `agent-2` — that would rename the address for no user-visible reason.
    """
    tok = seeded_app["admin_token"]
    agent = _create_blank_draft("admin1")
    renamed = _patch(seeded_app, agent["id"], tok, name="!!!")
    assert renamed["slug"] == "agent"


def test_governance_created_agent_slug_never_moves_through_a_builder_rename(seeded_app):
    """A governance-created agent (explicit slug, `POST /api/v1/agents`) is
    published by definition — the caller chose the slug at creation time, and
    `create_agent` marks it `ready` by default. Before the C1.1/C1.2
    consolidation, an agent created this way was indistinguishable from a
    builder placeholder if it went through the builder's own router: `status`
    defaulted to `draft` on both backends there, so the very rename rule
    this module tests would relocate its deliberately-chosen slug — and any
    PAT already minted against it — on the next builder field-edit save.
    """
    tok = seeded_app["admin_token"]
    client = seeded_app["client"]
    r = client.post("/api/v1/agents", json={"name": "My Bot", "slug": "my-bot"}, headers=_auth(tok))
    assert r.status_code == 201, r.text
    agent = r.json()
    assert agent["status"] == "ready", "a governance-created agent publishes its slug immediately"

    # The same route also refuses to move the slug directly.
    put = client.put(f"/api/v1/agents/{agent['id']}", json={"slug": "renamed"}, headers=_auth(tok))
    assert put.status_code == 400
    assert put.json()["detail"]["code"] == "slug_immutable"

    # A PUT that goes through `_draft_slug_rename` — status='ready' must
    # freeze the slug there too.
    renamed = _patch(seeded_app, agent["id"], tok, name="Support Bot")
    assert renamed["slug"] == "my-bot"
