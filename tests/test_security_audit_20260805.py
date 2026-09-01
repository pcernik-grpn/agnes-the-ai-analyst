"""Regression guards for the 2026-08-05 security audit.

Mirrors tests/test_security_audit_20260724.py: one test per finding, named after
it, so a refactor that reintroduces the hole fails with the finding id visible.
"""

import json

import pytest

# ── F-1: plugin name is a path segment; traversal must not survive ingest ──


@pytest.mark.parametrize(
    "name",
    [
        "..",
        ".",
        "../../../state",
        "analytics-tools/../../../state",
        "a/b",
        "a\\b",
        "\x00evil",
        " padded ",
        "trailing\n",
    ],
)
def test_f1_is_safe_plugin_name_rejects(name):
    from src.marketplace import is_safe_plugin_name

    assert is_safe_plugin_name(name) is False


@pytest.mark.parametrize("name", ["legit", "analytics-tools", "a.b_c-1"])
def test_f1_is_safe_plugin_name_accepts_plain_segments(name):
    from src.marketplace import is_safe_plugin_name

    assert is_safe_plugin_name(name) is True


def test_f1_is_safe_plugin_name_rejects_non_strings():
    from src.marketplace import is_safe_plugin_name

    assert is_safe_plugin_name(None) is False
    assert is_safe_plugin_name(42) is False
    assert is_safe_plugin_name({"name": "x"}) is False


def test_f1_read_plugins_drops_unsafe_names(tmp_path, monkeypatch):
    """A hostile marketplace.json must not put a traversing name into the DB."""
    import src.marketplace as mp

    root = tmp_path / "marketplaces"
    (root / "acme" / ".claude-plugin").mkdir(parents=True)
    (root / "acme" / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(
            {
                "plugins": [
                    {"name": "good-plugin"},
                    {"name": " padded-ok "},  # stripped form is safe → kept
                    {"name": "../../../state"},
                    {"name": "nested/plugin"},
                    {"name": ".."},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(mp, "get_marketplaces_dir", lambda: root)

    names = [p["name"] for p in mp.read_plugins("acme")]

    assert names == ["good-plugin", " padded-ok "]


# ── F-1 layer 2: containment at the path-construction site ──


def test_f1_contained_plugin_dir_rejects_escape(tmp_path):
    """A row that bypassed ingest (older Agnes, hand-edited DB) still can't escape."""
    from src.marketplace_filter import _contained_plugin_dir

    root = tmp_path / "marketplaces"
    (root / "acme" / "plugins").mkdir(parents=True)
    (tmp_path / "state").mkdir()

    assert _contained_plugin_dir(root, "acme", "../../../state") is None
    assert _contained_plugin_dir(root, "acme", "..") is None
    assert _contained_plugin_dir(root, "acme", "a/b") is None


def test_f1_contained_plugin_dir_accepts_plain_name(tmp_path):
    from src.marketplace_filter import _contained_plugin_dir

    root = tmp_path / "marketplaces"
    (root / "acme" / "plugins" / "legit").mkdir(parents=True)

    assert _contained_plugin_dir(root, "acme", "legit") == root / "acme" / "plugins" / "legit"


def test_f1_contained_plugin_dir_rejects_symlinked_segment(tmp_path):
    """A `plugins/<name>` that is itself a symlink out of the root is contained."""
    from src.marketplace_filter import _contained_plugin_dir

    root = tmp_path / "marketplaces"
    (root / "acme" / "plugins").mkdir(parents=True)
    outside = tmp_path / "state"
    outside.mkdir()
    (root / "acme" / "plugins" / "sneaky").symlink_to(outside)

    assert _contained_plugin_dir(root, "acme", "sneaky") is None


# ── F-1: third path — the v2 skills endpoint ──


def test_f1_v2_skills_path_is_contained(tmp_path, monkeypatch):
    """_skills_for_plugin must not read SKILL.md from outside the marketplaces root.

    This is the third construction of `<root>/<slug>/plugins/<name>` in the
    codebase; the audit found the two in marketplace_filter and missed this one.
    Its output is returned in an HTTP response body, so an escape here discloses
    file contents directly.
    """
    import app.api.v2_marketplace as v2

    root = tmp_path / "marketplaces"
    (root / "acme" / "plugins").mkdir(parents=True)
    # A SKILL.md that lives OUTSIDE the marketplaces root.
    outside = tmp_path / "elsewhere" / "skills" / "leak"
    outside.mkdir(parents=True)
    (outside / "SKILL.md").write_text("---\nname: leak\n---\nSECRET-BODY\n", encoding="utf-8")

    monkeypatch.setattr(v2, "get_marketplaces_dir", lambda: root)

    # Three `..` to climb plugins -> acme -> marketplaces -> tmp_path. Two would
    # land on <root>/elsewhere, which does not exist, and the test would pass
    # for the wrong reason.
    entries = v2._skills_for_plugin("acme", "../../../elsewhere")

    assert entries == [], f"escaped the marketplaces root: {entries!r}"


def test_f1c_v2_skills_does_not_follow_symlinks(tmp_path, monkeypatch):
    """The plugin dir is legitimately named and inside the root, so the
    containment check above passes — but a symlinked skill dir or SKILL.md
    still reaches outside. The packagers exclude symlinks; this endpoint puts
    the bytes straight into an HTTP response, so it is the worst place to
    follow one (Devin Review on #1180).
    """
    import app.api.v2_marketplace as v2

    root = tmp_path / "marketplaces"
    plugin = root / "acme" / "plugins" / "widget"
    (plugin / "skills").mkdir(parents=True)
    secret_dir = tmp_path / "elsewhere" / "leak"
    secret_dir.mkdir(parents=True)
    (secret_dir / "SKILL.md").write_text("---\nname: leak\n---\nSECRET-BODY\n", encoding="utf-8")

    # (a) the skill DIRECTORY is a symlink out of the tree
    (plugin / "skills" / "viadir").symlink_to(secret_dir, target_is_directory=True)
    # (b) a real skill dir whose SKILL.md is a symlink out of the tree
    real = plugin / "skills" / "viafile"
    real.mkdir()
    (real / "SKILL.md").symlink_to(secret_dir / "SKILL.md")

    monkeypatch.setattr(v2, "get_marketplaces_dir", lambda: root)

    entries = v2._skills_for_plugin("acme", "widget")

    assert entries == [], f"followed a symlink out of the plugin: {entries!r}"


def test_f1c_v2_skills_does_not_follow_a_symlinked_skills_dir():
    """The INTERMEDIATE component, which the two leaf checks could not see.

    The previous fix tested `skill_dir` and `skill_md` individually, so a curator
    repo shipping `plugins/<name>/skills -> /elsewhere` passed `is_dir()` and
    every real subdirectory below the link target was read and returned in the
    HTTP response. Two rounds of per-component checks each closed the case that
    had been pointed at and left the next one open; the walk now routes every
    path through `escapes_base`, which is one rule for all of them
    (Devin Review on #1183).
    """
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    import app.api.v2_marketplace as v2

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        root = tmp / "marketplaces"
        plugin = root / "acme" / "plugins" / "widget"
        plugin.mkdir(parents=True)

        # A perfectly ordinary skills tree — but somewhere else on the volume.
        outside = tmp / "elsewhere" / "skills"
        (outside / "leak").mkdir(parents=True)
        (outside / "leak" / "SKILL.md").write_text("---\nname: leak\n---\nSECRET-BODY\n", encoding="utf-8")

        # The plugin dir and its name are legitimate; only `skills` is a link.
        (plugin / "skills").symlink_to(outside, target_is_directory=True)

        with patch.object(v2, "get_marketplaces_dir", lambda: root):
            entries = v2._skills_for_plugin("acme", "widget")

        assert entries == [], f"followed a symlinked skills dir: {entries!r}"


# ── F-1b: hostile symlinks inside a legitimately-named plugin dir ──


def test_f1b_escapes_base_flags_symlink_and_outside_paths(tmp_path):
    from src.marketplace_filter import escapes_base

    root = tmp_path / "marketplaces"
    base = root / "acme" / "plugins" / "legit"
    base.mkdir(parents=True)
    secret = tmp_path / "system_secret.txt"
    secret.write_text("SUPER-SECRET", encoding="utf-8")

    plain = base / "README.md"
    plain.write_text("hi", encoding="utf-8")
    link = base / "evil.txt"
    link.symlink_to(secret)

    assert escapes_base(plain, [root]) is False
    assert escapes_base(link, [root]) is True
    assert escapes_base(secret, [root]) is True


def test_f1b_escapes_base_accepts_any_of_several_bases(tmp_path):
    """Multi-base: cowork_packager merges several source roots into one zip."""
    from src.marketplace_filter import escapes_base

    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    f = b / "SKILL.md"
    f.write_text("hi", encoding="utf-8")

    assert escapes_base(f, [a, b]) is False
    assert escapes_base(f, [a]) is True


def test_f1b_symlinked_plugin_dir_is_stopped_by_layer_a_not_layer_b(tmp_path):
    """The two layers divide the work; neither covers the other's case alone.

    Layer B (escapes_base) is anchored on the resolved plugin_dir, so it cannot
    see a plugin_dir that IS a symlink — resolving re-anchors it on the target.
    That case belongs to layer A (_contained_plugin_dir), which refuses to hand
    out such a plugin_dir at all. Asserting both halves here keeps a future
    refactor from dropping layer A on the assumption that layer B covers it.
    """
    from src.marketplace_filter import _contained_plugin_dir, escapes_base

    root = tmp_path / "marketplaces"
    (root / "acme" / "plugins").mkdir(parents=True)
    outside = tmp_path / "state"
    outside.mkdir()
    (outside / "system_secret.txt").write_text("SUPER-SECRET", encoding="utf-8")
    plugin_dir = root / "acme" / "plugins" / "sneaky"
    plugin_dir.symlink_to(outside)

    leaked = next(p for p in plugin_dir.rglob("*") if p.is_file())

    # Layer B, anchored on the resolved dir, does NOT catch this.
    assert escapes_base(leaked, [plugin_dir.resolve()]) is False
    # Layer A does — the plugin_dir is never produced in the first place.
    assert _contained_plugin_dir(root, "acme", "sneaky") is None


def test_f1b_zip_packager_skips_symlinked_files(tmp_path, monkeypatch):
    """A symlink in plugin content must not put out-of-tree bytes in the ZIP."""
    from app.marketplace_server import packager

    root = tmp_path / "marketplaces"
    plugin_dir = root / "acme" / "plugins" / "legit"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "README.md").write_text("hi", encoding="utf-8")
    secret = tmp_path / "system_secret.txt"
    secret.write_text("SUPER-SECRET", encoding="utf-8")
    (plugin_dir / "leak.txt").symlink_to(secret)

    members = packager._collect_members(
        [
            {
                "prefixed_name": "acme-legit",
                "manifest_name": "legit",
                "original_name": "legit",
                "marketplace_slug": "acme",
                "version": None,
                "raw": {},
                "plugin_dir": plugin_dir,
            }
        ],
        "deadbeef",
    )

    arcs = [arc for arc, _data, _x in members]
    payloads = b"".join(data for _arc, data, _x in members)
    assert not any(a.endswith("leak.txt") for a in arcs)
    assert b"SUPER-SECRET" not in payloads
    assert any(a.endswith("README.md") for a in arcs)


# ── F-2: the marketplace sync PAT must never hit argv or .git/config ──


def test_f2_no_authenticated_url_helper_remains():
    """The credential-in-URL builder is gone, not merely unused."""
    import src.marketplace as mp

    assert not hasattr(mp, "_authenticated_url")


def test_f2_git_env_carries_token_and_helper():
    from src.marketplace import _CREDENTIAL_HELPER, _git_env

    env = _git_env("SECRET123")

    assert env["AGNES_TOKEN"] == "SECRET123"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "AGNES_TOKEN" in _CREDENTIAL_HELPER
    assert "SECRET123" not in _CREDENTIAL_HELPER
    # No token → no AGNES_TOKEN leaked into the child env at all.
    assert "AGNES_TOKEN" not in _git_env(None)


def test_f2_scrub_strips_credentials_from_existing_config(tmp_path):
    """Instances that synced before the fix get their .git/config cleaned."""
    import subprocess

    from src.marketplace import _scrub_credentialed_remote

    repo = tmp_path / "acme"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "add",
            "origin",
            "https://x-access-token:SECRET123@example.com/acme.git",
        ],
        check=True,
    )
    assert "SECRET123" in (repo / ".git" / "config").read_text(encoding="utf-8")

    _scrub_credentialed_remote(repo, "https://example.com/acme.git")

    config = (repo / ".git" / "config").read_text(encoding="utf-8")
    assert "SECRET123" not in config
    assert "https://example.com/acme.git" in config


def test_f2_scrub_runs_even_when_ref_validation_rejects_the_row(tmp_path, monkeypatch):
    """A row with a malformed ref still gets its pre-fix credential scrubbed.

    The ValueError is raised before the sync body, so a scrub placed inside that
    body would never run for such a row — leaving the PAT on disk until an admin
    noticed. Ordering matters here, hence the test.
    """
    import subprocess

    import src.marketplace as mp

    root = tmp_path / "marketplaces"
    repo = root / "acme"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "add",
            "origin",
            "https://x-access-token:SECRET123@example.com/acme.git",
        ],
        check=True,
    )
    monkeypatch.setattr(mp, "get_marketplaces_dir", lambda: root)

    with pytest.raises(ValueError):
        mp._sync_spec({"id": "acme", "url": "https://example.com/acme.git", "ref": "bad..ref"})

    assert "SECRET123" not in (repo / ".git" / "config").read_text(encoding="utf-8")


# ── F-4: every quoted SQL identifier routes through quote_ident ──

# The SHAPE of a hand-quoted identifier: a double quote that lives *inside* a
# string literal and wraps an interpolation — `f'"{col}"'`, `f"… \"{col}\" …"`.
# The negative lookbehind drops the one false family the shape alone can't
# distinguish: an f-string whose entire body is one expression (`f"{value}"`),
# where the quotes are the literal's own delimiters.
#
# This is deliberately POSITION-BLIND, and that is the whole point. Two earlier
# versions of this guard keyed off where the quote sat — first "a keyword before
# it", then "a keyword or a dot before it" — and each time the emptiness of the
# allowlist was an artifact of not looking:
#   * `DROP VIEW IF EXISTS "{name}"` — ` IF EXISTS ` sits between the keyword and
#     the quote, so the keyword branch never fired. Four live sites.
#   * `f'"{col}" = ?'` — a predicate column with no keyword and no dot anywhere
#     near it. That one was a real injection: the allow-list upstream only
#     checked that the column EXISTS (`DESCRIBE` output), and for a
#     collection-ingested table the column name is whatever an uploaded file's
#     header said, unsanitized (src/ingest/tabular.py COPYs the reader's output
#     straight to parquet). A header of `x") OR 1=1 --` passed the allow-list and
#     then broke out of the quotes.
#   * Five more sites built the quoted fragment on its own line
#     (`", ".join(f'"{c}"' …)`), so no content heuristic — not "a SQL verb
#     somewhere on the line" either — can see them. Position and content are
#     both dead ends; only the shape is reliable.
#
# The cost of scanning by shape is that legitimate non-SQL uses of the same shape
# now surface, so the allowlist below is NOT empty — it is a list of families,
# each with the reason it is not a SQL identifier. An honest allowlist beats a
# vacuously empty one: a new unexplained `"{…}"` fails this test.
_QUOTED_IDENT_SHAPE_RE = r'(?<![fFrRbB])\\?"\{[^{}"]*\}\\?"'

# (family regex, why this shape is not a SQL identifier). A hit is excused only
# if it matches one of these.
_NON_SQL_QUOTED_FAMILIES: list[tuple[str, str]] = [
    (r"require_resource_access\(", 'FastAPI path-template argument, e.g. "{collection_id}"'),
    (
        r"require_collection_access\(",
        'FastAPI path-template argument — the COLLECTION-scoped wrapper of require_resource_access, same "{collection_id}" placeholder',
    ),
    (r'(?i)content-disposition|filename="\{', "HTTP header: quotes are RFC 6266 filename syntax"),
    (r"(?i)etag", "HTTP entity tag: quotes are part of the ETag grammar (RFC 7232)"),
    (r'"\{\}"', "empty JSON object as a literal default / json.loads fallback"),
    (r'\.replace\("\{', "template placeholder substitution, not SQL"),
    (
        r'"\{server_url\}"',
        "install-prompt click-time placeholder literal (compared/filtered as a string), not SQL",
    ),
    (r'<\w+[^>]*="\{', "HTML/XML attribute value"),
    (r"launch_cmd", "shell/batch command quoting in the launcher shortcut"),
    (r'", "\.join\(f\'"\{n\}": \{n\}\'', "building a Python dict literal for generated code"),
    (r'agnes search "\{', "CLI help text showing a quoted search term"),
    (r'label = f\'"\{query\}"\'', "CLI display label for a quoted search term"),
    (r"return f'\"\{escaped\}\"'", "dotenv value quoting (see the escaping above it)"),
    (r"vocab_list", "LLM prompt: a quoted vocabulary list, never executed"),
    (r"header_line", "CSV header row, not SQL"),
    (r'project = "\{jira_project\}"', "JQL string literal, not a SQL identifier"),
    (r'database "\{dbname\}" does not exist', "substring match against a Postgres error message"),
    (
        r'No MCP server named "\{',
        "substring match against the `claude mcp get` not-found message — the quotes are that "
        "CLI's output format, and the value interpolated is our own module constant",
    ),
    (r"^src/sql_ident\.py:", "the module's own docstrings, which quote the shape they forbid"),
    (r'server_default="\{\}"', "Alembic/SQLAlchemy column default of an empty JSON object"),
    (r'env: str = "\{\}"', "repository signature default of an empty JSON object"),
    (r'#.*"\{', "comment describing the shape"),
    (r'resource_metadata="\{', "WWW-Authenticate challenge parameter (RFC 9728)"),
    (r'"\{token\}"', "welcome-template placeholder, substituted at render time"),
    (r"^app/web/setup_instructions\.py:", "copy-paste CLI instructions and their placeholder tokens"),
]


def test_f4_no_bare_quoted_identifier_interpolation():
    """Ratchet: every hand-quoted identifier is either gone or explained.

    ``-P``, not ``-E``. git grep's ``-E`` is POSIX ERE, where ``\\s`` degrades to
    a literal ``s`` — the ``-E`` form of this pattern matches NOTHING and the
    test would pass vacuously forever. That is not hypothetical: it is how this
    guard was first written during the 2026-08-05 audit follow-up, and it was
    caught in review rather than by the test.
    """
    import re
    import subprocess

    proc = subprocess.run(
        ["git", "grep", "-nP", _QUOTED_IDENT_SHAPE_RE, "--", "*.py"],
        capture_output=True,
        text=True,
    )
    # git grep emits repo-relative paths with NO leading slash, so a
    # `"/tests/" not in ln` filter would never fire.
    hits = [ln for ln in proc.stdout.splitlines() if ln and not ln.startswith("tests/")]

    compiled = [(re.compile(pat), reason) for pat, reason in _NON_SQL_QUOTED_FAMILIES]
    unexplained = [ln for ln in hits if not any(rx.search(ln) for rx, _ in compiled)]

    assert unexplained == [], (
        "hand-quoted SQL identifier(s) — route through quote_ident, or add a family to "
        "_NON_SQL_QUOTED_FAMILIES with the reason it is not an identifier:\n" + "\n".join(unexplained)
    )


def test_f4_no_dead_exemption_families():
    """Shrinks-only: an exemption family that matches nothing must be deleted.

    Without this, the allowlist rots into a list of shapes nobody uses, and the
    next reader can't tell which entries are load-bearing.
    """
    import re
    import subprocess

    proc = subprocess.run(
        ["git", "grep", "-nP", _QUOTED_IDENT_SHAPE_RE, "--", "*.py"],
        capture_output=True,
        text=True,
    )
    hits = [ln for ln in proc.stdout.splitlines() if ln and not ln.startswith("tests/")]

    dead = [pat for pat, _ in _NON_SQL_QUOTED_FAMILIES if not any(re.search(pat, ln) for ln in hits)]

    assert dead == [], "exemption family matches nothing — delete it from _NON_SQL_QUOTED_FAMILIES:\n" + "\n".join(dead)


def test_f4_quote_ident_reexported_from_profiler():
    """Existing `from src.profiler import quote_ident` importers keep working."""
    from src.profiler import quote_ident as from_profiler
    from src.sql_ident import quote_ident as canonical

    assert from_profiler is canonical
    assert canonical('a") AS x, (SELECT 1) AS "b') == '"a"") AS x, (SELECT 1) AS ""b"'


# ── F-4b: server-supplied manifest ids are path segments AND SQL identifiers ──


def test_f4b_safe_id_re_rejects_traversal_and_injection():
    """Two regexes, deliberately: only table ids may carry a dot.

    The first version of F-4b widened the single shared ``_SAFE_ID_RE`` so a
    dotted table id (``orders.v2``) would pass. That also relaxed the three
    consumers that have nothing to do with table ids — knowledge corpus id,
    digest slug, memory item id — each of which is spliced into a request path
    (``/api/knowledge/artifacts/{cid}/download``), where a value like ``..``
    silently normalizes away and the request hits a different endpoint. None of
    those three has a legitimate dotted spelling, so the dot-tolerance belongs
    to table ids alone. Caught in review on #1181.
    """
    from cli.lib.pull import _SAFE_ID_RE, _SAFE_TABLE_ID_RE

    # Shared/strict: no dots at all, so no dot-based path trickery downstream.
    assert _SAFE_ID_RE.match("corpus_v2")
    assert not _SAFE_ID_RE.match("orders.v2")
    assert not _SAFE_ID_RE.match("..")
    assert not _SAFE_ID_RE.match("../../.ssh/authorized_keys")
    assert not _SAFE_ID_RE.match('x" AS y, (SELECT 1) AS "z')
    assert not _SAFE_ID_RE.match("a/b")

    # Table ids: dots are legitimate — admin-derived ids carry them.
    assert _SAFE_TABLE_ID_RE.match("orders_v2")
    assert _SAFE_TABLE_ID_RE.match("orders.v2")
    assert not _SAFE_TABLE_ID_RE.match("../../.ssh/authorized_keys")
    assert not _SAFE_TABLE_ID_RE.match('x" AS y, (SELECT 1) AS "z')
    assert not _SAFE_TABLE_ID_RE.match("a/b")
    # The charset alone admits these; _safe_manifest_tables rejects them
    # separately (leading dot), which the next test pins.
    assert _SAFE_TABLE_ID_RE.match("..")


def test_f4b_pull_skips_unsafe_manifest_table_ids():
    """An unsafe id is dropped, and the drop must NOT become a pull error.

    cli/commands/pull.py raises typer.Exit(1) on a non-empty result.errors —
    including from the --quiet SessionStart hook path — so collecting these
    would turn one odd id into a permanently red `agnes pull`.
    """
    from cli.lib.pull import _safe_manifest_tables

    kept, dropped = _safe_manifest_tables(
        {
            "orders": {"hash": "a"},
            "orders.v2": {"hash": "b"},
            "../../../etc/passwd": {"hash": "c"},
            "..": {"hash": "d"},
            'x" AS y': {"hash": "e"},
        }
    )

    assert sorted(kept) == ["orders", "orders.v2"]
    assert sorted(dropped) == sorted(["../../../etc/passwd", "..", 'x" AS y'])


def test_f2b_origin_refusal_never_echoes_a_credential():
    """The refusal path reports the origin URL, and the value it reports is
    exactly what this release removes: a remote with an embedded PAT. Redacting
    against the *currently resolved* token is not enough — by then the env var
    may be unset or the PAT rotated, making redaction a no-op and persisting the
    secret into `marketplace_registry.last_error`, which the admin UI renders
    (Devin Review on #1180).
    """
    from src.marketplace import _strip_userinfo

    creds = "https://x-access-token:ghp_SUPERSECRET@github.com/acme/repo.git"
    out = _strip_userinfo(creds)
    assert "ghp_SUPERSECRET" not in out
    assert "x-access-token" not in out
    assert out == "https://github.com/acme/repo.git", out

    # Still useful: the host survives, so the operator can see WHERE it points.
    assert _strip_userinfo("https://user:pw@host:8443/a/b") == "https://host:8443/a/b"
    # And harmless shapes pass through untouched.
    assert _strip_userinfo("https://github.com/acme/repo.git") == "https://github.com/acme/repo.git"
    assert _strip_userinfo("not a url") == "not a url"
