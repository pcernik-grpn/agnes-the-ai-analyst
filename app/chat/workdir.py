"""Per-user workspace and per-session working-directory lifecycle."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from app.chat.profiles import ChatProfile

from src.initial_workspace import (
    TemplateStatus,
    initialize_default_workspace,
    initialize_workspace_from_template,
    read_sentinel_server_url,
)

from app.chat.persistence import ChatRepository

logger = logging.getLogger(__name__)

#: Workspace entries ``prepare_session_dir`` symlinks into a session dir
#: (``CLAUDE.local.md`` only when ``include_personal_override``). "scaffolds"
#: carries the data-apps starter templates the agnes-data-apps-extras skill
#: copies from (wave 3C) — without it the sandbox's /work has no scaffolds/
#: and the skill's very first ``cp -R scaffolds/...`` step fails. Co-sessions
#: deliberately get no workspace symlinks (see prepare_ephemeral_session_dir),
#: so app scaffolding is a solo-session capability.
#:
#: The docker provider's profile-session mounts are derived from this SAME
#: list (``app/chat/docker_provider.py``) — the allowlist of what a profiled
#: sandbox may see must never be inferred from the agent-writable session dir.
WORKSPACE_LINK_ENTRIES = (".claude", "CLAUDE.md", "snapshots", "scripts", "scaffolds", "CLAUDE.local.md")


#: Bundled skills that only make sense when a feature is switched on, keyed by
#: the flag that governs them. The workspace tree ships every skill it has, and
#: nothing consults the instance's own configuration — so on an instance with
#: data apps OFF the agent still learns the data-app workflow and reaches for
#: it. Observed on a live instance: asked for a chart, it loaded
#: `agnes-data-apps-extras`, called `data_apps_list`, and got a 404
#: `data_apps_disabled` — a wasted round trip, and worse, the skill had already
#: aimed it at building a hosted dashboard for what was a one-off plot.
_FEATURE_GATED_SKILLS = {
    "agnes-data-apps-extras": ("data_apps", "enabled", "AGNES_DATA_APPS_ENABLED"),
}


def skill_disabled_on_this_instance(skill_name: str) -> bool:
    """Is this bundled skill gated off by an instance feature flag?

    The single reader of ``_FEATURE_GATED_SKILLS``, shared by the sandbox
    prune below and by ``app.chat.skills_catalog`` — the composer's slash menu
    lists the SHIPPED template, not the converged workspace, so without a
    common gate it went on advertising a skill whose files the prune had
    already removed.
    """
    from app.instance_config import feature_enabled

    gate = _FEATURE_GATED_SKILLS.get(skill_name)
    if gate is None:
        return False
    section, key, env_var = gate
    return not feature_enabled(section, key, env_var=env_var, default=False)


def _reconcile_feature_gated_skills(ws: Path, bundled_template_dir: Path, *, allow_restore: bool = True) -> None:
    """Make the workspace's gated skills agree with the instance's flags.

    Both directions, because pruning alone is a one-way door: a skill removed
    while its feature was off was never put back when the operator turned the
    feature on again, so the assistant lost it permanently on every workspace
    that had already converged (the template copy that would restore it only
    runs on a reinit, and a feature flag is not something ``needs_reinit``
    compares). Restoring is the same shape as the prune — copy the directory
    back from the bundled tree, which is the source this only ever subtracted
    from. (Devin Review on this PR.)
    """
    import shutil

    _prune_disabled_feature_skills(ws)

    # PRUNING applies everywhere — a skill for a feature this instance does not
    # have is useless whoever shipped it. RESTORING does not: in template-
    # OVERRIDE mode the operator's repo is authoritative for the workspace
    # tree, and copying a skill in from the SHIPPED default because a flag is
    # on would add something their template deliberately omits. Restore only
    # puts back what the default tree would have provided anyway.
    # (Devin Review on this PR.)
    if not allow_restore:
        return

    skills_root = ws / ".claude" / "skills"
    src_root = bundled_template_dir / ".claude" / "skills"
    if not src_root.is_dir():
        return
    for skill_name in _FEATURE_GATED_SKILLS:
        if skill_disabled_on_this_instance(skill_name):
            continue
        src = src_root / skill_name
        target = skills_root / skill_name
        if not src.is_dir() or target.exists():
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, target)
            logger.info("workdir: restored skill %s (its feature is on again)", skill_name)
        except OSError:
            logger.warning("workdir: could not restore skill %s", skill_name, exc_info=True)


def _prune_disabled_feature_skills(ws: Path) -> None:
    """Remove bundled skills whose feature is off on THIS instance.

    Runs after the template copy rather than filtering the copy itself: the
    workspace is converged repeatedly, and an operator who turns a feature off
    later must see the skill leave. Removal is by directory, so turning the
    flag back on restores it on the next convergence — the bundled tree is the
    source, this only subtracts.

    Called from ``ensure_user_workdir``, on BOTH paths — the one that reinits
    and the one that finds the workspace already current. Placing it inside
    ``run_init`` alone made the sentence above false for every existing
    workspace, because a feature flag is not one of the things ``needs_reinit``
    compares. It also applies in template-OVERRIDE mode: an operator's repo may
    vendor the bundled skill, and a skill for a feature this instance does not
    have is exactly as useless whoever shipped it.

    Best-effort by design. A skill that cannot be removed is a worse outcome
    than the one it causes (the agent wastes a call on a 404), so a failure
    here is logged and the workspace is still usable.
    """
    import shutil

    skills_root = ws / ".claude" / "skills"
    if not skills_root.is_dir():
        return
    for skill_name, (section, key, _env_var) in _FEATURE_GATED_SKILLS.items():
        if not skill_disabled_on_this_instance(skill_name):
            continue
        target = skills_root / skill_name
        if not target.is_dir():
            continue
        try:
            shutil.rmtree(target)
            logger.info("workdir: pruned skill %s (%s.%s is off)", skill_name, section, key)
        except OSError:
            logger.warning("workdir: could not prune skill %s", skill_name, exc_info=True)


def _safe_email_dir(email: str) -> str:
    """Email → directory-safe slug. Lowercase, replace non-[a-z0-9_-.@] with '_'."""
    return "".join(c if c.isalnum() or c in "._-@" else "_" for c in email.lower())


class WorkdirManager:
    def __init__(
        self,
        *,
        data_dir: Path,
        repo: ChatRepository,
        bundled_template_dir: Path,
        server_url: str,
        agnes_version: str,
        get_marketplace_sha: Callable[[], str],
        get_template_status: Callable[[], Optional[TemplateStatus]],
        fetch_template_zip: Optional[Callable[[], bytes]] = None,
        render_workspace_prompt: Optional[Callable[[str], Optional[str]]] = None,
        marketplace_sha_debounce_seconds: int = 0,
    ) -> None:
        self._data_dir = data_dir
        self._repo = repo
        self._bundled_template_dir = bundled_template_dir
        self._server_url = server_url
        self._agnes_version = agnes_version
        self._get_marketplace_sha = get_marketplace_sha
        self._get_template_status = get_template_status
        self._fetch_template_zip = fetch_template_zip
        # Optional ``user_email -> rendered CLAUDE.md`` hook. When set,
        # ``run_init`` overwrites the workspace CLAUDE.md with the
        # server-rendered analyst prompt (admin Workspace Prompt override or
        # the shipped default), RBAC-filtered for the user — the same content
        # ``agnes init`` writes on a laptop via ``GET /api/welcome``. Keeps
        # cloud chat consistent with a local install instead of diverging onto
        # the static bundled CLAUDE.md. Returns None → keep the static file.
        self._render_workspace_prompt = render_workspace_prompt
        # Debounce cache for the marketplace-SHA lookup. Operators set
        # ``marketplace_sha_debounce_seconds`` in instance.yaml to bound
        # how often the (potentially-slow) SHA source is consulted; this
        # caches the last value plus the monotonic timestamp it was read.
        self._sha_debounce_seconds = marketplace_sha_debounce_seconds
        self._cached_sha: Optional[str] = None
        self._cached_sha_at: float = 0.0

    def _user_root(self, user_email: str) -> Path:
        return self._data_dir / "users" / _safe_email_dir(user_email)

    def user_workspace(self, user_email: str) -> Path:
        return self._user_root(user_email) / "workspace"

    def user_sessions_root(self, user_email: str) -> Path:
        return self._user_root(user_email) / "sessions"

    def _current_marketplace_sha(self) -> str:
        """Read the marketplace SHA, honouring the debounce window.

        When ``marketplace_sha_debounce_seconds`` is positive, the cached
        SHA is returned for up to that many seconds; subsequent calls
        within the window re-use the cache without invoking the source
        callable. Setting the knob to ``0`` (default) disables caching.
        """
        if self._sha_debounce_seconds <= 0:
            return self._get_marketplace_sha()
        import time as _time

        now_mono = _time.monotonic()
        if self._cached_sha is not None and (now_mono - self._cached_sha_at) < self._sha_debounce_seconds:
            return self._cached_sha
        self._cached_sha = self._get_marketplace_sha()
        self._cached_sha_at = now_mono
        return self._cached_sha

    def _template_override_active(self) -> bool:
        """Is the admin's git template repo authoritative for the workspace?

        Mirrors the condition ``run_init`` branches on, so the two can never
        disagree about which tree owns the workspace.
        """
        status = self._get_template_status()
        return bool(status and status.configured and status.synced and self._fetch_template_zip is not None)

    def needs_reinit(self, user_email: str) -> bool:
        row = self._repo.get_workdir(user_email)
        if row is None:
            return True
        if row.marketplace_sha != self._current_marketplace_sha():
            return True
        if row.agnes_version_at_init != self._agnes_version:
            return True
        # The rendered CLAUDE.md names the server URL, so a workspace
        # initialized under one URL goes stale the moment the operator moves
        # the instance to another — the in-sandbox agent then reads the
        # mismatch between its rails and the host it's reached on as a
        # phishing indicator. The URL at init time lives in the
        # ``.claude/init-complete`` sentinel (written by both init modes);
        # ``None`` means a pre-server_url sentinel or a missing/unreadable
        # one, and a single self-healing reinit re-stamps it.
        # ``.strip()`` on our side too: the sentinel READER strips, so a
        # SERVER_URL carrying stray whitespace would compare unequal forever
        # and reinit the workspace on every single attach. Wasteful rather than
        # destructive (the init path only overwrites template-owned files), but
        # a malformed env var should not cost a full template copy per session.
        if read_sentinel_server_url(self.user_workspace(user_email)) != self._server_url.strip():
            return True
        return False

    def ensure_user_workdir(self, user_email: str) -> Path:
        ws = self.user_workspace(user_email)
        ws.mkdir(parents=True, exist_ok=True)
        sentinel = ws / ".claude" / "init-complete"
        if sentinel.exists() and not self.needs_reinit(user_email):
            # Still prune: a feature flag is not part of `needs_reinit`, which
            # compares only the marketplace SHA and the Agnes version. With the
            # prune living inside `run_init` its docstring's promise — "an
            # operator who turns a feature off later must see the skill leave"
            # — held for a fresh workspace and for nobody else: an operator
            # flipping `data_apps.enabled` off on a live instance changed
            # nothing until an unrelated upgrade happened to force a reinit.
            # Cheap enough to run on every convergence: one `is_dir()` per
            # gated skill on the common path. (Devin Review on this PR.)
            _reconcile_feature_gated_skills(
                ws, self._bundled_template_dir, allow_restore=not self._template_override_active()
            )
            return ws

        self.run_init(user_email, ws)
        _reconcile_feature_gated_skills(
            ws, self._bundled_template_dir, allow_restore=not self._template_override_active()
        )
        return ws

    def run_init(self, user_email: str, workspace: Optional[Path] = None) -> None:
        ws = workspace or self.user_workspace(user_email)
        status = self._get_template_status()
        template_sha = None
        if status and status.configured and status.synced and self._fetch_template_zip is not None:
            # OVERRIDE MODE: the admin's git template repo is authoritative for
            # CLAUDE.md (verbatim, no Jinja2, no RBAC filtering). Mirror the
            # laptop `agnes init`, which in override mode SKIPS the
            # /api/welcome write so the repo's CLAUDE.md wins. So we do NOT
            # overwrite with the Workspace Prompt here. (The git override and
            # /admin/workspace-prompt are mutually exclusive by design — see
            # docs/initial-workspace-override.md.)
            zip_bytes = self._fetch_template_zip()
            initialize_workspace_from_template(
                ws,
                zip_bytes,
                agnes_version=self._agnes_version,
                server_url=self._server_url,
                template_source=status.template_source,
                template_sha=status.template_sha,
            )
            template_sha = status.template_sha
        else:
            initialize_default_workspace(
                ws,
                agnes_version=self._agnes_version,
                server_url=self._server_url,
                bundled_template_dir=self._bundled_template_dir,
            )
            # DEFAULT MODE: overwrite the workspace CLAUDE.md with the
            # server-rendered analyst prompt (admin Workspace Prompt override
            # or shipped default), RBAC-filtered for this user — the same
            # content `agnes init` writes on a laptop in default mode.
            # Best-effort: any failure leaves the bundled static CLAUDE.md in
            # place, so the agent always has *some* rails.
            if self._render_workspace_prompt is not None:
                try:
                    rendered = self._render_workspace_prompt(user_email)
                    if rendered and rendered.strip():
                        (ws / "CLAUDE.md").write_text(rendered, encoding="utf-8")
                        logger.info("workdir CLAUDE.md rendered from workspace-prompt: user=%s", user_email)
                except Exception:
                    logger.exception(
                        "run_init: workspace-prompt render failed for %s; keeping static CLAUDE.md",
                        user_email,
                    )

        self._repo.upsert_workdir(
            user_email=user_email,
            marketplace_sha=self._current_marketplace_sha(),
            initial_workspace_sha=template_sha,
            agnes_version=self._agnes_version,
        )
        logger.info("workdir initialized: user=%s template_sha=%s", user_email, template_sha)

    def prepare_session_dir(
        self,
        user_email: str,
        chat_id: str,
        *,
        include_personal_override: bool = True,
        profile: "ChatProfile | None" = None,
    ) -> Path:
        """Prepare a regular per-user session directory.

        By default (``include_personal_override=True``) the user's personal
        ``CLAUDE.local.md`` is symlinked into the session dir alongside the
        shared workspace state, so regular per-user sessions carry the
        analyst's personal overrides. Co-drive sessions never call this
        method — they use :meth:`prepare_ephemeral_session_dir`, which
        deliberately excludes ``CLAUDE.local.md`` (SR-6 protection).

        When ``profile`` is set (authoring-agent sessions), the session is
        specialized: the workspace ``CLAUDE.md`` is replaced by the profile
        persona and a read-only knowledge skill is injected. To avoid writing
        through the ``.claude`` symlink into the *shared* workspace, ``.claude``
        is **copied** (not symlinked) for profiled sessions and ``CLAUDE.md`` is
        written as a real file. The profile is materialized into the workdir
        only — it is never persisted, so no schema migration is involved.
        """
        sessions_root = self.user_sessions_root(user_email)
        sessions_root.mkdir(parents=True, exist_ok=True)
        sdir = sessions_root / chat_id
        sdir.mkdir(parents=True, exist_ok=True)
        # Symlink shared workspace state into the session dir so
        # claude-agent-sdk resolves .claude/{skills,plugins,agents,commands,hooks}
        # against the per-user workspace.
        ws = self.user_workspace(user_email)
        entries = [e for e in WORKSPACE_LINK_ENTRIES if e != "CLAUDE.local.md"]
        if include_personal_override:
            entries.append("CLAUDE.local.md")
        # A profile owns .claude (copied, see below) and CLAUDE.md (persona) —
        # skip symlinking those two so we don't link-through to the workspace.
        profile_owned = {".claude", "CLAUDE.md"} if profile is not None else set()
        for entry in entries:
            if entry in profile_owned:
                continue
            link = sdir / entry
            target = ws / entry
            if not target.exists():
                continue
            if link.is_symlink() and not link.exists():
                # Dangling (e.g. created under a relative DATA_DIR, whose
                # target resolves against the link's own directory): replace
                # it, or the recreate below dies with FileExistsError —
                # exists() follows the link and reports False, so a re-run of
                # this method (post-restart resume) was not idempotent.
                link.unlink()
            if not link.exists():
                link.symlink_to(target)
        if profile is not None:
            self._materialize_profile(sdir, ws, profile)
        (sdir / "work").mkdir(exist_ok=True)
        return sdir

    @staticmethod
    def _materialize_profile(sdir: Path, ws: Path, profile: "ChatProfile") -> None:
        """Copy the workspace ``.claude`` into ``sdir`` and overlay the profile
        persona + knowledge skill, without mutating the shared workspace."""
        import shutil

        claude_dst = sdir / ".claude"
        if claude_dst.is_symlink():
            claude_dst.unlink()
        elif claude_dst.is_dir():
            shutil.rmtree(claude_dst)
        claude_src = ws / ".claude"
        if claude_src.exists():
            shutil.copytree(claude_src, claude_dst)
        else:
            claude_dst.mkdir(parents=True, exist_ok=True)
        (sdir / "CLAUDE.md").write_text(profile.claude_md, encoding="utf-8")
        skill_dir = claude_dst / "skills" / profile.skill_name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(profile.skill_body, encoding="utf-8")

    def prepare_ephemeral_session_dir(
        self,
        chat_id: str,
        participant_emails: list[str],
        intersection: "dict[str, frozenset[str]]",
    ) -> Path:
        """Fresh co-session workspace. NO symlinks to any personal workspace,
        NO CLAUDE.local.md in any form, fresh empty memory/, shared work/.
        Only intersection-filtered .claude/skills entries are copied in."""
        import shutil

        root = self._data_dir / "ephemeral_sessions" / chat_id
        if root.exists():
            shutil.rmtree(root)
        (root / ".claude" / "skills").mkdir(parents=True, exist_ok=True)
        (root / ".claude" / "agents").mkdir(parents=True, exist_ok=True)
        (root / "memory").mkdir(exist_ok=True)
        (root / "work").mkdir(exist_ok=True)
        # FIX 4 (H1): do NOT render the owner-scoped workspace prompt for the
        # ephemeral co-drive path. The render_workspace_prompt callable is
        # bound to a single user's identity (participant_emails[0] was the
        # owner), so calling it would leak owner-scoped catalog metadata
        # ({{tables}}, {{marketplaces}}) into the shared CLAUDE.md even when
        # those resources are not in the intersection. Analysts use
        # `agnes catalog` for discovery, which is intersection-gated. The
        # static "# Co-drive session" header is always safe.
        (root / "CLAUDE.md").write_text("# Co-drive session\n", encoding="utf-8")
        allowed = intersection.get("marketplace_plugin", frozenset())
        src_root = self._bundled_template_dir / ".claude" / "skills"
        if src_root.exists():
            for plug in allowed:
                src = src_root / plug
                if src.exists():
                    shutil.copytree(src, root / ".claude" / "skills" / plug, dirs_exist_ok=True)
        return root

    def purge_user(self, user_email: str) -> int:
        """GDPR hard-delete. Returns file count removed."""
        import shutil

        root = self._user_root(user_email)
        if not root.exists():
            return 0
        count = sum(1 for _ in root.rglob("*") if _.is_file())
        shutil.rmtree(root)
        self._repo.delete_workdir_row(user_email)
        return count
