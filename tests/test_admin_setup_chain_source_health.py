"""The setup chain's first link must be checked like every other one.

``_build_steps`` guards three hops with an integrity term — ``unpackaged``
(tables → packages), ``uncovered`` (people → groups) and ``unshared``
(packages → grants) — and each renders as a ``health`` warn line on its step.
The "Connect a source" step had none: its ``done`` was a bare row count, so a
connection that failed token validation still read "✓ 1 source is connected",
``chain_ok`` still went true, and the wizard went on to claim the project was
"already connected — no token to paste again" before dead-ending.

These tests pin the missing term. A source with neither a stored secret nor a
``token_env`` cannot read anything, and the chain has to say so.
"""

from app.services import admin_dashboard as ad


def _steps(*, sources=0, sources_unhealthy=0, tables=0, packages_with_tables=0, unpackaged=0):
    data = {
        "sources": sources,
        "sources_unhealthy": sources_unhealthy,
        "tables": tables,
        "packages": packages_with_tables,
        "packages_with_tables": packages_with_tables,
        "unpackaged": unpackaged,
    }
    people = {"people": 1, "groups": 2, "uncovered": 0, "covered": 0}
    access = {"grants": 0, "shared": 0, "unshared": 0}
    return {s["key"]: s for s in ad._build_steps(data, people, access)}


class TestConnectStepHealth:
    def test_a_credential_less_source_is_not_a_finished_step(self):
        step = _steps(sources=1, sources_unhealthy=1)["connect"]
        assert step["done"] is False

    def test_it_says_which_source_is_the_problem(self):
        step = _steps(sources=1, sources_unhealthy=1)["connect"]
        assert step["health"]["level"] == "warn"
        assert "credential" in step["health"]["text"].lower()

    def test_a_working_source_completes_the_step(self):
        step = _steps(sources=1, sources_unhealthy=0)["connect"]
        assert step["done"] is True
        assert step["health"]["level"] == "ok"

    def test_a_partly_broken_fleet_still_warns(self):
        """Two good sources do not excuse the third — the broken one is the
        one whose tables silently never arrive."""
        step = _steps(sources=3, sources_unhealthy=1)["connect"]
        assert step["done"] is False
        assert step["health"]["level"] == "warn"

    def test_no_sources_at_all_is_the_original_not_started_state(self):
        """The new term must not turn 'nothing yet' into 'something broke'."""
        step = _steps(sources=0)["connect"]
        assert step["done"] is False
        assert step["health"] is None or step["health"]["level"] != "warn"

    def test_registered_tables_alone_no_longer_tick_the_step(self):
        """`done` used to be `sources or tables`, so a hand-registered table
        marked the connection step complete on an instance with no working
        connection at all."""
        step = _steps(sources=1, sources_unhealthy=1, tables=5)["connect"]
        assert step["done"] is False


class TestVerifyStepGate:
    def test_broken_source_stops_the_final_check_going_green(self):
        """`chain_ok` is the one step an admin cannot tick by visiting a page.
        A source that cannot read is exactly the kind of break it exists for."""
        verify = _steps(
            sources=1, sources_unhealthy=1, tables=2, packages_with_tables=1
        )["verify"]
        assert verify["done"] is False
