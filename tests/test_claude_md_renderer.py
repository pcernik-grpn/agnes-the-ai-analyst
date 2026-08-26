"""Unit tests for the analyst-workspace CLAUDE.md renderer (src/claude_md.py)."""

import duckdb
import pytest
from jinja2 import TemplateError

from src.db import _ensure_schema
from src.repositories.claude_md_template import ClaudeMdTemplateRepository
from src.claude_md import (
    build_claude_md_context,
    compute_default_claude_md,
    render_claude_md,
)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    db_path = tmp_path / "system.duckdb"
    c = duckdb.connect(str(db_path))
    _ensure_schema(c)
    # RBAC reads that go through the repository factory (e.g.
    # get_accessible_tables → data_packages_repo()) resolve their connection
    # via src.repositories.get_system_db, NOT the conn passed in here. Redirect
    # that name to this fixture's connection so the factory reads the same DB
    # the test seeds — otherwise the factory opens a separate (empty) system DB
    # and package-scoped grants resolve to nothing.
    monkeypatch.setattr("src.repositories.get_system_db", lambda: c)
    yield c
    c.close()


def _user(email="alice@example.com", is_admin=False):
    return {
        "id": "u1",
        "email": email,
        "name": "Alice",
        "is_admin": is_admin,
        "groups": ["Everyone"],
    }


# ---------------------------------------------------------------------------
# Default (no override) — renders a non-empty markdown string
# ---------------------------------------------------------------------------


def test_compute_default_returns_non_empty(conn):
    out = compute_default_claude_md(conn, user=_user(), server_url="https://example.com")
    assert out.strip() != ""


def test_default_contains_server_url(conn):
    out = compute_default_claude_md(conn, user=_user(), server_url="https://myagnes.example.com")
    assert "https://myagnes.example.com" in out


def test_default_contains_user_reference(conn):
    # The footer uses `user.name or user.email` — a user with no name falls back to email.
    user_no_name = {"id": "u1", "email": "bob@example.com", "name": "", "is_admin": False, "groups": []}
    out = compute_default_claude_md(conn, user=user_no_name, server_url="https://example.com")
    assert "bob@example.com" in out


def test_default_private_sessions_policy_is_user_only(conn):
    """Pin the private-sessions policy copy in the default workspace CLAUDE.md
    (config/claude_md_template.txt): transcript upload is designed behavior,
    and marking a session private is exclusively the analyst's own deliberate
    action — the agent may SUGGEST `/agnes-private`, never invoke it. Guards
    against the copy drifting back to auto-marking guidance."""
    out = compute_default_claude_md(conn, user=_user(), server_url="https://example.com")
    assert "the product's designed behavior" in out
    assert "scrubbed client-side" in out
    assert "they type `/agnes-private` themselves" in out
    assert "SUGGEST the command" in out
    assert "never mark a session private" in out
    assert "run `agnes mark-private`" in out
    assert "auto-mark" not in out


def test_render_uses_default_when_no_override(conn):
    out = render_claude_md(conn, user=_user(), server_url="https://example.com")
    assert out.strip() != ""


# ---------------------------------------------------------------------------
# Override renders correctly
# ---------------------------------------------------------------------------


def test_render_uses_override_when_set(conn):
    ClaudeMdTemplateRepository(conn).set(
        "# {{ instance.name }} Workspace\n\nHello {{ user.email }}.",
        updated_by="admin@example.com",
    )
    out = render_claude_md(conn, user=_user("charlie@example.com"), server_url="https://example.com")
    assert "charlie@example.com" in out


def test_render_override_tables_list(conn):
    # Seed a table registry entry and ensure the test user is an admin so
    # RBAC filtering does not hide the table.
    conn.execute(
        "INSERT INTO table_registry (id, name, description, query_mode, source_type) "
        "VALUES ('t1', 'orders', 'All orders', 'local', 'keboola')"
    )
    from src.repositories.users import UserRepository
    from src.repositories.user_group_members import UserGroupMembersRepository

    UserRepository(conn).create(id="u1", email="alice@example.com", name="Alice")
    admin_gid = conn.execute("SELECT id FROM user_groups WHERE name='Admin'").fetchone()[0]
    UserGroupMembersRepository(conn).add_member("u1", admin_gid, source="admin")
    ClaudeMdTemplateRepository(conn).set(
        "{% for t in tables %}- {{ t.name }}: {{ t.description }}{% endfor %}",
        updated_by="admin@example.com",
    )
    out = render_claude_md(conn, user=_user(), server_url="https://example.com")
    assert "orders" in out
    assert "All orders" in out


def test_render_override_metrics_summary(conn):
    # Seed a metric definition — must include NOT NULL columns: display_name, sql
    conn.execute(
        "INSERT INTO metric_definitions (id, name, display_name, category, sql) "
        "VALUES ('m1', 'mrr', 'MRR', 'revenue', 'SELECT SUM(amount)')"
    )
    ClaudeMdTemplateRepository(conn).set(
        "Metrics: {{ metrics.count }}, cats: {{ metrics.categories | join(', ') }}",
        updated_by="admin@example.com",
    )
    out = render_claude_md(conn, user=_user(), server_url="https://example.com")
    assert "1" in out  # 1 metric
    assert "revenue" in out


# ---------------------------------------------------------------------------
# RBAC-filtered marketplaces — two users with different grants render differently
# ---------------------------------------------------------------------------


def test_marketplaces_empty_for_user_with_no_grants(conn):
    # No grants seeded — _marketplaces_for_user returns []
    ClaudeMdTemplateRepository(conn).set(
        "{% if marketplaces %}HAS_PLUGINS{% else %}NO_PLUGINS{% endif %}",
        updated_by="admin@example.com",
    )
    out = render_claude_md(conn, user=_user(), server_url="https://example.com")
    assert "NO_PLUGINS" in out


# ---------------------------------------------------------------------------
# Anonymous / minimal user context
# ---------------------------------------------------------------------------


def test_render_with_minimal_user_context(conn):
    """Templates referencing user fields must work with minimal user dict."""
    ClaudeMdTemplateRepository(conn).set(
        "User: {{ user.email }}, admin: {{ user.is_admin }}",
        updated_by="admin@example.com",
    )
    out = render_claude_md(conn, user=_user(), server_url="https://example.com")
    assert "alice@example.com" in out
    assert "False" in out


# ---------------------------------------------------------------------------
# Build context shape
# ---------------------------------------------------------------------------


def test_context_exposes_all_documented_keys(conn):
    ctx = build_claude_md_context(conn, user=_user(), server_url="https://example.com")
    for key in (
        "instance",
        "server",
        "sync_interval",
        "data_source",
        "tables",
        "metrics",
        "marketplaces",
        "user",
        "now",
        "today",
    ):
        assert key in ctx, f"missing context key: {key}"


def test_context_tables_is_list(conn):
    ctx = build_claude_md_context(conn, user=_user(), server_url="https://example.com")
    assert isinstance(ctx["tables"], list)


def test_context_metrics_shape(conn):
    ctx = build_claude_md_context(conn, user=_user(), server_url="https://example.com")
    assert "count" in ctx["metrics"]
    assert "categories" in ctx["metrics"]


def test_context_marketplaces_is_list(conn):
    ctx = build_claude_md_context(conn, user=_user(), server_url="https://example.com")
    assert isinstance(ctx["marketplaces"], list)


# ---------------------------------------------------------------------------
# Render failure raises (caller handles)
# ---------------------------------------------------------------------------


def test_render_raises_on_template_error(conn):
    ClaudeMdTemplateRepository(conn).set("{{ does_not_exist }}", updated_by="admin@example.com")
    with pytest.raises(TemplateError):
        render_claude_md(conn, user=_user(), server_url="https://example.com")


# ---------------------------------------------------------------------------
# RBAC-filtered tables — two users with different grants see different tables
# ---------------------------------------------------------------------------


def _make_user(conn, *, user_id: str, email: str) -> None:
    from src.repositories.users import UserRepository

    UserRepository(conn).create(id=user_id, email=email, name=email.split("@")[0])


def _make_group(conn, *, name: str) -> str:
    from src.repositories.user_groups import UserGroupsRepository

    return UserGroupsRepository(conn).create(name=name)["id"]


def _add_member(conn, *, user_id: str, group_id: str) -> None:
    from src.repositories.user_group_members import UserGroupMembersRepository

    UserGroupMembersRepository(conn).add_member(user_id, group_id, source="admin")


def _grant_table(conn, *, group_id: str, table_id: str) -> None:
    """Stack-gated RBAC: wrap ``table_id`` in an auto data_package and
    grant the package to ``group_id`` with ``requirement='required'``
    so every user in the group has the package in their stack."""
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.data_packages import DataPackagesRepository

    pkgs = DataPackagesRepository(conn)
    pkg_slug = f"_test-pkg-{table_id.lower()}"[:63]
    existing = pkgs.get_by_slug(pkg_slug)
    if existing:
        pkg_id = existing["id"]
    else:
        pkg_id = pkgs.create(
            name=f"Test wrap {table_id}",
            slug=pkg_slug,
            description=None,
            icon=None,
            color=None,
            created_by="test",
        )
    pkgs.add_table(pkg_id, table_id, added_by="test")
    ResourceGrantsRepository(conn).create(
        group_id=group_id,
        resource_type="data_package",
        resource_id=pkg_id,
        requirement="required",
    )


def test_render_tables_filtered_by_rbac(conn):
    """Non-admin users see only tables granted to their groups."""
    # Seed two tables
    conn.execute(
        "INSERT INTO table_registry (id, name, description, query_mode, source_type) "
        "VALUES ('t-a', 'orders', 'Order data', 'local', 'keboola')"
    )
    conn.execute(
        "INSERT INTO table_registry (id, name, description, query_mode, source_type) "
        "VALUES ('t-b', 'revenue', 'Revenue data', 'local', 'keboola')"
    )

    # Two users, two groups
    _make_user(conn, user_id="ua", email="alice@example.com")
    _make_user(conn, user_id="ub", email="bob@example.com")
    gid_a = _make_group(conn, name="group-a")
    gid_b = _make_group(conn, name="group-b")
    _add_member(conn, user_id="ua", group_id=gid_a)
    _add_member(conn, user_id="ub", group_id=gid_b)

    # Grant: group-a → t-a, group-b → t-b
    _grant_table(conn, group_id=gid_a, table_id="t-a")
    _grant_table(conn, group_id=gid_b, table_id="t-b")

    user_a = {"id": "ua", "email": "alice@example.com", "name": "Alice", "is_admin": False, "groups": []}
    user_b = {"id": "ub", "email": "bob@example.com", "name": "Bob", "is_admin": False, "groups": []}

    ctx_a = build_claude_md_context(conn, user=user_a, server_url="https://example.com")
    table_names_a = {t["name"] for t in ctx_a["tables"]}
    assert "orders" in table_names_a
    assert "revenue" not in table_names_a

    ctx_b = build_claude_md_context(conn, user=user_b, server_url="https://example.com")
    table_names_b = {t["name"] for t in ctx_b["tables"]}
    assert "revenue" in table_names_b
    assert "orders" not in table_names_b


def test_render_tables_admin_sees_all(conn):
    """Admin users see all tables regardless of grants."""
    conn.execute(
        "INSERT INTO table_registry (id, name, description, query_mode, source_type) "
        "VALUES ('t-x', 'alpha', 'Alpha table', 'local', 'keboola')"
    )
    conn.execute(
        "INSERT INTO table_registry (id, name, description, query_mode, source_type) "
        "VALUES ('t-y', 'beta', 'Beta table', 'local', 'keboola')"
    )

    # Admin user: member of the Admin system group
    _make_user(conn, user_id="u-admin", email="admin@example.com")
    admin_gid = conn.execute("SELECT id FROM user_groups WHERE name='Admin'").fetchone()[0]
    _add_member(conn, user_id="u-admin", group_id=admin_gid)

    user_admin = {"id": "u-admin", "email": "admin@example.com", "name": "Admin", "is_admin": True, "groups": []}
    ctx = build_claude_md_context(conn, user=user_admin, server_url="https://example.com")
    table_names = {t["name"] for t in ctx["tables"]}
    assert "alpha" in table_names
    assert "beta" in table_names


def test_render_tables_empty_for_user_with_no_grants(conn):
    """Non-admin with no grants sees no tables."""
    conn.execute(
        "INSERT INTO table_registry (id, name, description, query_mode, source_type) "
        "VALUES ('t-z', 'secret', 'Secret table', 'local', 'keboola')"
    )
    _make_user(conn, user_id="u-none", email="none@example.com")
    user_none = {"id": "u-none", "email": "none@example.com", "name": "None", "is_admin": False, "groups": []}
    ctx = build_claude_md_context(conn, user=user_none, server_url="https://example.com")
    assert ctx["tables"] == []


# ---------------------------------------------------------------------------
# RBAC-filtered metrics summary — same table-stack gate as GET /api/metrics
# ---------------------------------------------------------------------------


def _seed_metric(conn, *, metric_id: str, category: str, table_name: str) -> None:
    from src.repositories import metric_repo

    metric_repo().create(
        id=metric_id,
        name=metric_id,
        display_name=metric_id,
        category=category,
        sql="SELECT 1",
        table_name=table_name,
        source="manual",
    )


def test_metrics_summary_filtered_by_rbac(conn):
    """Non-admin users only see counts/categories for metrics whose table
    is in their stack — same gate as GET /api/metrics (app/api/metrics.py)."""
    conn.execute(
        "INSERT INTO table_registry (id, name, description, query_mode, source_type) "
        "VALUES ('t-a', 'orders', 'Order data', 'local', 'keboola')"
    )
    conn.execute(
        "INSERT INTO table_registry (id, name, description, query_mode, source_type) "
        "VALUES ('t-b', 'revenue', 'Revenue data', 'local', 'keboola')"
    )
    _seed_metric(conn, metric_id="m-orders", category="ops", table_name="orders")
    _seed_metric(conn, metric_id="m-revenue", category="finance", table_name="revenue")

    _make_user(conn, user_id="ua", email="alice@example.com")
    gid_a = _make_group(conn, name="group-a")
    _add_member(conn, user_id="ua", group_id=gid_a)
    _grant_table(conn, group_id=gid_a, table_id="t-a")

    user_a = {"id": "ua", "email": "alice@example.com", "name": "Alice", "is_admin": False, "groups": []}
    ctx = build_claude_md_context(conn, user=user_a, server_url="https://example.com")
    assert ctx["metrics"]["count"] == 1
    assert ctx["metrics"]["categories"] == ["ops"]


def test_metrics_summary_admin_sees_all(conn):
    """Admin users see the unfiltered metric count/categories."""
    conn.execute(
        "INSERT INTO table_registry (id, name, description, query_mode, source_type) "
        "VALUES ('t-a', 'orders', 'Order data', 'local', 'keboola')"
    )
    conn.execute(
        "INSERT INTO table_registry (id, name, description, query_mode, source_type) "
        "VALUES ('t-b', 'revenue', 'Revenue data', 'local', 'keboola')"
    )
    _seed_metric(conn, metric_id="m-orders", category="ops", table_name="orders")
    _seed_metric(conn, metric_id="m-revenue", category="finance", table_name="revenue")

    _make_user(conn, user_id="u-admin", email="admin@example.com")
    admin_gid = conn.execute("SELECT id FROM user_groups WHERE name='Admin'").fetchone()[0]
    _add_member(conn, user_id="u-admin", group_id=admin_gid)

    user_admin = {"id": "u-admin", "email": "admin@example.com", "name": "Admin", "is_admin": True, "groups": []}
    ctx = build_claude_md_context(conn, user=user_admin, server_url="https://example.com")
    assert ctx["metrics"]["count"] == 2
    assert ctx["metrics"]["categories"] == ["finance", "ops"]


def test_metrics_summary_no_table_metric_always_visible(conn):
    """A metric with no table_name/tables (nothing to gate) is not hidden."""
    _seed_metric(conn, metric_id="m-untabled", category="misc", table_name=None)

    _make_user(conn, user_id="u-none", email="none@example.com")
    user_none = {"id": "u-none", "email": "none@example.com", "name": "None", "is_admin": False, "groups": []}
    ctx = build_claude_md_context(conn, user=user_none, server_url="https://example.com")
    assert ctx["metrics"]["count"] == 1
    assert ctx["metrics"]["categories"] == ["misc"]


def _seed_semantic_model(conn, *, slug: str = "retail", status: str = "valid") -> dict:
    from src.repositories import semantic_model_repo

    return semantic_model_repo().upsert(
        id=f"manual/_/{slug}",
        slug=slug,
        name=slug,
        description=None,
        document="version: '0.2.0.dev0'\nsemantic_model:\n  - name: " + slug + "\n",
        document_json={"semantic_model": [{"name": slug, "datasets": [{"name": "orders", "source": "db.orders"}]}]},
        spec_version="0.2.0.dev0",
        content_hash=f"hash-{slug}",
        source="manual",
        source_ref=None,
        status=status,
        validation_errors=None,
        validated_at=None,
    )


def _admin_user(conn, *, user_id: str = "u-admin", email: str = "admin@example.com") -> dict:
    """Seed a real Admin-group member — `is_user_admin` (app/auth/access.py)
    checks DB group membership, not the `is_admin` field on the passed-in
    user dict, so a synthetic `{"is_admin": True}` dict alone is not enough
    (same pattern as `test_render_tables_admin_sees_all` above)."""
    _make_user(conn, user_id=user_id, email=email)
    admin_gid = conn.execute("SELECT id FROM user_groups WHERE name='Admin'").fetchone()[0]
    _add_member(conn, user_id=user_id, group_id=admin_gid)
    return {"id": user_id, "email": email, "name": "Admin", "is_admin": True, "groups": []}


class TestSemanticLayerSection:
    """`semantic_layer.has_models` — CLAUDE.md section gate (parity spec §4)."""

    def test_absent_without_any_semantic_model(self, conn):
        ctx = build_claude_md_context(conn, user=_admin_user(conn), server_url="https://example.com")
        assert ctx["semantic_layer"]["has_models"] is False

    def test_present_for_admin_when_a_valid_model_exists(self, conn):
        _seed_semantic_model(conn)
        ctx = build_claude_md_context(conn, user=_admin_user(conn), server_url="https://example.com")
        assert ctx["semantic_layer"]["has_models"] is True

    def test_absent_for_non_admin_with_no_grant(self, conn):
        """RBAC matches GET /api/semantic-models/search: a model with no
        linked package/direct grant is invisible to a non-admin."""
        _seed_semantic_model(conn)
        _make_user(conn, user_id="ua", email="alice@example.com")
        user = {"id": "ua", "email": "alice@example.com", "name": "Alice", "is_admin": False, "groups": []}
        ctx = build_claude_md_context(conn, user=user, server_url="https://example.com")
        assert ctx["semantic_layer"]["has_models"] is False

    def test_present_for_non_admin_via_direct_model_grant(self, conn):
        from src.repositories import resource_grants_repo, semantic_model_repo, user_groups_repo
        from src.repositories.user_group_members import UserGroupMembersRepository

        row = _seed_semantic_model(conn)
        _make_user(conn, user_id="ua", email="alice@example.com")
        gid = _make_group(conn, name="Semantic Readers")
        UserGroupMembersRepository(conn).add_member("ua", gid, source="test")
        resource_grants_repo().create(
            group_id=gid, resource_type="semantic_model", resource_id=row["id"], assigned_by="test"
        )
        user = {"id": "ua", "email": "alice@example.com", "name": "Alice", "is_admin": False, "groups": []}
        ctx = build_claude_md_context(conn, user=user, server_url="https://example.com")
        assert ctx["semantic_layer"]["has_models"] is True
        assert semantic_model_repo().get(row["id"]) is not None  # sanity: repo() reads the same seeded row
        assert user_groups_repo().get(gid) is not None

    def test_invalid_status_model_does_not_count(self, conn):
        _seed_semantic_model(conn, status="invalid")
        ctx = build_claude_md_context(conn, user=_admin_user(conn), server_url="https://example.com")
        assert ctx["semantic_layer"]["has_models"] is False

    def test_degrades_when_the_rbac_lookup_hits_a_missing_table(self, conn, monkeypatch):
        """A half-migrated DB (semantic_models present, but a resource_grants /
        data-package table the RBAC gate reads not yet created) degrades the
        gate to False, not an error — _can_read_model sits inside the same try
        that tolerates a missing semantic_models table (Devin review #1398)."""
        import app.api.semantic_models as sm_mod

        _seed_semantic_model(conn)  # a valid row, so list_all() reaches _can_read_model
        _make_user(conn, user_id="ua", email="alice@example.com")
        user = {"id": "ua", "email": "alice@example.com", "name": "Alice", "is_admin": False, "groups": []}

        def _boom(_u, _row, _c):
            raise duckdb.CatalogException("Table with name resource_grants does not exist!")

        monkeypatch.setattr(sm_mod, "_can_read_model", _boom)
        ctx = build_claude_md_context(conn, user=user, server_url="https://example.com")
        assert ctx["semantic_layer"]["has_models"] is False

    def test_rendered_section_present_with_models(self, conn):
        _seed_semantic_model(conn)
        out = render_claude_md(conn, user=_admin_user(conn), server_url="https://example.com")
        assert "## Semantic layer" in out
        assert "agnes semantic-model context" in out

    def test_rendered_section_absent_without_models(self, conn):
        out = render_claude_md(conn, user=_admin_user(conn), server_url="https://example.com")
        assert "## Semantic layer" not in out


# ---------------------------------------------------------------------------
# Vendor-neutral "Remote Queries" — BigQuery/Databricks content is gated on
# the instance actually having that engine (primary data_source.type or a
# registered table's source_type), so e.g. a Snowflake- or Keboola-backed
# instance never tells its agent about BigQuery dialects it doesn't have.
# ---------------------------------------------------------------------------


def _seed_table(conn, *, table_id: str, name: str, source_type: str, query_mode: str = "remote") -> None:
    conn.execute(
        "INSERT INTO table_registry (id, name, description, query_mode, source_type) VALUES (?, ?, ?, ?, ?)",
        [table_id, name, f"{name} table", query_mode, source_type],
    )


def _contiguous_pipe_runs(text: str) -> list[int]:
    """Lengths of maximal runs of consecutive markdown-table (`|`-prefixed)
    lines — a Jinja whitespace-control regression splits a table with blank
    lines, which markdown renders as broken fragments."""
    runs, current = [], 0
    for line in text.splitlines():
        if line.lstrip().startswith("|"):
            current += 1
        elif current:
            runs.append(current)
            current = 0
    if current:
        runs.append(current)
    return runs


def _failure_table_runs(out: str) -> list[int]:
    """Pipe-runs of just the failure-mode dictionary — the table whose rows
    are conditionally rendered, so the one a whitespace regression breaks."""
    import re

    m = re.search(r"### Failure-mode dictionary.*?(?=\n### )", out, re.S)
    assert m, "Failure-mode dictionary section not found"
    return _contiguous_pipe_runs(m.group(0))


def test_context_tables_include_source_type(conn):
    _seed_table(conn, table_id="t-sf", name="sf_orders", source_type="snowflake")
    ctx = build_claude_md_context(conn, user=_admin_user(conn), server_url="https://example.com")
    assert ctx["tables"][0]["source_type"] == "snowflake"


def test_context_source_types_reflect_rbac_visible_tables(conn):
    """data_source.source_types is derived from the tables the CALLER can see,
    so a user with no grant on the instance's only BigQuery table gets a
    prompt without BigQuery guidance."""
    _seed_table(conn, table_id="t-bq", name="events", source_type="bigquery")
    _seed_table(conn, table_id="t-kbc", name="orders", source_type="keboola", query_mode="local")

    _make_user(conn, user_id="ua", email="alice@example.com")
    gid = _make_group(conn, name="bq-readers")
    _add_member(conn, user_id="ua", group_id=gid)
    _grant_table(conn, group_id=gid, table_id="t-bq")

    user_a = {"id": "ua", "email": "alice@example.com", "name": "Alice", "is_admin": False, "groups": []}
    ctx = build_claude_md_context(conn, user=user_a, server_url="https://example.com")
    assert ctx["data_source"]["source_types"] == ["bigquery"]

    ctx_admin = build_claude_md_context(conn, user=_admin_user(conn), server_url="https://example.com")
    assert ctx_admin["data_source"]["source_types"] == ["bigquery", "keboola"]


def test_context_source_types_empty_without_tables(conn):
    ctx = build_claude_md_context(conn, user=_admin_user(conn), server_url="https://example.com")
    assert ctx["data_source"]["source_types"] == []


def test_default_remote_queries_vendor_neutral_without_bigquery(conn, monkeypatch):
    """A non-BigQuery instance (e.g. Keboola- or Snowflake-backed) renders the
    Remote Queries core with no BigQuery/Databricks content anywhere."""
    monkeypatch.setattr("src.claude_md.get_data_source_type", lambda: "local")
    _seed_table(conn, table_id="t-sf", name="sf_orders", source_type="snowflake")
    out = compute_default_claude_md(conn, user=_admin_user(conn), server_url="https://example.com")

    assert "## Remote Queries" in out
    assert "agnes snapshot create" in out
    assert "remote_scan_too_large" in out  # engine-shared cost/size gate stays in the core
    lowered = out.lower()
    assert "bigquery" not in lowered
    assert "bq" not in lowered
    assert "databricks" not in lowered
    assert "gcp" not in lowered

    # the failure-mode table survives the conditional rows as ONE table
    runs = _failure_table_runs(out)
    assert len(runs) == 1 and runs[0] >= 7, runs


def test_default_remote_queries_bigquery_blocks_when_primary_bq(conn, monkeypatch):
    monkeypatch.setattr("src.claude_md.get_data_source_type", lambda: "bigquery")
    out = compute_default_claude_md(conn, user=_admin_user(conn), server_url="https://example.com")
    assert "BigQuery SQL flavor" in out
    assert "cross_project_forbidden" in out
    assert "bq_path_not_registered" in out
    assert "personal GCP auth" in out


def test_default_remote_queries_bigquery_blocks_via_registered_table(conn, monkeypatch):
    """Multi-source instance: primary type 'local' but a BigQuery table is
    registered — the BigQuery guidance must still render."""
    monkeypatch.setattr("src.claude_md.get_data_source_type", lambda: "local")
    _seed_table(conn, table_id="t-bq", name="events", source_type="bigquery")
    out = compute_default_claude_md(conn, user=_admin_user(conn), server_url="https://example.com")
    assert "BigQuery SQL flavor" in out
    assert "cross_project_forbidden" in out
    # conditional rows render inside one contiguous failure-mode table
    runs = _failure_table_runs(out)
    assert len(runs) == 1 and runs[0] >= 9, runs


def test_default_remote_queries_databricks_block_gated(conn, monkeypatch):
    monkeypatch.setattr("src.claude_md.get_data_source_type", lambda: "local")
    _seed_table(conn, table_id="t-dbx", name="dbx_sales", source_type="databricks")
    out = compute_default_claude_md(conn, user=_admin_user(conn), server_url="https://example.com")
    assert "Databricks SQL flavor" in out
    assert "bigquery" not in out.lower()


def test_default_template_carries_no_legacy_strings():
    """The shipped default saved verbatim as an admin override must not trip
    the /admin/prompts stale-override banner (_LEGACY_STRINGS scan)."""
    from app.api.claude_md import _scan_legacy_strings
    from src.claude_md import _load_default_template

    assert _scan_legacy_strings(_load_default_template()) == []


def test_metrics_summary_degrades_when_table_registry_missing(conn, monkeypatch):
    """A half-migrated DB (metric_definitions present, table_registry not yet
    created) must degrade to an empty summary for a non-admin caller, not
    raise — the RBAC gate's table_registry lookup sits inside the same
    try/except that already tolerates a missing metric_definitions table."""
    import app.api.metrics as metrics_mod

    class _MissingTableRegistryRepo:
        def get_by_name(self, name):
            raise duckdb.CatalogException(f"Table with name {name} does not exist!")

    monkeypatch.setattr(metrics_mod, "table_registry_repo", lambda: _MissingTableRegistryRepo())

    _seed_metric(conn, metric_id="m-orders", category="ops", table_name="orders")

    _make_user(conn, user_id="u-none", email="none@example.com")
    user_none = {"id": "u-none", "email": "none@example.com", "name": "None", "is_admin": False, "groups": []}
    ctx = build_claude_md_context(conn, user=user_none, server_url="https://example.com")
    assert ctx["metrics"] == {"count": 0, "categories": []}
