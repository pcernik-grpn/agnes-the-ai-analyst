"""Task 6 -- the AST rewrite that substitutes a policied table's resolved
relation into a caller's SQL on every read surface that has a SQL tree to
walk (table access policies design doc §5.2, §16, §19).

Pure unit tests against a FAKE ``resolve`` callable -- deliberately not the
real ``policied_relation`` (Task 5's own contract test,
``tests/test_access_policy_resolver.py``, already covers that end) -- so
this module does not depend on live registry rows, per the plan's Task 6
instruction.
"""

from __future__ import annotations

import inspect

import pytest

from src.access_policy import (
    PoliciedRelation,
    PolicyError,
    PolicyNameCollision,
    PolicyUnknownTable,
    policied_relation,
    rewrite_sql,
)

SOLO_USER = {"id": "u_solo", "email": "solo@example.com"}

# A distinctive, parenthesizable body -- its exact text must survive the
# rewrite verbatim (never re-parsed/regenerated -- see rewrite_sql's
# docstring on the ``list_contains`` -> ``ARRAY_CONTAINS`` drift risk).
POLICY_SQL = "SELECT id, amount FROM invoices WHERE list_contains($user_groups, cost_center)"


def fake_resolver_policying(target_name: str, *, relation_sql: str = POLICY_SQL, table_id: str | None = None):
    """A ``resolve=`` double good enough to exercise ``rewrite_sql`` without
    a registry: ``policied=True`` for exactly ``target_name`` (matched
    case-insensitively, mirroring DuckDB folding unquoted identifiers),
    ``policied=False`` passthrough for every other name -- the same two
    outcomes ``policied_relation`` itself returns for "policy attached" vs.
    "registered but no policy".
    """
    resolved_id = table_id or target_name

    def _resolve(name: str, principal) -> PoliciedRelation:
        if name.lower() == target_name.lower():
            return PoliciedRelation(
                relation_sql=relation_sql,
                params={"user_groups": ["Finance"]},
                policied=True,
                table_id=resolved_id,
            )
        return PoliciedRelation(relation_sql=f"SELECT * FROM {name}", params={}, policied=False, table_id=name)

    return _resolve


def resolver_raising_for_unknown(target_name: str):
    """Like :func:`fake_resolver_policying`, but any OTHER name raises
    ``PolicyUnknownTable`` instead of a passthrough -- the shape a REAL
    ``policied_relation`` call takes for a name that is not a registered
    table id or name at all (``_resolve_table_row``'s "neither resolving").
    """

    def _resolve(name: str, principal) -> PoliciedRelation:
        if name.lower() == target_name.lower():
            return PoliciedRelation(
                relation_sql=POLICY_SQL, params={"user_groups": ["Finance"]}, policied=True, table_id=target_name
            )
        raise PolicyUnknownTable(name)

    return _resolve


# §19: unqualified, 2- and 3-part qualified, aliased, uppercase, quoted,
# CTE-nested, nested subquery, LATERAL, UNION ALL BY NAME, DuckDB FROM-first,
# comment-interleaved. Every fixture pairs the policied table with a
# non-policied sibling ("dim") so "left untouched" is actually exercised,
# not merely assumed. ``alias_marker`` is the exact ``AS ...`` text rule 2
# requires for that fixture's aliasing shape.
REWRITE_FIXTURES = [
    pytest.param("SELECT * FROM invoices, dim", "AS invoices", id="unqualified"),
    pytest.param("SELECT * FROM main.invoices JOIN dim d ON d.k = invoices.k", "AS invoices", id="two_part_qualified"),
    pytest.param("SELECT * FROM analytics.main.invoices i JOIN dim d ON d.k = i.k", "AS i", id="three_part_qualified"),
    pytest.param("SELECT * FROM invoices i JOIN dim d ON d.k = i.k", "AS i", id="aliased"),
    pytest.param("SELECT * FROM INVOICES JOIN dim d ON d.k = INVOICES.k", "AS INVOICES", id="uppercase"),
    pytest.param('SELECT * FROM "invoices" JOIN dim d ON d.k = invoices.k', 'AS "invoices"', id="quoted"),
    pytest.param(
        "WITH recent AS (SELECT * FROM invoices WHERE d > 1) SELECT * FROM recent JOIN dim ON TRUE",
        "AS invoices",
        id="cte_nested",
    ),
    pytest.param("SELECT * FROM (SELECT * FROM invoices) sub JOIN dim ON TRUE", "AS invoices", id="nested_subquery"),
    pytest.param("SELECT * FROM invoices i, LATERAL (SELECT * FROM dim WHERE dim.k = i.k) sub", "AS i", id="lateral"),
    pytest.param("SELECT * FROM invoices UNION ALL BY NAME SELECT * FROM dim", "AS invoices", id="union_all_by_name"),
    pytest.param("SELECT * FROM (FROM invoices) i, dim", "AS invoices", id="duckdb_from_first"),
    pytest.param(
        "SELECT * /* x */ FROM invoices -- c\n JOIN dim d ON d.k = invoices.k",
        "AS invoices",
        id="comment_interleaved",
    ),
]


class TestRewriteFixtures:
    """§19's rewrite fixture list: the policied table is wrapped in
    ``(<relation_sql>) AS <alias>`` with the alias rule 2 dictates, the
    substitution is reported in ``policied_table_ids``, the referenced
    ``$user_groups`` value is bound in ``params``, and the non-policied
    sibling table ``dim`` is left syntactically untouched (never itself
    reported as substituted)."""

    @pytest.mark.parametrize("sql, alias_marker", REWRITE_FIXTURES)
    def test_policied_table_wrapped_alias_preserved_sibling_untouched(self, sql, alias_marker):
        out, params, ids = rewrite_sql(sql, SOLO_USER, resolve=fake_resolver_policying("invoices"))

        assert f"({POLICY_SQL})" in out
        assert alias_marker in out
        assert ids == ["invoices"]
        assert params == {"user_groups": ["Finance"]}
        assert "dim" in out.lower()


class TestNameCollision:
    """Rule 4: a caller-introduced name that shadows a policied table's name
    is refused, whether the shadow is a CTE alias or a subquery/derived-table
    alias -- checked on both node shapes (§5.2)."""

    def test_cte_alias_collision_raises(self):
        with pytest.raises(PolicyNameCollision) as exc_info:
            rewrite_sql(
                "WITH invoices AS (SELECT 1) SELECT * FROM invoices",
                SOLO_USER,
                resolve=fake_resolver_policying("invoices"),
            )
        assert exc_info.value.table_id == "invoices"

    def test_cte_alias_collision_raises_even_when_cte_body_reads_the_real_table(self):
        """The single most common analyst idiom (§16): the CTE's own body
        legitimately reads the real table, but the outer reference is
        ambiguous -- refused outright, not disambiguated."""
        with pytest.raises(PolicyNameCollision) as exc_info:
            rewrite_sql(
                "WITH invoices AS (SELECT * FROM invoices WHERE amount > 10) SELECT * FROM invoices",
                SOLO_USER,
                resolve=fake_resolver_policying("invoices"),
            )
        assert exc_info.value.table_id == "invoices"

    def test_subquery_alias_collision_raises(self):
        """``SELECT * FROM t AS x, (SELECT 1) invoices`` (§5.2): the
        shadowing name lives on an ``exp.Subquery`` alias, with no
        ``exp.Table`` named "invoices" anywhere in the query -- collision
        detection must not depend on one existing."""
        with pytest.raises(PolicyNameCollision) as exc_info:
            rewrite_sql(
                "SELECT * FROM t AS x, (SELECT 1) invoices",
                SOLO_USER,
                resolve=fake_resolver_policying("invoices"),
            )
        assert exc_info.value.table_id == "invoices"

    def test_collision_on_a_non_policied_table_name_is_not_raised(self):
        """A CTE or subquery alias that merely coincides with a table's name
        is only a problem if that table is actually policied -- otherwise
        there is nothing to misdirect."""
        out, params, ids = rewrite_sql(
            "WITH dim AS (SELECT 1) SELECT * FROM dim, invoices",
            SOLO_USER,
            resolve=fake_resolver_policying("invoices"),
        )
        assert ids == ["invoices"]


class TestUnparseable:
    """Rule 3: unparseable SQL that references a policied table is rejected
    (never passed through unfiltered); unparseable SQL that references none
    is unaffected -- today's behavior is not allowed to regress just because
    sqlglot lags DuckDB's grammar (§19's ``SAMPLE 50%`` tripwire)."""

    def test_unparseable_sql_referencing_a_policied_table_raises_policy_error(self):
        with pytest.raises(PolicyError) as exc_info:
            rewrite_sql("SELECT * FROM invoices SAMPLE 50%", SOLO_USER, resolve=fake_resolver_policying("invoices"))
        assert exc_info.value.table_id == "invoices"

    def test_unparseable_sql_referencing_no_policied_table_is_unchanged(self):
        sql = "SELECT * FROM dim SAMPLE 50%"
        out, params, ids = rewrite_sql(sql, SOLO_USER, resolve=fake_resolver_policying("invoices"))
        assert out == sql
        assert params == {}
        assert ids == []


class TestNonRecursion:
    """Rule 1: applied exactly once. The policy body's own ``FROM invoices``
    is the base relation and must not itself be found-and-wrapped again."""

    def test_policy_body_is_not_rewritten(self):
        out, _params, _ids = rewrite_sql(
            "SELECT * FROM invoices", SOLO_USER, resolve=fake_resolver_policying("invoices")
        )
        # The relation body is inserted exactly once, and the "invoices"
        # inside it (the policy's own base table) is not itself matched and
        # wrapped a second time.
        assert out.count("list_contains($user_groups, cost_center)") == 1
        assert out.count("SELECT id, amount FROM invoices") == 1


class TestQueryWithNoPoliciedTableIsInert:
    """Enforcement is inert until a policy is attached (plan Architecture):
    a query that touches no policied table is returned byte-for-byte
    unchanged, not merely semantically equivalent."""

    def test_byte_identical_passthrough(self):
        sql = "SELECT * FROM dim d JOIN other o ON o.k = d.k"
        out, params, ids = rewrite_sql(sql, SOLO_USER, resolve=fake_resolver_policying("invoices"))
        assert out == sql
        assert params == {}
        assert ids == []


class TestUnregisteredNameIsSwallowed:
    """A table-shaped name that resolves to no registered table at all
    (``PolicyError`` from ``resolve`` -- ``_resolve_table_row``'s own
    signal) is not this function's concern: a query mentioning
    ``information_schema.tables`` or similar keeps working."""

    def test_unregistered_reference_left_untouched(self):
        out, params, ids = rewrite_sql(
            "SELECT * FROM invoices i, information_schema.tables t",
            SOLO_USER,
            resolve=resolver_raising_for_unknown("invoices"),
        )
        assert ids == ["invoices"]
        assert "information_schema" in out.lower()


class TestMultiplePoliciedTables:
    """Params from every substituted relation are unioned; ids are reported
    once per DISTINCT table, in first-encountered order, even across a
    self-join of the same policied table."""

    @staticmethod
    def _two_table_resolver(name: str, principal) -> PoliciedRelation:
        if name.lower() == "invoices":
            return PoliciedRelation(
                "SELECT * FROM invoices WHERE list_contains($user_groups, unit)",
                {"user_groups": ["Finance"]},
                True,
                "invoices",
            )
        if name.lower() == "contracts":
            return PoliciedRelation(
                "SELECT * FROM contracts WHERE owner_email = $user_email",
                {"user_email": "solo@example.com"},
                True,
                "contracts",
            )
        return PoliciedRelation(f"SELECT * FROM {name}", {}, False, name)

    def test_two_different_policied_tables_merge_params_and_list_both_ids(self):
        out, params, ids = rewrite_sql(
            "SELECT * FROM contracts c JOIN invoices i ON i.contract_id = c.id",
            SOLO_USER,
            resolve=self._two_table_resolver,
        )
        assert ids == ["contracts", "invoices"]
        assert params == {"user_email": "solo@example.com", "user_groups": ["Finance"]}

    def test_self_join_of_the_same_policied_table_deduplicates_ids(self):
        out, params, ids = rewrite_sql(
            "SELECT * FROM invoices a JOIN invoices b ON a.id = b.parent_id",
            SOLO_USER,
            resolve=self._two_table_resolver,
        )
        assert ids == ["invoices"]
        assert out.count("list_contains($user_groups, unit)") == 2


class TestDefaultResolverIsPoliciedRelation:
    """Production callers (Task 7/11) call ``rewrite_sql(sql, principal)``
    with no ``resolve=`` override at all -- the default must be the real
    resolver, not merely something that behaves like it in tests."""

    def test_default_resolve_parameter_is_policied_relation(self):
        assert inspect.signature(rewrite_sql).parameters["resolve"].default is policied_relation


class TestSecurityRefusalIsNeverSwallowed:
    """#1979's fail-OPEN bug: ``resolve`` has TWO failure modes and only one
    of them is "not this function's concern".

    ``policied_relation`` signals "no registered table answers to this name"
    with ``PolicyUnknownTable`` -- a CTE alias, an ``information_schema``
    view -- and every OTHER resolution failure (a transpile error, an
    identity variable in pattern position, a policy body that no longer
    parses) with a plain ``PolicyError``. Swallowing the second kind leaves
    the table reference UNSUBSTITUTED, so the caller reads the raw base view
    with a 200: a policy refusal turned into a policy bypass, on the primary
    query surface. Only the ``PolicyUnknownTable`` arm may be swallowed.
    """

    @staticmethod
    def _refusing_resolver(name: str, principal) -> PoliciedRelation:
        """``invoices`` is registered and policied but REFUSED; every other
        name is genuinely unregistered."""
        if name.lower() == "invoices":
            raise PolicyError("tbl_invoices")
        raise PolicyUnknownTable(name)

    def test_unknown_table_is_a_policy_error_subclass(self):
        # So every existing `except PolicyError` handler (which maps to a
        # structured, fail-closed response) keeps behaving exactly as before.
        assert issubclass(PolicyUnknownTable, PolicyError)

    def test_a_refusal_on_a_policied_table_propagates(self):
        with pytest.raises(PolicyError) as exc_info:
            rewrite_sql("SELECT * FROM invoices, dim", SOLO_USER, resolve=self._refusing_resolver)
        assert not isinstance(exc_info.value, PolicyUnknownTable)
        assert exc_info.value.table_id == "tbl_invoices"

    def test_a_refusal_propagates_from_the_unparseable_scan_too(self):
        # Rule 3's best-effort token scan swallowed the same conflated type,
        # so an unparseable statement naming a REFUSED table returned
        # unchanged -- and then ran, unfiltered.
        with pytest.raises(PolicyError) as exc_info:
            rewrite_sql("SELECT * FROM invoices SAMPLE 50%", SOLO_USER, resolve=self._refusing_resolver)
        assert exc_info.value.table_id == "tbl_invoices"

    def test_an_unknown_name_is_still_swallowed(self):
        sql = "SELECT * FROM information_schema.tables t"
        out, params, ids = rewrite_sql(sql, SOLO_USER, resolve=self._refusing_resolver)
        assert out == sql
        assert params == {}
        assert ids == []

    def test_a_cte_alias_that_is_not_a_registered_table_still_resolves_benignly(self):
        # The commonest analyst idiom: every name in it -- the CTE alias and
        # the real table -- goes through `resolve`, and only the unregistered
        # one may be swallowed.
        sql = "WITH recent AS (SELECT * FROM dim) SELECT * FROM recent"
        out, params, ids = rewrite_sql(sql, SOLO_USER, resolve=resolver_raising_for_unknown("invoices"))
        assert out == sql
        assert ids == []
