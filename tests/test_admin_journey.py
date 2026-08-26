"""The `/admin` setup chain — the guided six-step journey
(`app/services/admin_dashboard.py::resolve_journey`).

A different contract from the signal zones, so a different suite:

  * the chain ALWAYS has something to say — the healthy state ("every table
    is in a package") is information, not noise — where a zone renders only
    when something needs an admin;
  * it is ONE list: the gap that used to live in a separate card is now the
    health line under the step that owns it, so the thing to pin is that a
    step's counts and its gap derive from the same read;
  * a step is guidance, not a status: every one of them carries the WHY that
    makes the step make sense to someone meeting the object for the first
    time, and a CTA whose verb matches its state;
  * a broken repo degrades the steps that read it to "could not be checked",
    never to a green tick and never to a 500;
  * every href lands on a page where the named work can actually be done —
    the same rule the signal registry enforces.
"""

from __future__ import annotations

import re
from pathlib import Path

import markupsafe
import pytest

from app.services import admin_dashboard
from app.services.admin_dashboard import resolve_journey

_SERVICE_SRC = Path("app/services/admin_dashboard.py").read_text(encoding="utf-8")

# The chain, in the order the work happens: get data in, get people in,
# connect the two, then look at the result the way they will.
_STEPS = ["connect", "tables", "package", "people", "share", "verify"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class _Raises:
    """A repo factory stand-in whose every method raises — the 'one broken
    backend' the isolation rule exists for."""

    def __getattr__(self, name):  # pragma: no cover - trivial
        raise RuntimeError("repo unavailable")


class TestChainShape:
    def test_the_six_steps_in_order(self, seeded_app):
        setup = resolve_journey()["setup"]
        assert setup is not None
        assert [s["key"] for s in setup["steps"]] == _STEPS

    def test_every_step_is_guidance_not_a_status(self, seeded_app):
        """Title + state + WHY + somewhere to go. The `why` is what separates
        onboarding from a checklist, so a step without one is a regression
        even when it renders fine."""
        for s in resolve_journey()["setup"]["steps"]:
            assert s["title"] and s["detail"]
            assert len(s["why"]) > 40, f"{s['key']} has no real 'why'"
            assert s["href"].startswith("/admin")
            assert s["cta"] and s["done_cta"]
            assert s["state"] in {"done", "current", "later"}

    def test_exactly_one_step_is_current_until_complete(self, seeded_app):
        setup = resolve_journey()["setup"]
        current = [s for s in setup["steps"] if s["current"]]
        if setup["complete"]:
            assert current == []
        else:
            assert len(current) == 1
            # ...and it is the FIRST not-done step: the chain is a sequence,
            # not six alarms.
            first_undone = next(s for s in setup["steps"] if not s["done"])
            assert current[0] is first_undone

    def test_progress_matches_the_steps(self, seeded_app):
        setup = resolve_journey()["setup"]
        assert setup["total"] == len(setup["steps"]) == len(_STEPS)
        assert setup["done_count"] == sum(1 for s in setup["steps"] if s["done"])
        assert setup["complete"] == (setup["done_count"] == setup["total"])
        assert setup["summary"]

    def test_verify_is_the_chain_read_back(self, seeded_app):
        """The last step is the one an admin cannot tick by visiting a page:
        it is done only when nothing earlier is merely 'done' while still
        broken."""
        setup = resolve_journey()["setup"]
        steps = {s["key"]: s for s in setup["steps"]}
        verify = steps["verify"]
        earlier_all_done = all(steps[k]["done"] for k in _STEPS[:-1])
        gaps = [s["health"] for s in setup["steps"] if s.get("health")]
        no_warnings = all(h["level"] != "warn" for h in gaps)
        assert verify["done"] == (earlier_all_done and no_warnings)

    def test_every_journey_href_is_a_real_admin_route(self):
        """Same dead-link rule as the signal registry: these URLs are
        hardcoded in a module the router does not import, so nothing else
        notices when a destination moves. Query strings are stripped — the
        wizard deep link is `/admin/data-sources?add=1`."""
        router_src = Path("app/web/router.py").read_text(encoding="utf-8")
        declared = {d.split("?", 1)[0] for d in re.findall(r'"(/admin[^"]*)"', _SERVICE_SRC)}
        routes = set(re.findall(r'@router\.get\("(/admin[^"]*)"', router_src))
        prefixes = tuple(routes)
        missing = [d for d in declared if d not in routes and not d.startswith(prefixes)]
        assert not missing, f"journey hrefs with no matching /admin route: {missing}"


class TestStepIsolation:
    def test_a_raising_repo_degrades_only_the_steps_that_read_it(self, seeded_app, monkeypatch):
        import src.repositories as repos

        monkeypatch.setattr(repos, "users_repo", lambda: _Raises())
        steps = {s["key"]: s for s in resolve_journey()["setup"]["steps"]}
        assert steps["people"]["failed"] is True
        assert steps["people"]["done"] is False
        # ...and the data steps, which never touch that repo, still resolved.
        assert not steps["tables"]["failed"]
        # The end-to-end step reads all three areas, so it must not claim the
        # chain is healthy while one of them is unreadable.
        assert steps["verify"]["failed"] is True
        assert steps["verify"]["done"] is False

    def test_a_raising_repo_never_500s_the_admin_page(self, seeded_app, monkeypatch):
        import src.repositories as repos

        monkeypatch.setattr(repos, "data_packages_repo", lambda: _Raises())
        c = seeded_app["client"]
        resp = c.get("/admin", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        assert "Could not be checked" in resp.text


class TestChainSemantics:
    def test_everyone_grant_covers_every_account(self, seeded_app):
        """Auto-membership: a data-package grant to the system Everyone group
        means nobody is uncovered, without enumerating a single membership."""
        from src.repositories import resource_grants_repo, user_groups_repo

        everyone = next(g for g in user_groups_repo().list_all() if g.get("is_system") and g["name"] == "Everyone")
        grants = resource_grants_repo()
        grant_id = grants.create(group_id=everyone["id"], resource_type="data_package", resource_id="pkg-x")
        try:
            people = next(s for s in resolve_journey()["setup"]["steps"] if s["key"] == "people")
            assert people["health"]["level"] == "ok"
        finally:
            grants.delete(grant_id)

    def test_unpackaged_counts_only_distributable_modes(self):
        """`remote` tables are reachable without a package (server-side
        execution), so counting them as 'analysts cannot pull them' would be
        a false alarm — while a blank query_mode folds to `local`, the same
        rule the manifest and the distribution gate apply."""
        assert admin_dashboard._is_distributable({"query_mode": ""})
        assert admin_dashboard._is_distributable({"query_mode": None})
        assert admin_dashboard._is_distributable({"query_mode": "local"})
        assert admin_dashboard._is_distributable({"query_mode": "materialized"})
        assert not admin_dashboard._is_distributable({"query_mode": "remote"})

    def test_a_warn_health_line_always_has_somewhere_to_go(self, seeded_app):
        for s in resolve_journey()["setup"]["steps"]:
            health = s.get("health")
            if health and health["level"] == "warn":
                assert health["text"] and health["href"].startswith("/admin")
            elif health:
                assert health["text"]

    def test_people_step_is_about_the_other_people(self, seeded_app):
        """An instance only its own admin can sign into has not finished
        setup, however healthy its data is."""
        from src.repositories import users_repo

        active = [u for u in users_repo().list_all() if u.get("active", True)]
        people = next(s for s in resolve_journey()["setup"]["steps"] if s["key"] == "people")
        assert people["done"] == (len(active) > 1)


class TestHubRendersTheChain:
    def test_steps_render_on_admin(self, seeded_app):
        c = seeded_app["client"]
        body = c.get("/admin", headers=_auth(seeded_app["admin_token"])).text
        for key in _STEPS:
            assert f'data-step="{key}"' in body

    def test_the_open_panel_leads_with_the_next_step(self, seeded_app):
        c = seeded_app["client"]
        body = c.get("/admin", headers=_auth(seeded_app["admin_token"])).text
        setup = resolve_journey()["setup"]
        if setup["complete"]:
            pytest.skip("fixture instance is fully set up")
        current = next(s for s in setup["steps"] if s["current"])
        assert "Do this next" in body
        # The current step's WHY is on the page — the reason the panel exists.
        # Escaped: the copy carries apostrophes, which Jinja renders as `&#39;`.
        assert markupsafe.escape(current["why"])[:40] in body

    def test_a_completed_chain_graduates_instead_of_disappearing(self, seeded_app, monkeypatch):
        """The old path deleted itself once done, taking the guidance with it
        exactly when a second admin inherited the instance. It now collapses
        to a summary row that reopens the list."""
        real = admin_dashboard.resolve_journey

        def _complete():
            journey = real()
            for s in journey["setup"]["steps"]:
                s.update(done=True, current=False, state="done", failed=False)
            journey["setup"].update(
                complete=True,
                done_count=journey["setup"]["total"],
            )
            return journey

        monkeypatch.setattr(admin_dashboard, "resolve_journey", _complete)
        body = seeded_app["client"].get("/admin", headers=_auth(seeded_app["admin_token"])).text
        assert "This instance is set up" in body
        # Still reachable — every step is still in the DOM, behind the summary.
        assert 'data-step="verify"' in body
        # ...and the panel no longer claims the setup is unfinished.
        assert "Set up this instance" not in body


@pytest.fixture(autouse=True)
def _fresh_cache():
    admin_dashboard.invalidate_cache()
    yield
    admin_dashboard.invalidate_cache()


class TestEveryStepLandsWhereItsButtonSays:
    """A step's CTA is a promise about where the click goes.

    `verify` shipped saying "Simulate a person" with `href="/admin/access"`,
    which is the Groups workspace — Simulate is a sibling LENS on that path
    (`?lens=simulate`), so the one button whose entire purpose is "see the
    instance as they see it" opened the grant editor instead. The done-state
    sibling had it too: "Manage packages" shared the open state's href and
    opened the Tables lens, a page about something else. Structural, not a
    copy review: the pairs below are (CTA text, required href substring), so
    a renamed lens or a moved page fails here.
    """

    # CTA phrase → what its href must contain for the phrase to be true.
    # Each state is checked against ITS OWN destination: `cta` against
    # `href`, `done_cta` against `done_href` — sharing one href is what made
    # the done row's "Manage packages" open the Tables lens.
    _PROMISES = {
        "Simulate a person": "lens=simulate",
        "Manage packages": "/admin/data-packages",
        "Manage people": "/admin/users",
    }

    def test_a_cta_naming_a_destination_carries_it(self, seeded_app):
        from app.services.admin_dashboard import resolve_journey

        steps = resolve_journey()["setup"]["steps"]
        assert steps, "the journey must have steps to check"
        for step in steps:
            for key, href_key in (("cta", "href"), ("done_cta", "done_href")):
                phrase = (step.get(key) or "").strip()
                required = self._PROMISES.get(phrase)
                if not required:
                    continue
                href = step.get(href_key) or ""
                assert required in href, (
                    f"step {step.get('key')!r}: {key} says {phrase!r} but {href_key} is "
                    f"{href!r} — it must carry {required!r} or the button lies"
                )

    def test_a_done_step_keeps_its_own_destination(self, seeded_app):
        """`done_href` defaults to `href`, so a step that needs no second
        destination is unaffected — but the key must always be present, or the
        template's `s.done_href` silently renders an empty link."""
        from app.services.admin_dashboard import resolve_journey

        for step in resolve_journey()["setup"]["steps"]:
            assert step.get("done_href"), f"step {step.get('key')!r} has no done_href"

    def test_the_verify_step_opens_the_simulate_lens(self, seeded_app):
        """The specific regression, pinned by name so the fix is legible."""
        from app.services.admin_dashboard import resolve_journey

        verify = {s["key"]: s for s in resolve_journey()["setup"]["steps"]}["verify"]
        assert verify["cta"] == "Simulate a person"
        assert verify["href"] == "/admin/access?lens=simulate"
