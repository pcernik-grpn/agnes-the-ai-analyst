"""``assert_pg_at_head()`` must tell a STRANDED database (issue #2086) apart
from a genuinely AHEAD one, both of which start from the same signal —
``_pg_revisions()`` reporting a revision id unknown to this image's
``ScriptDirectory`` (``db_ahead=True``).

The disambiguation is the repo's strict ``NNNN_name`` revision-id numbering:
an unknown id whose leading 4-digit prefix is <= the script head's prefix
cannot have come from a newer image (a newer image's head number only ever
goes up), so it must instead be an id that shipped once and was later
renumbered or deleted out from under a database that had already applied it.
A prefix strictly greater than head's is the ordinary rollback case, and an
unparsable prefix (an id that doesn't follow this repo's numbering at all —
e.g. a raw hex value from a botched manual stamp) cannot be classified
either way, so it keeps the existing AHEAD wording plus one extra sentence
naming the stranded possibility.

These are pure unit tests over a monkeypatched ``_pg_revisions()`` — no
Postgres needed (the PG-backed end-to-end repair test lives in
``tests/db_pg/test_startup_revision_check.py``).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_skip_env(monkeypatch):
    """Every test here exercises the real classification logic — never the
    escape hatch, which would short-circuit before it runs."""
    monkeypatch.delenv("AGNES_SKIP_PG_REVISION_CHECK", raising=False)


def _patch_revisions(monkeypatch, current: str, head: str, db_ahead: bool) -> None:
    import src.db_pg as db_pg

    monkeypatch.setattr(db_pg, "_pg_revisions", lambda: (current, head, db_ahead))


def test_stranded_when_unknown_prefix_is_at_or_below_head(monkeypatch):
    """A renumbered-away id (#2086's actual incident) — prefix 0077 cannot
    come from a newer image whose head is 0095, so this is STRANDED, never
    AHEAD, and the message names the id, the issue, and the repair path."""
    import src.db_pg as db_pg

    _patch_revisions(monkeypatch, "0077_facts_ingest_runs", "0095_memory_detection_runs", True)

    with pytest.raises(RuntimeError) as exc:
        db_pg.assert_pg_at_head()

    msg = str(exc.value)
    assert "STRANDED" in msg
    assert "Postgres schema is AHEAD of the application" not in msg, (
        "must raise the distinct stranded message, not the ahead one "
        f"(it may still mention the word AHEAD to contrast the two): {msg}"
    )
    assert "0077_facts_ingest_runs" in msg
    assert "#2086" in msg
    assert "RENUMBERED_REVISION_REPAIRS" in msg
    assert "docs/migrations.md" in msg


def test_stranded_when_unknown_prefix_equals_head(monkeypatch):
    """Same numeric slot as head but a different string is still provably
    not a newer image (the head number cannot repeat with different
    content across images) — also STRANDED."""
    import src.db_pg as db_pg

    _patch_revisions(monkeypatch, "0095_a_sibling_that_lost_the_race", "0095_memory_detection_runs", True)

    with pytest.raises(RuntimeError) as exc:
        db_pg.assert_pg_at_head()

    assert "STRANDED" in str(exc.value)


def test_ahead_when_unknown_prefix_is_above_head(monkeypatch):
    """A higher numeric prefix than head CAN only come from a newer image
    that has since migrated further — the ordinary app-rollback case from
    issue #636, unchanged by the #2086 fix."""
    import src.db_pg as db_pg

    _patch_revisions(monkeypatch, "0099_a_future_revision", "0095_memory_detection_runs", True)

    with pytest.raises(RuntimeError) as exc:
        db_pg.assert_pg_at_head()

    msg = str(exc.value)
    assert "Postgres schema is AHEAD of the application" in msg
    assert "STRANDED" not in msg
    assert "0099_a_future_revision" in msg
    assert "0095_memory_detection_runs" in msg


def test_unparsable_prefix_keeps_ahead_wording_with_a_stranded_hint(monkeypatch):
    """An id that doesn't follow this repo's NNNN_name numbering at all
    (e.g. a raw hex value from a botched manual stamp) can't be classified
    either way — stays AHEAD (today's behavior, preserved for
    ``test_raises_db_ahead_when_revision_unknown`` in
    tests/db_pg/test_startup_revision_check.py, which uses exactly this
    shape of id), but gains one sentence naming the stranded possibility so
    an operator doesn't rule it out with false confidence."""
    import src.db_pg as db_pg

    _patch_revisions(monkeypatch, "ffffffffffff", "0095_memory_detection_runs", True)

    with pytest.raises(RuntimeError) as exc:
        db_pg.assert_pg_at_head()

    msg = str(exc.value)
    assert "AHEAD" in msg
    assert "STRANDED" not in msg
    assert "#2086" in msg, "unparsable case must still mention the stranded possibility"
    assert "ffffffffffff" in msg
