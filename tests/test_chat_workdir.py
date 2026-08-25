"""Tests for WorkdirManager — per-user workspace + per-session dir + reinit.

Fixture note: the plan's spec names ``open_db`` / ``migrate`` but those don't
exist in src/db.py.  The real equivalents (same pattern as
tests/test_chat_persistence.py) are:
  - ``duckdb.connect(":memory:")``   to open an in-memory connection
  - ``_ensure_schema(conn)``         to migrate it to the current version
"""

from pathlib import Path

import duckdb
import pytest

from src.db import _ensure_schema

from app.chat.persistence import ChatRepository
from app.chat.workdir import WorkdirManager


@pytest.fixture
def workdir_mgr(tmp_path: Path) -> WorkdirManager:
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "CLAUDE.md").write_text("default")
    (bundled / ".claude").mkdir()
    (bundled / ".claude" / "settings.json").write_text("{}")
    return WorkdirManager(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        server_url="https://agnes.example",
        agnes_version="0.55.0",
        get_marketplace_sha=lambda: "mkt-sha-1",
        get_template_status=lambda: None,  # no override template
    )


def test_user_workspace_path_isolated(workdir_mgr: WorkdirManager):
    a = workdir_mgr.user_workspace("a@x")
    b = workdir_mgr.user_workspace("b@x")
    assert a != b
    assert a.name == "workspace"
    assert b.name == "workspace"


def test_ensure_user_workdir_initializes_once(workdir_mgr: WorkdirManager, tmp_path: Path):
    ws = workdir_mgr.ensure_user_workdir("u@x")
    assert (ws / "CLAUDE.md").read_text() == "default"
    assert (ws / ".claude/init-complete").exists()
    # second call is a no-op (marketplace SHA unchanged, sentinel present)
    (ws / "CLAUDE.md").write_text("edited")
    ws2 = workdir_mgr.ensure_user_workdir("u@x")
    assert (ws2 / "CLAUDE.md").read_text() == "edited"  # not clobbered


def test_needs_reinit_on_marketplace_sha_change(workdir_mgr: WorkdirManager):
    workdir_mgr.ensure_user_workdir("u@x")
    assert workdir_mgr.needs_reinit("u@x") is False
    workdir_mgr._get_marketplace_sha = lambda: "mkt-sha-2"
    assert workdir_mgr.needs_reinit("u@x") is True


def test_needs_reinit_on_agnes_version_change(workdir_mgr: WorkdirManager):
    workdir_mgr.ensure_user_workdir("u@x")
    workdir_mgr._agnes_version = "0.56.0"
    assert workdir_mgr.needs_reinit("u@x") is True


def test_session_dir_creates_subtree(workdir_mgr: WorkdirManager):
    workdir_mgr.ensure_user_workdir("u@x")
    sdir = workdir_mgr.prepare_session_dir("u@x", "chat_abc")
    assert sdir.is_dir()
    assert sdir.name == "chat_abc"
    # sessions sit under <user>/sessions/<chat_id>/
    assert sdir.parent.name == "sessions"


def test_regular_session_includes_claude_local_md(workdir_mgr: WorkdirManager):
    """Regression: a regular per-user session must symlink the analyst's
    personal CLAUDE.local.md by default (include_personal_override defaults
    to True). Co-sessions exclude it via prepare_ephemeral_session_dir."""
    workdir_mgr.ensure_user_workdir("u@x")
    ws = workdir_mgr.user_workspace("u@x")
    (ws / "CLAUDE.local.md").write_text("# personal override\n")

    sdir = workdir_mgr.prepare_session_dir("u@x", "chat_local")

    link = sdir / "CLAUDE.local.md"
    assert link.exists(), "regular session must include CLAUDE.local.md"
    assert link.is_symlink()
    assert link.resolve() == (ws / "CLAUDE.local.md").resolve()


def test_prepare_session_dir_materializes_profile(workdir_mgr: WorkdirManager):
    """An authoring profile overrides the session CLAUDE.md with its persona
    and injects a read-only knowledge skill — WITHOUT mutating the shared
    workspace (.claude is copied, not symlinked-through)."""
    from app.chat.profiles import get_profile

    workdir_mgr.ensure_user_workdir("admin@x")
    sdir = workdir_mgr.prepare_session_dir("admin@x", "chat_prof", profile=get_profile("data-package-builder"))

    claude_md = (sdir / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Data Package Builder" in claude_md  # profile persona, not "default"
    assert not (sdir / "CLAUDE.md").is_symlink()
    skill = sdir / ".claude" / "skills" / "agnes-data-package" / "SKILL.md"
    assert skill.exists()
    assert "data_packages" in skill.read_text(encoding="utf-8")
    # the shared workspace must NOT have gained the profile skill
    ws = workdir_mgr.user_workspace("admin@x")
    assert not (ws / ".claude" / "skills" / "agnes-data-package").exists()


def test_prepare_session_dir_without_profile_symlinks_claude_md(workdir_mgr: WorkdirManager):
    """Regression: with no profile the session still symlinks the workspace
    CLAUDE.md (unchanged behaviour)."""
    workdir_mgr.ensure_user_workdir("u@x")
    sdir = workdir_mgr.prepare_session_dir("u@x", "chat_noprof")
    assert (sdir / "CLAUDE.md").is_symlink()


def test_prepare_session_dir_replaces_dangling_symlinks(workdir_mgr: WorkdirManager):
    """Regression: re-preparing a session dir that holds a DANGLING symlink
    (e.g. created under a relative DATA_DIR, whose target resolves against the
    link's own directory) must replace it, not die with FileExistsError —
    ``exists()`` follows the link and reports False, so the recreate fired on
    a path that was already occupied. Hit live on the post-restart resume
    path (`_resume_from_row` → prepare_session_dir)."""
    workdir_mgr.ensure_user_workdir("u@x")
    sdir = workdir_mgr.prepare_session_dir("u@x", "chat_dangle")
    link = sdir / "CLAUDE.md"
    assert link.is_symlink()
    link.unlink()
    link.symlink_to("data/nonexistent/CLAUDE.md")  # dangling
    assert link.is_symlink() and not link.exists()
    sdir2 = workdir_mgr.prepare_session_dir("u@x", "chat_dangle")  # must not raise
    assert sdir2 == sdir
    assert (sdir / "CLAUDE.md").exists()  # re-pointed at the real workspace file


def test_purge_user_removes_root(workdir_mgr: WorkdirManager):
    workdir_mgr.ensure_user_workdir("u@x")
    n = workdir_mgr.purge_user("u@x")
    assert n >= 2
    assert not workdir_mgr.user_workspace("u@x").exists()
    assert workdir_mgr._repo.get_workdir("u@x") is None


# ---------------------------------------------------------------------------
# render_workspace_prompt hook — sandbox CLAUDE.md == server-rendered analyst
# prompt (admin Workspace Prompt / default), matching a laptop `agnes init`.
# ---------------------------------------------------------------------------


def _mgr_with_render(tmp_path: Path, render) -> WorkdirManager:
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "CLAUDE.md").write_text("default")
    (bundled / ".claude").mkdir()
    (bundled / ".claude" / "settings.json").write_text("{}")
    return WorkdirManager(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        server_url="https://agnes.example",
        agnes_version="0.55.0",
        get_marketplace_sha=lambda: "mkt-sha-1",
        get_template_status=lambda: None,
        render_workspace_prompt=render,
    )


def test_run_init_overwrites_claude_md_with_rendered_prompt(tmp_path: Path):
    seen: list[str] = []

    def render(email: str):
        seen.append(email)
        return f"# Rendered for {email}"

    ws = _mgr_with_render(tmp_path, render).ensure_user_workdir("u@x")
    assert (ws / "CLAUDE.md").read_text() == "# Rendered for u@x"
    assert seen == ["u@x"]  # called with the user's email for RBAC context


def test_run_init_keeps_static_claude_md_when_render_returns_none(tmp_path: Path):
    ws = _mgr_with_render(tmp_path, lambda email: None).ensure_user_workdir("u@x")
    assert (ws / "CLAUDE.md").read_text() == "default"


def test_run_init_keeps_static_claude_md_when_render_raises(tmp_path: Path):
    def boom(email: str):
        raise RuntimeError("render failed")

    # Must not propagate — best-effort, static CLAUDE.md stays.
    ws = _mgr_with_render(tmp_path, boom).ensure_user_workdir("u@x")
    assert (ws / "CLAUDE.md").read_text() == "default"


def test_run_init_git_template_keeps_repo_claude_md_not_rendered(tmp_path: Path):
    """Override mode: when an admin git initial-workspace template is active,
    the repo's CLAUDE.md is authoritative (verbatim) — the Workspace Prompt
    render must NOT overwrite it. Mirrors `agnes init`, which skips
    /api/welcome in override mode. (The two are mutually exclusive by design.)
    """
    import io
    import zipfile

    from src.initial_workspace import TemplateStatus

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("CLAUDE.md", "# FROM GIT REPO")
        zf.writestr(".claude/settings.json", "{}")
    zip_bytes = buf.getvalue()

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "CLAUDE.md").write_text("default")
    (bundled / ".claude").mkdir()
    (bundled / ".claude" / "settings.json").write_text("{}")

    mgr = WorkdirManager(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        server_url="https://agnes.example",
        agnes_version="0.59.0",
        get_marketplace_sha=lambda: "sha-1",
        get_template_status=lambda: TemplateStatus(
            configured=True,
            synced=True,
            template_source="git",
            template_sha="abc",
        ),
        fetch_template_zip=lambda: zip_bytes,
        render_workspace_prompt=lambda email: "RENDERED WORKSPACE PROMPT",  # must NOT win
    )
    ws = mgr.ensure_user_workdir("u@x")
    assert (ws / "CLAUDE.md").read_text() == "# FROM GIT REPO"


def test_regular_session_symlinks_scaffolds(workdir_mgr: WorkdirManager):
    """Wave 3C: the data-apps starter templates under <workspace>/scaffolds/
    must reach the session dir (and therefore the sandbox's /work), or the
    agnes-data-apps-extras skill's `cp -R scaffolds/...` first step fails
    (Devin review finding)."""
    workdir_mgr.ensure_user_workdir("u@x")
    ws = workdir_mgr.user_workspace("u@x")
    (ws / "scaffolds" / "nodejs-dashboard").mkdir(parents=True)
    (ws / "scaffolds" / "nodejs-dashboard" / "app.js").write_text("// starter\n")

    sdir = workdir_mgr.prepare_session_dir("u@x", "chat_scaffold")

    link = sdir / "scaffolds"
    assert link.exists(), "session dir must include scaffolds/"
    assert (link / "nodejs-dashboard" / "app.js").exists()


class TestTheFeaturePruneRunsOnEveryConvergence:
    """Devin Review on #1239, three threads with one root cause.

    `_prune_disabled_feature_skills` lived inside `run_init`, which
    `ensure_user_workdir` only reaches when the sentinel is missing or
    `needs_reinit` is true — and `needs_reinit` compares the marketplace SHA
    and the Agnes version, never a feature flag. So its own docstring's
    promise ("an operator who turns a feature off later must see the skill
    leave") held for a brand-new workspace and for nobody else. It was also
    skipped entirely in template-OVERRIDE mode, where an operator's repo can
    vendor the same bundled skill.
    """

    def test_the_prune_runs_on_the_already_current_path(self):
        import inspect

        from app.chat.workdir import WorkdirManager

        src = inspect.getsource(WorkdirManager.ensure_user_workdir)
        before_reinit = src[: src.index("self.run_init")]
        assert "_reconcile_feature_gated_skills(" in before_reinit, (
            "a workspace that needs no reinit never prunes, so flipping the flag off does nothing"
        )

    def test_the_prune_runs_after_run_init_too(self):
        import inspect

        from app.chat.workdir import WorkdirManager

        src = inspect.getsource(WorkdirManager.ensure_user_workdir)
        after_reinit = src[src.index("self.run_init") :]
        assert "_reconcile_feature_gated_skills(" in after_reinit

    def test_it_is_not_confined_to_the_default_branch_of_run_init(self):
        """OVERRIDE mode gets it too — being called from the caller, not from
        one arm of the branch."""
        import inspect

        from app.chat.workdir import WorkdirManager

        assert "_prune_disabled_feature_skills" not in inspect.getsource(WorkdirManager.run_init), (
            "pruning inside run_init reaches only the branch it sits in"
        )


class TestTurningTheFeatureBackOnRestoresTheSkill:
    """Devin Review on #1239: pruning alone was a one-way door.

    The template copy that would put the skill back only runs on a reinit,
    and a feature flag is not something `needs_reinit` compares — so a
    workspace that had already converged lost the skill permanently the
    moment an operator toggled the feature off and on again.
    """

    def _ws(self, tmp_path):
        ws = tmp_path / "workspace"
        (ws / ".claude" / "skills" / "agnes-data-apps-extras").mkdir(parents=True)
        (ws / ".claude" / "skills" / "agnes-data-apps-extras" / "SKILL.md").write_text("x", encoding="utf-8")
        return ws

    def test_the_skill_comes_back_when_the_feature_is_on(self, tmp_path, monkeypatch):
        from app.chat.workdir import _reconcile_feature_gated_skills

        ws = self._ws(tmp_path)
        template = tmp_path / "template"
        (template / ".claude" / "skills" / "agnes-data-apps-extras").mkdir(parents=True)
        (template / ".claude" / "skills" / "agnes-data-apps-extras" / "SKILL.md").write_text("y", encoding="utf-8")

        target = ws / ".claude" / "skills" / "agnes-data-apps-extras"

        monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "0")
        _reconcile_feature_gated_skills(ws, template)
        assert not target.exists(), "the prune half stopped working"

        monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "1")
        _reconcile_feature_gated_skills(ws, template)
        assert target.is_dir(), "turning the feature back on never restored the skill"
        assert (target / "SKILL.md").read_text(encoding="utf-8") == "y"

    def test_an_existing_skill_is_not_overwritten(self, tmp_path, monkeypatch):
        """Restore fills a gap; it must not clobber a workspace that has it."""
        from app.chat.workdir import _reconcile_feature_gated_skills

        ws = self._ws(tmp_path)
        template = tmp_path / "template"
        (template / ".claude" / "skills" / "agnes-data-apps-extras").mkdir(parents=True)
        (template / ".claude" / "skills" / "agnes-data-apps-extras" / "SKILL.md").write_text("y", encoding="utf-8")

        monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "1")
        _reconcile_feature_gated_skills(ws, template)
        assert (ws / ".claude" / "skills" / "agnes-data-apps-extras" / "SKILL.md").read_text(encoding="utf-8") == "x"


# ---------------------------------------------------------------------------
# Marketplace skill delivery (#1552) — the workspace half of
# `chat.bootstrap_marketplace`. The composer offers `/<skill-name>` for every
# marketplace skill in the caller's stack, and these directories are what makes
# that token resolve: the session dir symlinks `.claude` from the workspace, so
# a skill written here is a PROJECT skill in the sandbox.
# ---------------------------------------------------------------------------


def _marketplace_src(root: Path, name: str, *, body: str = "Body.", extra: dict | None = None) -> Path:
    """A marketplace-side skill directory, as resolve_user_marketplace exposes it."""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n\n{body}", encoding="utf-8")
    for rel, content in (extra or {}).items():
        target = d / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return d


def _mgr_with_marketplace(tmp_path: Path, resolver, *, bundled_skills: dict | None = None) -> WorkdirManager:
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    bundled = tmp_path / "bundled"
    (bundled / ".claude").mkdir(parents=True)
    (bundled / "CLAUDE.md").write_text("default")
    (bundled / ".claude" / "settings.json").write_text("{}")
    for name, body in (bundled_skills or {}).items():
        _marketplace_src(bundled / ".claude" / "skills", name, body=body)
    return WorkdirManager(
        data_dir=tmp_path / "data",
        repo=ChatRepository(conn),
        bundled_template_dir=bundled,
        server_url="https://agnes.example",
        agnes_version="0.55.0",
        get_marketplace_sha=lambda: "mkt-sha-1",
        get_template_status=lambda: None,
        list_marketplace_skills=resolver,
    )


def test_a_stack_skill_is_materialized_into_the_workspace(tmp_path: Path):
    """The bug: the slash menu offered `/keboola-cli` and the sandbox had never
    been given it, so the turn came back "Unknown command"."""
    src = _marketplace_src(tmp_path / "mkt", "keboola-cli", body="Use the Keboola CLI.")
    mgr = _mgr_with_marketplace(tmp_path, lambda _email: [("keboola-cli", src)])

    ws = mgr.ensure_user_workdir("u@x")

    landed = ws / ".claude" / "skills" / "keboola-cli" / "SKILL.md"
    assert landed.is_file(), "the skill the menu offers never reached the agent's project scope"
    assert "Use the Keboola CLI." in landed.read_text()


def test_supporting_files_travel_with_the_skill(tmp_path: Path):
    """A skill's own references/ are part of it — copying SKILL.md alone gives
    the agent a skill whose first Read fails."""
    src = _marketplace_src(tmp_path / "mkt", "s1", extra={"references/deep.md": "detail"})
    mgr = _mgr_with_marketplace(tmp_path, lambda _email: [("s1", src)])

    ws = mgr.ensure_user_workdir("u@x")

    assert (ws / ".claude" / "skills" / "s1" / "references" / "deep.md").read_text() == "detail"


def test_a_skill_that_left_the_stack_is_removed(tmp_path: Path):
    src = _marketplace_src(tmp_path / "mkt", "gone-later")
    desired: list = [("gone-later", src)]
    mgr = _mgr_with_marketplace(tmp_path, lambda _email: list(desired))

    ws = mgr.ensure_user_workdir("u@x")
    assert (ws / ".claude" / "skills" / "gone-later").is_dir()

    desired.clear()  # user unsubscribed
    mgr.ensure_user_workdir("u@x")

    assert not (ws / ".claude" / "skills" / "gone-later").exists()


def test_unsubscribing_restores_the_bundled_skill_it_shadowed(tmp_path: Path):
    """Marketplace wins the name clash while subscribed (merged_skills's rule),
    but the user must not LOSE the bundled skill by having tried the other one."""
    src = _marketplace_src(tmp_path / "mkt", "shared", body="from the marketplace")
    desired: list = [("shared", src)]
    mgr = _mgr_with_marketplace(tmp_path, lambda _email: list(desired), bundled_skills={"shared": "from the template"})

    ws = mgr.ensure_user_workdir("u@x")
    assert "from the marketplace" in (ws / ".claude" / "skills" / "shared" / "SKILL.md").read_text()

    desired.clear()
    mgr.ensure_user_workdir("u@x")

    assert "from the template" in (ws / ".claude" / "skills" / "shared" / "SKILL.md").read_text()


def test_the_reconcile_never_deletes_a_skill_it_did_not_write(tmp_path: Path):
    """Pruning is bounded by the manifest. A bundled skill (or anything else in
    .claude/skills) must survive a reconcile that wants nothing."""
    mgr = _mgr_with_marketplace(tmp_path, lambda _email: [], bundled_skills={"bundled-only": "keep me"})

    ws = mgr.ensure_user_workdir("u@x")
    hand_written = ws / ".claude" / "skills" / "hand-written"
    hand_written.mkdir(parents=True, exist_ok=True)
    (hand_written / "SKILL.md").write_text("mine", encoding="utf-8")

    mgr.ensure_user_workdir("u@x")

    assert "keep me" in (ws / ".claude" / "skills" / "bundled-only" / "SKILL.md").read_text()
    assert (hand_written / "SKILL.md").read_text() == "mine"


def test_an_unchanged_skill_is_not_rewritten(tmp_path: Path):
    """The workspace is uploaded to the sandbox on every spawn; rewriting an
    identical tree would churn mtimes (and the upload) for nothing."""
    src = _marketplace_src(tmp_path / "mkt", "stable")
    mgr = _mgr_with_marketplace(tmp_path, lambda _email: [("stable", src)])

    ws = mgr.ensure_user_workdir("u@x")
    landed = ws / ".claude" / "skills" / "stable" / "SKILL.md"
    first = landed.stat().st_mtime_ns

    mgr.ensure_user_workdir("u@x")

    assert landed.stat().st_mtime_ns == first


def test_an_updated_skill_is_refreshed(tmp_path: Path):
    """...but a real content change must land — copy-if-changed, not copy-once."""
    src = _marketplace_src(tmp_path / "mkt", "moving", body="v1")
    mgr = _mgr_with_marketplace(tmp_path, lambda _email: [("moving", src)])

    ws = mgr.ensure_user_workdir("u@x")
    _marketplace_src(tmp_path / "mkt", "moving", body="v2")

    mgr.ensure_user_workdir("u@x")

    assert "v2" in (ws / ".claude" / "skills" / "moving" / "SKILL.md").read_text()


def test_no_marketplace_wiring_leaves_the_workspace_alone(tmp_path: Path):
    """`list_marketplace_skills=None` is "this instance has no marketplace
    wiring", not "the user's stack is empty" — it must not prune."""
    mgr = _mgr_with_marketplace(tmp_path, None, bundled_skills={"b": "keep"})

    ws = mgr.ensure_user_workdir("u@x")

    assert "keep" in (ws / ".claude" / "skills" / "b" / "SKILL.md").read_text()
    assert not (ws / ".claude" / ".agnes-marketplace-skills.json").exists()


def test_a_failing_resolver_does_not_deny_the_session(tmp_path: Path):
    def _boom(_email):
        raise RuntimeError("marketplace resolver exploded")

    mgr = _mgr_with_marketplace(tmp_path, _boom)

    ws = mgr.ensure_user_workdir("u@x")  # must not raise

    assert (ws / "CLAUDE.md").exists()
