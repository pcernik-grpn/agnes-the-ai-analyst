"""Append-only Alembic revision-id ratchet (issue #2086).

``facts_ingest_runs`` shipped as revision id ``0077_facts_ingest_runs``, then
got RENUMBERED to ``0078_facts_ingest_runs`` when ``0077_ontology_drafts``
was inserted before it. A database that had already applied the old id was
left stranded: no image, past or future, ships a migration script under that
exact string any more, so ``ScriptDirectory`` can't resolve it and
``assert_pg_at_head()`` couldn't even tell "stranded" from "AHEAD" (that
distinction — and a targeted repair — is issue #2086's other half; see
``src/db_pg.py``'s ``_unknown_revision_is_stranded`` /
``RENUMBERED_REVISION_REPAIRS`` and ``tests/db_pg/test_startup_revision_
check.py``).

This file is the durable "test for next time": ``migrations/shipped_
revision_ids.txt`` is a checked-in, append-only manifest of every revision id
that has ever shipped, and these tests enforce it from both directions so it
never silently rots:

  (a) every id in the manifest must still resolve in the live migration
      chain — a renumber or delete fails this;
  (b) every id in the live migration chain must be listed in the manifest —
      a new migration that forgets to append its id fails this until the
      line is added, which is what keeps the manifest self-maintaining;
  (c) the chain must be linear with a single head.

These need no Postgres — pure ``ScriptDirectory`` introspection over the
files already on disk.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "migrations" / "shipped_revision_ids.txt"


def _script_directory():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return ScriptDirectory.from_config(cfg)


def _parse_manifest(text: str) -> list[str]:
    """Parse the manifest into an ordered list of revision ids.

    Blank lines and ``#``-prefixed comment lines are ignored; everything
    else is one revision id per line.
    """
    ids = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        ids.append(stripped)
    return ids


def _chain_ids(script) -> set[str]:
    """Every revision id in the live migration chain (base..heads)."""
    return {r.revision for r in script.walk_revisions("base", "heads")}


def _unresolvable_manifest_ids(script, manifest_ids: list[str]) -> list[str]:
    """Manifest ids that ``ScriptDirectory.get_revision`` cannot resolve."""
    unresolvable = []
    for rev_id in manifest_ids:
        try:
            script.get_revision(rev_id)
        except Exception:
            unresolvable.append(rev_id)
    return unresolvable


def _chain_ids_missing_from_manifest(script, manifest_ids: list[str]) -> list[str]:
    """Chain ids that the manifest has not (yet) recorded."""
    return sorted(_chain_ids(script) - set(manifest_ids))


# ---------------------------------------------------------------------------
# (a) + (b): the two directions of the ratchet, run for real against the
# live manifest and migration chain.
# ---------------------------------------------------------------------------


def test_every_manifest_id_still_resolves_in_the_live_chain():
    """A renumbered or deleted shipped id must fail loudly, not silently."""
    script = _script_directory()
    manifest_ids = _parse_manifest(MANIFEST_PATH.read_text())

    unresolvable = _unresolvable_manifest_ids(script, manifest_ids)

    assert not unresolvable, (
        "revision ids are immutable once shipped; renumbering strands every "
        "database that applied the old id (issue #2086); insert new "
        "revisions at the head instead. The following id(s) recorded in "
        f"{MANIFEST_PATH.relative_to(REPO_ROOT)} no longer resolve via "
        f"ScriptDirectory.get_revision(): {unresolvable}"
    )


def test_every_chain_id_is_recorded_in_the_manifest():
    """A new migration must append its id to the manifest before this
    passes — the message says exactly what to add."""
    script = _script_directory()
    manifest_ids = _parse_manifest(MANIFEST_PATH.read_text())

    missing = _chain_ids_missing_from_manifest(script, manifest_ids)

    assert not missing, (
        "new Alembic revision(s) are missing from the append-only ratchet "
        f"manifest. Append the following line(s) to "
        f"{MANIFEST_PATH.relative_to(REPO_ROOT)} (never renumber or reuse "
        f"an id that already shipped elsewhere):\n" + "\n".join(missing)
    )


def test_migration_chain_is_linear_with_a_single_head():
    """A second head means two revisions were shipped with the same
    ``down_revision`` and never reconciled with a merge revision.

    No dedicated single-head test exists elsewhere: ``alembic upgrade
    head`` (exercised by ``tests/db_pg/test_alembic_skeleton.py::
    test_alembic_upgrade_head_runs`` and friends) would also fail on
    multiple heads, but only as an incidental side effect buried in a
    Postgres-backed test — this makes the requirement explicit,
    fast (no PG needed), and immediately diagnosable.
    """
    script = _script_directory()
    heads = script.get_heads()

    assert len(heads) == 1, (
        f"expected exactly one Alembic head, found {len(heads)}: {heads} — "
        "reconcile the branches with a merge revision (see "
        "migrations/versions/0091_merge_semantic_facts.py or "
        "0093_merge_train23_semantic.py for the pattern)"
    )


# ---------------------------------------------------------------------------
# Meta-tests: prove the ratchet's own assertions actually fire, using
# fabricated manifests/chains rather than mutating the real ones (which
# would either be a no-op — the real chain has no such gap — or would break
# the real guards above for the rest of the suite).
# ---------------------------------------------------------------------------


def test_ratchet_catches_a_renumbered_or_deleted_shipped_id():
    """A manifest id that no longer resolves must be flagged — using the
    ACTUAL legacy id issue #2086 orphaned, which by construction cannot
    resolve against the current chain (it was renumbered to
    ``0078_facts_ingest_runs``)."""
    script = _script_directory()
    fabricated_manifest = ["0001_baseline", "0077_facts_ingest_runs"]

    unresolvable = _unresolvable_manifest_ids(script, fabricated_manifest)

    assert unresolvable == ["0077_facts_ingest_runs"]


def test_ratchet_catches_a_chain_id_missing_from_the_manifest():
    """Dropping one real id from an otherwise-complete manifest must be
    flagged by name."""
    script = _script_directory()
    real_manifest_ids = _parse_manifest(MANIFEST_PATH.read_text())
    incomplete_manifest = [rev_id for rev_id in real_manifest_ids if rev_id != "0095_memory_detection_runs"]

    missing = _chain_ids_missing_from_manifest(script, incomplete_manifest)

    assert missing == ["0095_memory_detection_runs"]


def test_ratchet_manifest_ids_are_unique():
    """The manifest is a set as far as the tests are concerned (comment
    header says so); a duplicate line would silently hide a missing id
    from ``_chain_ids_missing_from_manifest``'s set-difference check."""
    manifest_ids = _parse_manifest(MANIFEST_PATH.read_text())

    duplicates = sorted({rev_id for rev_id in manifest_ids if manifest_ids.count(rev_id) > 1})

    assert not duplicates, f"duplicate id(s) in {MANIFEST_PATH.relative_to(REPO_ROOT)}: {duplicates}"
