"""Cross-engine contract tests for the store/marketplace repository cluster.

Targets: marketplace_registry_repo, store_entities_repo,
         user_store_installs_repo, store_submissions_repo.
Parametrises over [DuckDB impl, Postgres impl]; identical inputs must
produce identical outputs from both engines.

Follows the pattern established in test_audit_contract.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest
import sqlalchemy as sa


# ---------------------------------------------------------------------------
# repo construction helpers — one per backend
# ---------------------------------------------------------------------------


def _make_duckdb_repos(tmp_path):
    from src.db import _ensure_schema
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository
    from src.repositories.store_entities import StoreEntitiesRepository
    from src.repositories.user_store_installs import UserStoreInstallsRepository
    from src.repositories.store_submissions import StoreSubmissionsRepository
    from src.repositories.users import UserRepository

    conn = duckdb.connect(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return {
        "registry": MarketplaceRegistryRepository(conn),
        "entities": StoreEntitiesRepository(conn),
        "installs": UserStoreInstallsRepository(conn),
        "submissions": StoreSubmissionsRepository(conn),
        "users": UserRepository(conn),
    }, conn


def _make_pg_repos(pg_engine, monkeypatch):
    from alembic import command
    from alembic.config import Config

    REPO_ROOT = Path(__file__).resolve().parents[2]
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    engine = db_pg.get_engine()

    from src.repositories.marketplace_registry_pg import MarketplaceRegistryPgRepository
    from src.repositories.store_entities_pg import StoreEntitiesPgRepository
    from src.repositories.user_store_installs_pg import UserStoreInstallsPgRepository
    from src.repositories.store_submissions_pg import StoreSubmissionsPgRepository
    from src.repositories.users_pg import UsersPgRepository

    return {
        "registry": MarketplaceRegistryPgRepository(engine),
        "entities": StoreEntitiesPgRepository(engine),
        "installs": UserStoreInstallsPgRepository(engine),
        "submissions": StoreSubmissionsPgRepository(engine),
        "users": UsersPgRepository(engine),
    }, None


@pytest.fixture(params=["duckdb", "pg"])
def store_repos(request, tmp_path, pg_engine, monkeypatch):
    """Yields ``(repos_dict, raw_conn_or_None, backend)`` for both backends."""
    backend = request.param
    if backend == "duckdb":
        repos, conn = _make_duckdb_repos(tmp_path)
        yield repos, conn, backend
        if conn is not None:
            conn.close()
    else:
        repos, _ = _make_pg_repos(pg_engine, monkeypatch)
        yield repos, None, backend


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_entity(repo, **kwargs):
    defaults = dict(
        id="entity-1",
        owner_user_id="user-1",
        owner_username="alice",
        type="skill",
        name="my-skill",
        description="A test skill",
        category="Productivity",
        version="abc123",
        file_size=1024,
        visibility_status="pending",
    )
    defaults.update(kwargs)
    return repo.create(**defaults)


# ---------------------------------------------------------------------------
# contract tests
# ---------------------------------------------------------------------------


def test_register_marketplace_then_list_returns_it(store_repos):
    repos, _, _ = store_repos
    reg = repos["registry"]
    reg.register(
        id="mp-1",
        name="Test Marketplace",
        url="https://example.com/repo.git",
        curator_name="alice",
    )
    all_regs = reg.list_all()
    ids = [r["id"] for r in all_regs]
    assert "mp-1" in ids
    fetched = reg.get("mp-1")
    assert fetched is not None
    assert fetched["name"] == "Test Marketplace"
    assert fetched["curator_name"] == "alice"


def test_register_marketplace_upsert_preserves_curator(store_repos):
    """Re-register with curator_name=None must NOT clobber existing curator."""
    repos, _, _ = store_repos
    reg = repos["registry"]
    reg.register(id="mp-2", name="MP v1", url="https://example.com/r.git", curator_name="bob")
    reg.register(id="mp-2", name="MP v2", url="https://example.com/r.git")
    row = reg.get("mp-2")
    assert row["name"] == "MP v2"
    assert row["curator_name"] == "bob"


def test_register_marketplace_with_ref_pin(store_repos):
    """`ref` (tag/commit pin, #781) round-trips through register + get."""
    repos, _, _ = store_repos
    reg = repos["registry"]
    reg.register(id="mp-ref", name="Ref MP", url="https://example.com/r.git", ref="v1.2.3")
    row = reg.get("mp-ref")
    assert row["ref"] == "v1.2.3"
    assert row["branch"] is None


def test_register_marketplace_upsert_overwrites_ref(store_repos):
    """Unlike curator fields, `ref` is always overwritten on conflict —
    mirrors `branch`'s "current desired state" semantics, not sticky
    metadata. Re-registering with ref=None clears a previous pin."""
    repos, _, _ = store_repos
    reg = repos["registry"]
    reg.register(id="mp-ref-2", name="Ref MP", url="https://example.com/r.git", ref="v1.0.0")
    reg.register(id="mp-ref-2", name="Ref MP v2", url="https://example.com/r.git", branch="main")
    row = reg.get("mp-ref-2")
    assert row["ref"] is None
    assert row["branch"] == "main"


def test_store_entity_create_then_get(store_repos):
    repos, _, backend = store_repos
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    entity = _make_entity(repos["entities"])
    assert entity["id"] == "entity-1"
    assert entity["name"] == "my-skill"
    assert entity["visibility_status"] == "pending"
    fetched = repos["entities"].get("entity-1")
    assert fetched is not None
    assert fetched["id"] == "entity-1"


def test_store_entity_set_visibility_approved(store_repos):
    repos, _, _ = store_repos
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    _make_entity(repos["entities"])
    repos["entities"].set_visibility("entity-1", "approved")
    row = repos["entities"].get("entity-1")
    assert row["visibility_status"] == "approved"


def test_synthetic_name_taken(store_repos):
    repos, _, _ = store_repos
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    se = repos["entities"]
    # Nothing registered yet.
    assert se.synthetic_name_taken("my-skill-by-alice") is False
    # _make_entity defaults synthetic_name to "<name>-by-<owner_username>".
    _make_entity(se)
    assert se.synthetic_name_taken("my-skill-by-alice") is True
    # Excluding the row itself frees the slot (used by the rename path).
    assert se.synthetic_name_taken("my-skill-by-alice", exclude_entity_id="entity-1") is False
    # Archived rows are skipped when exclude_archived=True (re-upload after archive).
    se.set_visibility("entity-1", "archived")
    assert se.synthetic_name_taken("my-skill-by-alice", exclude_archived=True) is False
    assert se.synthetic_name_taken("my-skill-by-alice") is True  # still present without the flag


# ---------------------------------------------------------------------------
# list_approved_synthetic_types — backs MarketplaceItemLookup (usage-telemetry
# attribution) flea-bundle prefix recognition.
# ---------------------------------------------------------------------------


def test_list_approved_synthetic_types_empty_when_no_entities(store_repos):
    repos, _, _ = store_repos
    assert repos["entities"].list_approved_synthetic_types() == {}


def test_list_approved_synthetic_types_only_includes_approved(store_repos):
    repos, _, _ = store_repos
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    _make_entity(
        repos["entities"],
        id="entity-approved",
        name="approved-skill",
        synthetic_name="approved-skill-by-alice",
        visibility_status="approved",
    )
    _make_entity(
        repos["entities"],
        id="entity-pending",
        name="pending-skill",
        synthetic_name="pending-skill-by-alice",
        visibility_status="pending",
    )
    result = repos["entities"].list_approved_synthetic_types()
    assert result == {"approved-skill-by-alice": "skill"}


def test_list_approved_synthetic_types_maps_type(store_repos):
    repos, _, _ = store_repos
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    _make_entity(
        repos["entities"],
        id="entity-agent",
        type="agent",
        name="my-agent",
        synthetic_name="my-agent-by-alice",
        visibility_status="approved",
    )
    result = repos["entities"].list_approved_synthetic_types()
    assert result == {"my-agent-by-alice": "agent"}


def test_install_plugin_creates_row(store_repos):
    repos, _, _ = store_repos
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    _make_entity(repos["entities"], visibility_status="approved")

    result = repos["installs"].install("user-1", "entity-1")
    assert result is True  # new row created

    # Idempotent — second call returns False
    result2 = repos["installs"].install("user-1", "entity-1")
    assert result2 is False


def test_uninstall_removes_row(store_repos):
    repos, _, _ = store_repos
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    _make_entity(repos["entities"], visibility_status="approved")

    repos["installs"].install("user-1", "entity-1")
    removed = repos["installs"].uninstall("user-1", "entity-1")
    assert removed is True

    removed2 = repos["installs"].uninstall("user-1", "entity-1")
    assert removed2 is False


def test_list_for_user_returns_enriched_columns(store_repos):
    """list_for_user must surface title/tagline/synthetic_name on BOTH
    backends — _flea_to_item reads entity['synthetic_name'] directly
    (KeyError → marketplace My Stack 500 otherwise)."""
    repos, _, _ = store_repos
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    _make_entity(repos["entities"], visibility_status="approved")
    repos["installs"].install("user-1", "entity-1")

    rows = repos["installs"].list_for_user("user-1")
    assert len(rows) == 1
    for key in ("synthetic_name", "title", "tagline"):
        assert key in rows[0], f"missing {key}"


def test_submission_lifecycle_pending_to_approved(store_repos):
    """create → status='pending_llm' → update_status to 'approved' → get reflects it."""
    repos, _, _ = store_repos
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    _make_entity(repos["entities"])

    sub_id = repos["submissions"].create(
        submitter_id="user-1",
        submitter_email="alice@x.com",
        type="skill",
        name="my-skill",
        version="abc123",
        status="pending_llm",
        entity_id="entity-1",
    )
    assert isinstance(sub_id, str) and len(sub_id) > 0

    row = repos["submissions"].get(sub_id)
    assert row is not None
    assert row["status"] == "pending_llm"

    updated = repos["submissions"].update_status(sub_id, status="approved")
    assert updated is True

    row2 = repos["submissions"].get(sub_id)
    assert row2["status"] == "approved"


def test_submission_terminal_status_not_overwritten(store_repos):
    """update_status must not overwrite a terminal state (CAS guard)."""
    repos, _, _ = store_repos
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    _make_entity(repos["entities"])

    sub_id = repos["submissions"].create(
        submitter_id="user-1",
        submitter_email="alice@x.com",
        type="skill",
        name="my-skill",
        version="abc123",
        status="approved",
        entity_id="entity-1",
    )
    # Attempt to overwrite terminal 'approved' without allow flag
    updated = repos["submissions"].update_status(sub_id, status="pending_llm")
    assert updated is False

    row = repos["submissions"].get(sub_id)
    assert row["status"] == "approved"


def test_archived_entity_visible_with_list(store_repos):
    """Archived entity survives get(); list with visibility filter controls visibility."""
    repos, _, _ = store_repos
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    _make_entity(repos["entities"], visibility_status="approved")

    # Archive it
    repos["entities"].archive("entity-1", by_user_id="user-1")
    row = repos["entities"].get("entity-1")
    assert row is not None
    assert row["visibility_status"] == "archived"

    # list() with visibility_status=["approved"] must NOT include archived
    items, total = repos["entities"].list(visibility_status=["approved"])
    ids = [i["id"] for i in items]
    assert "entity-1" not in ids

    # list() without filter (admin view) DOES include archived
    items_all, total_all = repos["entities"].list()
    ids_all = [i["id"] for i in items_all]
    assert "entity-1" in ids_all


def test_publisher_kind_defaults_to_user(store_repos):
    """v104: an upload is user-published until an admin says otherwise, and
    'none' verification renders no chip — so the upgrade is visually inert."""
    repos, _, _ = store_repos
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    entity = _make_entity(repos["entities"])
    assert entity["publisher_kind"] == "user"
    assert entity["verification_state"] == "none"
    assert entity["verified_at"] is None


def test_create_rejects_unknown_publisher_kind(store_repos):
    repos, _, _ = store_repos
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    with pytest.raises(ValueError):
        _make_entity(repos["entities"], publisher_kind="admin")


def test_set_publisher_kind_round_trips(store_repos):
    repos, _, _ = store_repos
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    se = repos["entities"]
    _make_entity(se)
    assert se.set_publisher_kind("entity-1", "organization", by_user_id="admin-1") is True
    assert se.get("entity-1")["publisher_kind"] == "organization"
    assert se.set_publisher_kind("entity-1", "user", by_user_id="admin-1") is True
    assert se.get("entity-1")["publisher_kind"] == "user"


def test_set_publisher_kind_missing_row_returns_false(store_repos):
    repos, _, _ = store_repos
    assert repos["entities"].set_publisher_kind("nope", "organization", by_user_id="admin-1") is False


def test_set_verification_stamps_only_on_verified(store_repos):
    """verified_at/by must never read stale: stamped for 'verified', cleared
    for every other state."""
    repos, _, _ = store_repos
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    se = repos["entities"]
    _make_entity(se)

    assert se.set_verification("entity-1", "verified", by_user_id="admin-1") is True
    row = se.get("entity-1")
    assert row["verification_state"] == "verified"
    assert row["verified_at"] is not None
    assert row["verified_by"] == "admin-1"

    assert se.set_verification("entity-1", "changes_requested", by_user_id="admin-1", note="Thin description") is True
    row = se.get("entity-1")
    assert row["verification_state"] == "changes_requested"
    assert row["verified_at"] is None
    assert row["verification_note"] == "Thin description"


def test_set_verification_refused_on_organization_published(store_repos):
    """Invariant 1: publisher_kind='organization' ⇒ verification_state='none'.
    An org item carries the stronger claim; a checkmark on top would invent a
    fourth trust tier."""
    repos, _, _ = store_repos
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    se = repos["entities"]
    _make_entity(se)
    se.set_publisher_kind("entity-1", "organization", by_user_id="admin-1")

    assert se.set_verification("entity-1", "verified", by_user_id="admin-1") is False
    assert se.get("entity-1")["verification_state"] == "none"


def test_set_publisher_kind_to_organization_clears_verification(store_repos):
    """Invariant 1 from the other direction: promoting an already-verified user
    item to organization drops the now-redundant checkmark."""
    repos, _, _ = store_repos
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    se = repos["entities"]
    _make_entity(se)
    se.set_verification("entity-1", "verified", by_user_id="admin-1")

    se.set_publisher_kind("entity-1", "organization", by_user_id="admin-1")
    row = se.get("entity-1")
    assert row["verification_state"] == "none"
    assert row["verified_at"] is None
    assert row["verification_note"] is None


def test_set_verification_rejects_unknown_state(store_repos):
    repos, _, _ = store_repos
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    _make_entity(repos["entities"])
    with pytest.raises(ValueError):
        repos["entities"].set_verification("entity-1", "approved", by_user_id="admin-1")


def test_list_filters_by_publisher_kind_and_owner_split(store_repos):
    """The three Publisher facet values: Organization is publisher_kind alone;
    Me / Other users are a per-request split of publisher_kind='user'."""
    repos, _, _ = store_repos
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    repos["users"].create(id="user-2", email="bob@x.com", name="Bob")
    se = repos["entities"]
    _make_entity(se, id="e-org", name="org-skill", visibility_status="approved")
    _make_entity(se, id="e-mine", name="my-skill", visibility_status="approved")
    _make_entity(
        se,
        id="e-theirs",
        name="their-skill",
        owner_user_id="user-2",
        owner_username="bob",
        visibility_status="approved",
    )
    se.set_publisher_kind("e-org", "organization", by_user_id="admin-1")

    org, _ = se.list(publisher_kind="organization")
    assert [i["id"] for i in org] == ["e-org"]

    mine, _ = se.list(publisher_kind="user", owner_user_id="user-1")
    assert [i["id"] for i in mine] == ["e-mine"]

    theirs, _ = se.list(publisher_kind="user", exclude_owner_user_id="user-1")
    assert [i["id"] for i in theirs] == ["e-theirs"]


def test_list_includes_granted_hidden_entities(store_repos):
    """`include_ids` is the browse half of a `store_entity` grant: a hidden
    entity a group holds a grant for belongs in that group's listing, because
    private means "not everyone", not "nobody but me". Hand-maintained in two
    dialects beside `include_owner_id`, so both are asserted."""
    repos, _, _ = store_repos
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    se = repos["entities"]
    _make_entity(se, id="e-pub", name="public-skill", visibility_status="approved")
    _make_entity(se, id="e-shared", name="shared-skill", visibility_status="hidden")
    _make_entity(se, id="e-secret", name="secret-skill", visibility_status="hidden")

    plain, _ = se.list(visibility_status=["approved"])
    assert [i["id"] for i in plain] == ["e-pub"]

    granted, total = se.list(visibility_status=["approved"], include_ids=["e-shared"])
    assert sorted(i["id"] for i in granted) == ["e-pub", "e-shared"]
    assert total == 2, "the count has to agree with the rows, or paging lies"

    # ...and only what was granted: the id list is matched, not counted.
    assert "e-secret" not in [i["id"] for i in granted]


def test_granted_archived_entities_stay_out_of_browse(store_repos):
    """Archived is admin-only across browse — the same carve-out
    `include_owner_id` makes for the caller's own rows."""
    repos, _, _ = store_repos
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    se = repos["entities"]
    _make_entity(se, id="e-gone", name="archived-skill", visibility_status="archived")
    rows, _ = se.list(visibility_status=["approved"], include_ids=["e-gone"])
    assert [i["id"] for i in rows] == []


def test_list_filters_by_verification_state(store_repos):
    repos, _, _ = store_repos
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    se = repos["entities"]
    _make_entity(se, id="e-ver", name="verified-skill", visibility_status="approved")
    _make_entity(se, id="e-raw", name="raw-skill", visibility_status="approved")
    se.set_verification("e-ver", "verified", by_user_id="admin-1")

    verified, _ = se.list(verification_state=["verified"])
    assert [i["id"] for i in verified] == ["e-ver"]

    # "Unverified" is a filter VALUE (the union of the non-positive states),
    # never a label rendered on a card.
    unverified, _ = se.list(verification_state=["none", "requested", "changes_requested"])
    assert [i["id"] for i in unverified] == ["e-raw"]


def test_list_rejects_unknown_facet_values(store_repos):
    repos, _, _ = store_repos
    se = repos["entities"]
    with pytest.raises(ValueError):
        se.list(publisher_kind="admin")
    with pytest.raises(ValueError):
        se.list(verification_state=["approved"])


def test_publisher_counts_splits_me_from_other_users(store_repos):
    """The honest replacement for the retired per-tab counts."""
    repos, _, _ = store_repos
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    repos["users"].create(id="user-2", email="bob@x.com", name="Bob")
    se = repos["entities"]
    _make_entity(se, id="e-org", name="org-skill", visibility_status="approved")
    _make_entity(se, id="e-mine", name="my-skill", visibility_status="approved")
    _make_entity(
        se,
        id="e-theirs",
        name="their-skill",
        owner_user_id="user-2",
        owner_username="bob",
        visibility_status="approved",
    )
    se.set_publisher_kind("e-org", "organization", by_user_id="admin-1")

    counts = se.publisher_counts(visibility_status=["approved"], viewer_user_id="user-1")
    assert counts == {"organization": 1, "me": 1, "other_users": 1}

    # No viewer (system/anonymous caller): every user-published row is
    # "other users", nothing is silently attributed to nobody.
    anon = se.publisher_counts(visibility_status=["approved"])
    assert anon == {"organization": 1, "me": 0, "other_users": 2}


def test_category_counts_groups_and_buckets_other(store_repos):
    """category_counts must reproduce the /api/marketplace/categories
    GROUP BY on both backends: NULL/empty category collapses into 'Other',
    the type filter restricts the count, and (visibility_status + owner_id)
    counts approved-for-all plus the owner's own non-archived rows."""
    repos, _, _ = store_repos
    se = repos["entities"]
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    repos["users"].create(id="user-2", email="bob@x.com", name="Bob")

    # Two approved skills in 'Productivity', one approved skill with empty
    # category (→ 'Other'), one approved agent, plus a pending skill owned by
    # user-1 and an archived skill owned by user-1.
    _make_entity(se, id="e1", name="s1", category="Productivity", visibility_status="approved")
    _make_entity(se, id="e2", name="s2", category="Productivity", visibility_status="approved")
    _make_entity(se, id="e3", name="s3", category="   ", visibility_status="approved")
    _make_entity(se, id="e4", name="a1", type="agent", category="Data", visibility_status="approved")
    _make_entity(
        se,
        id="e5",
        name="s5",
        owner_user_id="user-1",
        owner_username="alice",
        category="Pending",
        visibility_status="pending",
    )
    _make_entity(
        se,
        id="e6",
        name="s6",
        owner_user_id="user-1",
        owner_username="alice",
        category="Gone",
        visibility_status="approved",
    )
    se.archive("e6", by_user_id="user-1")

    # No filters → counts everything, empty category bucketed as 'Other'.
    # e6 was renamed by archive but its category 'Gone' is untouched.
    counts_all = se.category_counts()
    assert counts_all.get("Productivity") == 2
    assert counts_all.get("Other") == 1
    assert counts_all.get("Data") == 1
    assert counts_all.get("Pending") == 1
    assert counts_all.get("Gone") == 1

    # type='skill' restricts to skills only (excludes the agent).
    counts_skill = se.category_counts(type="skill")
    assert counts_skill.get("Data") is None
    assert counts_skill.get("Productivity") == 2

    # Non-admin browse: approved-for-all + own non-archived. user-1 sees the
    # 4 approved (Productivity x2, Other, Data) plus their own pending one,
    # but NOT their archived e6.
    counts_user1 = se.category_counts(
        visibility_status=["approved"],
        owner_id="user-1",
    )
    assert counts_user1.get("Productivity") == 2
    assert counts_user1.get("Other") == 1
    assert counts_user1.get("Data") == 1
    assert counts_user1.get("Pending") == 1
    assert counts_user1.get("Gone") is None  # archived, owner suppressed

    # A different non-owner (user-2) sees only the approved set.
    counts_user2 = se.category_counts(
        visibility_status=["approved"],
        owner_id="user-2",
    )
    assert counts_user2.get("Pending") is None
    assert counts_user2.get("Productivity") == 2

    # visibility_status only (no owner) → strict whitelist.
    counts_appr = se.category_counts(visibility_status=["approved"])
    assert counts_appr.get("Pending") is None
    assert counts_appr.get("Other") == 1


def _backdate_created_at(repos, conn, backend, sub_id, ts):
    """Force a submission's created_at into the past on either backend —
    repo.create() always stamps NOW()."""
    if backend == "duckdb":
        conn.execute(
            "UPDATE store_submissions SET created_at = ? WHERE id = ?",
            [ts, sub_id],
        )
    else:
        with repos["submissions"]._engine.begin() as c:
            c.execute(
                sa.text("UPDATE store_submissions SET created_at = :ts WHERE id = :id"),
                {"ts": ts, "id": sub_id},
            )


def test_reap_stuck_pending_llm_contract(store_repos):
    """reap_stuck_pending_llm must behave identically on both backends:
    flip aged pending_llm → review_error, leave fresh rows + non-pending
    rows alone, and be idempotent. This is the parity the DuckDB-only
    reaper silently failed on Postgres-backed instances."""
    repos, conn, backend = store_repos
    subs = repos["submissions"]
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")

    # Aged pending_llm — should be reaped.
    _make_entity(repos["entities"], id="entity-old", name="old-skill")
    old_id = subs.create(
        submitter_id="user-1",
        submitter_email="alice@x.com",
        type="skill",
        name="old-skill",
        version="abc123",
        status="pending_llm",
        entity_id="entity-old",
    )
    _backdate_created_at(
        repos,
        conn,
        backend,
        old_id,
        datetime.now(timezone.utc) - timedelta(hours=1),
    )

    # Fresh pending_llm — within grace, must survive.
    _make_entity(repos["entities"], id="entity-fresh", name="fresh-skill")
    fresh_id = subs.create(
        submitter_id="user-1",
        submitter_email="alice@x.com",
        type="skill",
        name="fresh-skill",
        version="abc123",
        status="pending_llm",
        entity_id="entity-fresh",
    )

    err = {"error": "timeout_or_crash"}
    reaped = subs.reap_stuck_pending_llm(grace_seconds=1800, error_payload=err)

    assert [r[0] for r in reaped] == [old_id]
    assert reaped[0][1] == "user-1"  # submitter_id surfaced for audit

    old_row = subs.get(old_id)
    assert old_row["status"] == "review_error"
    assert (old_row["llm_findings"] or {}).get("error") == "timeout_or_crash"

    assert subs.get(fresh_id)["status"] == "pending_llm"

    # Idempotent — the now-review_error row is not pending_llm anymore.
    reaped_again = subs.reap_stuck_pending_llm(grace_seconds=1800, error_payload=err)
    assert reaped_again == []

    # Scoped strictly to pending_llm: an aged row in any other status must
    # survive, on both backends (parity with the DuckDB unit suite's
    # test_does_not_flip_other_statuses).
    _make_entity(repos["entities"], id="entity-appr", name="appr-skill")
    appr_id = subs.create(
        submitter_id="user-1",
        submitter_email="alice@x.com",
        type="skill",
        name="appr-skill",
        version="abc123",
        status="approved",
        entity_id="entity-appr",
    )
    _backdate_created_at(
        repos,
        conn,
        backend,
        appr_id,
        datetime.now(timezone.utc) - timedelta(hours=24),
    )
    assert subs.reap_stuck_pending_llm(grace_seconds=1800, error_payload=err) == []
    assert subs.get(appr_id)["status"] == "approved"


def test_find_purge_candidates_contract(store_repos):
    """find_purge_candidates must behave identically on both backends: only
    rows whose status is in the given set, ``bundle_purged_at IS NULL``, and
    ``created_at`` predates the cutoff are returned as ``(id, entity_id)``
    pairs. This backs ``src.store_guardrails.purge.purge_blocked_bundles``,
    which used to run this SELECT on a raw DuckDB ``conn`` (never reached PG
    rows on a Postgres-backed instance)."""
    repos, conn, backend = store_repos
    subs = repos["submissions"]
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")

    # Aged blocked_llm — eligible.
    _make_entity(repos["entities"], id="entity-old", name="old-skill")
    old_id = subs.create(
        submitter_id="user-1",
        submitter_email="alice@x.com",
        type="skill",
        name="old-skill",
        version="abc123",
        status="blocked_llm",
        entity_id="entity-old",
    )
    _backdate_created_at(
        repos,
        conn,
        backend,
        old_id,
        datetime.now(timezone.utc) - timedelta(days=45),
    )

    # Fresh blocked_llm — not old enough, must be excluded.
    _make_entity(repos["entities"], id="entity-fresh", name="fresh-skill")
    subs.create(
        submitter_id="user-1",
        submitter_email="alice@x.com",
        type="skill",
        name="fresh-skill",
        version="abc123",
        status="blocked_llm",
        entity_id="entity-fresh",
    )

    # Aged but 'approved' — terminal, must be excluded.
    _make_entity(repos["entities"], id="entity-appr", name="appr-skill")
    appr_id = subs.create(
        submitter_id="user-1",
        submitter_email="alice@x.com",
        type="skill",
        name="appr-skill",
        version="abc123",
        status="approved",
        entity_id="entity-appr",
    )
    _backdate_created_at(
        repos,
        conn,
        backend,
        appr_id,
        datetime.now(timezone.utc) - timedelta(days=100),
    )

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    candidates = subs.find_purge_candidates(
        statuses=["blocked_llm", "review_error"],
        older_than=cutoff,
    )
    assert candidates == [(old_id, "entity-old")]

    # Already-purged rows (bundle_purged_at set) are excluded even if they
    # still match on status + age.
    subs.mark_bundle_purged(old_id)
    assert (
        subs.find_purge_candidates(
            statuses=["blocked_llm", "review_error"],
            older_than=cutoff,
        )
        == []
    )


def test_submission_delete_removes_row(store_repos):
    """delete() must drop the submission row and report True; a second
    delete of the same id reports False. Parity on both backends —
    DuckDB uses RETURNING+len, PG uses rowcount."""
    repos, _, _ = store_repos
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    _make_entity(repos["entities"])

    sub_id = repos["submissions"].create(
        submitter_id="user-1",
        submitter_email="alice@x.com",
        type="skill",
        name="my-skill",
        version="abc123",
        status="pending_llm",
        entity_id="entity-1",
    )
    assert repos["submissions"].get(sub_id) is not None

    removed = repos["submissions"].delete(sub_id)
    assert removed is True
    assert repos["submissions"].get(sub_id) is None

    # Idempotent: deleting a gone row reports False on both backends.
    removed2 = repos["submissions"].delete(sub_id)
    assert removed2 is False


def test_submission_list_for_entity_newest_first(store_repos):
    """list_for_entity() must return every row linked to the entity, newest
    created_at first, with the fixed id/status/version/created_at/
    reviewed_by_model projection — identical ordering on both backends."""
    repos, conn, backend = store_repos
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    _make_entity(repos["entities"])

    older_id = repos["submissions"].create(
        submitter_id="user-1",
        submitter_email="alice@x.com",
        type="skill",
        name="my-skill",
        version="v-old",
        status="approved",
        entity_id="entity-1",
    )
    _backdate_created_at(
        repos,
        conn,
        backend,
        older_id,
        datetime.now(timezone.utc) - timedelta(hours=2),
    )
    newer_id = repos["submissions"].create(
        submitter_id="user-1",
        submitter_email="alice@x.com",
        type="skill",
        name="my-skill",
        version="v-new",
        status="pending_llm",
        entity_id="entity-1",
    )

    # An unrelated entity's submission must not leak in.
    _make_entity(repos["entities"], id="entity-other", name="other-skill")
    repos["submissions"].create(
        submitter_id="user-1",
        submitter_email="alice@x.com",
        type="skill",
        name="other-skill",
        version="v-x",
        status="approved",
        entity_id="entity-other",
    )

    rows = repos["submissions"].list_for_entity("entity-1")
    assert [r["id"] for r in rows] == [newer_id, older_id]
    assert rows[0]["version"] == "v-new"
    assert rows[0]["status"] == "pending_llm"
    # Fixed projection — exactly these keys, no JSON columns.
    assert set(rows[0].keys()) == {
        "id",
        "status",
        "version",
        "created_at",
        "reviewed_by_model",
    }


def _audit_for_backend(repos, conn, backend):
    """Build an audit repo bound to the same backend the fixture uses."""
    if backend == "duckdb":
        from src.repositories.audit import AuditRepository

        return AuditRepository(conn)
    from src.repositories.audit_pg import AuditPgRepository

    return AuditPgRepository(repos["submissions"]._engine)


def test_run_llm_review_persists_verdict_contract(store_repos, tmp_path):
    """run_llm_review must find the pending submission and flip it on BOTH
    backends. On Postgres the pre-fix DuckDB-only path logged
    'submission vanished' (rows live in PG, the DuckDB handle was empty)
    and left the row stuck at pending_llm forever — this guards that
    regression by driving the runner with backend-native repos."""
    from src.store_guardrails.runner import LlmResult, run_llm_review

    repos, conn, backend = store_repos
    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    _make_entity(repos["entities"])
    sub_id = repos["submissions"].create(
        submitter_id="user-1",
        submitter_email="alice@x.com",
        type="skill",
        name="my-skill",
        version="abc123",
        status="pending_llm",
        entity_id="entity-1",
        inline_checks={"manifest": {"status": "pass"}},
    )

    plugin_dir = tmp_path / "bundle"
    plugin_dir.mkdir()
    (plugin_dir / "SKILL.md").write_text("# Test\nbody " * 30)

    safe = {
        "risk_level": "safe",
        "summary": "OK",
        "findings": [],
        "template_placeholders_found": 0,
        "reviewed_by_model": "claude-haiku-4-5-20251001",
        "error": None,
    }
    audit = _audit_for_backend(repos, conn, backend)
    with patch(
        "src.store_guardrails.runner.llm_review.review_bundle",
        return_value=safe,
    ):
        result = run_llm_review(
            sub_id,
            plugin_dir=plugin_dir,
            api_key_loader=lambda: "sk-test",
            model_loader=lambda: "claude-haiku-4-5-20251001",
            subs_repo=repos["submissions"],
            ents_repo=repos["entities"],
            audit=audit,
        )

    assert isinstance(result, LlmResult)
    assert result.passed
    # The row was found (not "vanished") and flipped on this backend.
    assert repos["submissions"].get(sub_id)["status"] == "approved"
    assert repos["entities"].get("entity-1")["visibility_status"] == "approved"


def test_run_llm_review_factory_path_resolves_pg(store_repos, tmp_path):
    """Production path: run_llm_review with NO injected repos must resolve
    the Postgres repos via the src.repositories factory and flip the row.
    The injected-repo test above proves the runner body works on PG repos;
    this proves the no-injection factory wiring (what the app actually
    calls) resolves to PG when use_pg() is true.

    PG-only: the DuckDB fixture uses a standalone duckdb.connect(), not the
    get_system_db() singleton the factory resolves, so the no-injection
    path can't see the fixture's rows on DuckDB. The factory's DuckDB
    wiring is covered by the injected-repo test + the broad DuckDB unit
    suite (tests/test_store_guardrails_llm.py)."""
    repos, conn, backend = store_repos
    if backend != "pg":
        pytest.skip("factory no-injection path is integration-tested on PG only")

    from src.store_guardrails.runner import LlmResult, run_llm_review
    from src.repositories import use_pg

    assert use_pg() is True  # fixture set AGNES_DB_URL → factory must pick PG

    repos["users"].create(id="user-1", email="alice@x.com", name="Alice")
    _make_entity(repos["entities"])
    sub_id = repos["submissions"].create(
        submitter_id="user-1",
        submitter_email="alice@x.com",
        type="skill",
        name="my-skill",
        version="abc123",
        status="pending_llm",
        entity_id="entity-1",
        inline_checks={"manifest": {"status": "pass"}},
    )

    plugin_dir = tmp_path / "bundle"
    plugin_dir.mkdir()
    (plugin_dir / "SKILL.md").write_text("# Test\nbody " * 30)

    safe = {
        "risk_level": "safe",
        "summary": "OK",
        "findings": [],
        "template_placeholders_found": 0,
        "reviewed_by_model": "claude-haiku-4-5-20251001",
        "error": None,
    }
    with patch(
        "src.store_guardrails.runner.llm_review.review_bundle",
        return_value=safe,
    ):
        result = run_llm_review(
            sub_id,
            plugin_dir=plugin_dir,
            api_key_loader=lambda: "sk-test",
            model_loader=lambda: "claude-haiku-4-5-20251001",
            # No subs_repo/ents_repo/audit → forces factory resolution.
        )

    assert isinstance(result, LlmResult)
    assert result.passed
    assert repos["submissions"].get(sub_id)["status"] == "approved"
    assert repos["entities"].get("entity-1")["visibility_status"] == "approved"
