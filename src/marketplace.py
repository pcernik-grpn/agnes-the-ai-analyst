"""Nightly sync of marketplace git repos onto the data volume.

Each row in the `marketplace_registry` DuckDB table is cloned (first run)
or fast-forwarded (subsequent runs) into ${DATA_DIR}/marketplaces/<slug>/.
FastAPI reads the working copies via the filesystem — this module has no
HTTP surface.

Callable from:
  - the scheduler (in-process, daily 03:00 UTC) via sync_marketplaces()
  - the admin API (POST /api/marketplaces/{id}/sync) via sync_one()
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from app.utils import get_marketplace_cache_dir, get_marketplaces_dir

logger = logging.getLogger(__name__)

GIT_TIMEOUT_SEC = 300
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_lock = threading.Lock()

PLUGIN_MANIFEST_REL = Path(".claude-plugin") / "marketplace.json"

# A pinned `ref` is either a tag name or a full 40-char commit SHA. The tag
# charset is deliberately conservative (vs. full git-check-ref-format rules)
# so a malformed value can never be mistaken for a git CLI flag when passed
# as a positional arg to `git fetch`/`git clone --branch` — must start with
# an alnum (no leading `-`), no `..`, no trailing `.lock`/`.`.
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")

# One filesystem-safe path segment. Used with `fullmatch` (see
# `is_safe_plugin_name`) — no anchors here, so `$`'s trailing-newline
# tolerance can't creep in.
_SAFE_PLUGIN_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")


class MarketplaceNotFound(Exception):
    """Raised when a marketplace id is not present in the registry."""


class MarketplaceNotSyncable(Exception):
    """Raised when a sync targets a row that has no git remote to sync from.

    Built-in rows (``is_builtin=TRUE`` — ``agnes-builtin`` and the
    contributed marketplace) carry a ``builtin://`` sentinel URL: their
    content is baked from the wheel on boot or written locally, never
    cloned. ``sync_marketplaces()`` filters them out via
    ``list_non_builtin()``; this is the same refusal for the per-row path,
    which is reachable from the admin "Sync now" button and
    ``agnes admin marketplace sync <slug>``.

    Not a RuntimeError/ValueError subclass on purpose: those two are what
    ``sync_one()`` catches to stamp ``last_error`` on the registry row, and a
    refusal must leave no trace on a row it never touched.
    """

    def __init__(self, marketplace_id: str):
        self.marketplace_id = marketplace_id
        super().__init__(
            f"marketplace {marketplace_id!r} is built-in: its content ships with the "
            "instance (or is written locally) and has no git remote to sync from"
        )


def is_valid_slug(slug: str) -> bool:
    return bool(_SLUG_RE.match(slug or ""))


def is_safe_plugin_name(name: object) -> bool:
    """True iff ``name`` is EXACTLY one filesystem-safe path segment.

    A plugin ``name`` comes from a registered marketplace's
    ``.claude-plugin/marketplace.json`` — curator- and supply-chain-controlled,
    so adversarial. It is used verbatim as the ``plugins/<name>`` segment under
    ``${DATA_DIR}/marketplaces/<slug>/``, and that directory is walked and read
    wholesale into the served ZIP / git tree. A ``/`` or ``..`` escapes the
    marketplaces root and turns the PAT-gated marketplace endpoints into an
    arbitrary-file-read primitive.

    Deliberately does NOT strip. The serve-time callers
    (``app/api/marketplace.py``, ``src/marketplace_asset_mirror.py``) match the
    raw segment and then use that same raw value to build a path; stripping here
    would silently widen those checks. Callers whose value IS stripped downstream
    (``read_plugins`` → ``_refresh_plugin_cache``) strip before calling.

    ``fullmatch``, not ``match``: ``$`` also matches before a trailing newline,
    so ``match`` would accept ``"acme\\n"``.

    Security playbook §6 mandates BOTH layers — reject here at ingest so a bad
    row never reaches the DB, and contain at use in
    ``marketplace_filter._contained_plugin_dir``.
    """
    if not isinstance(name, str):
        return False
    if name in ("..", "."):
        return False
    return bool(_SAFE_PLUGIN_NAME_RE.fullmatch(name))


def is_safe_plugin_source(source: object) -> bool:
    """True iff a string ``source`` is a relative path that stays inside the
    marketplace clone.

    A plugin's ``source`` in ``.claude-plugin/marketplace.json`` names where
    the plugin lives relative to the marketplace repo root — ``"./"`` for a
    root-source plugin (the single-plugin-repo shape), ``"./plugins/<name>"``
    for the conventional layout, or any other in-repo subdirectory. Like the
    plugin ``name``, it is curator-controlled and becomes a filesystem path
    under ``${DATA_DIR}/marketplaces/<slug>/`` that is walked and served
    wholesale, so an absolute path or a ``..`` segment is an arbitrary-file-read
    primitive.

    Non-string sources (Claude Code's external object form) are not this
    function's concern, and neither are remote string sources — callers route
    both through ``is_external_plugin_source`` (valid at ingest, no local
    files to serve). What this function rejects is a string that *looks* like
    an in-repo path but escapes: absolute, ``..``, or backslash-bearing.

    Security playbook §6 mandates BOTH layers — reject here at ingest
    (``read_plugins``) so a bad row never reaches the DB, and contain at use in
    ``marketplace_filter._contained_plugin_dir``.
    """
    if not isinstance(source, str):
        return False
    if source.startswith("/") or "\\" in source or ":" in source:
        return False
    return all(seg != ".." for seg in source.split("/"))


def is_external_plugin_source(source: object) -> bool:
    """True when a string ``source`` names a location OUTSIDE the marketplace
    clone — a URL (``https://…``, ``git+ssh://…``) or an scp-style git address
    (``git@host:owner/repo``).

    A remote string source is a real shape, not a typo: Agnes's own
    ``marketplace_metadata_scaffold`` has always accepted one (it keeps the
    plugin and merely skips skill/agent enumeration, see
    ``tests/test_marketplace_metadata_scaffold.py::
    test_remote_source_skips_enumeration_but_keeps_plugin_fields``). Ingest
    therefore keeps such a row — dropping it would take the plugin off the
    Browse shelf and leave its ``resource_grants`` dangling — and treats it
    exactly like the dict form: a catalog entry Agnes serves no files for.

    Deliberately narrow. A backslash-bearing string is NOT external (it is a
    Windows-style path attempt and stays a rejection), and a bare
    ``owner/repo`` is indistinguishable from a relative directory, so it keeps
    being read as one.
    """
    if not isinstance(source, str):
        return False
    s = source.strip()
    if not s or "\\" in s:
        return False
    if "://" in s:
        return True
    # scp-style `git@host:owner/repo` — a colon before the first slash.
    return ":" in s.split("/", 1)[0]


def is_full_sha(ref: str) -> bool:
    """True when `ref` is a full 40-character (hex) commit SHA."""
    return bool(_SHA_RE.match(ref or ""))


def is_valid_ref(ref: str) -> bool:
    """True when `ref` is a syntactically valid tag name or commit SHA.

    Used both by the admin API (400 on a malformed pin at registration time)
    and defensively by ``_sync_spec`` (a row edited directly in the DB, or
    seeded by an older Agnes version, must not reach `git` with an
    attacker-controlled or flag-like value).
    """
    if not ref:
        return False
    if is_full_sha(ref):
        return True
    if ".." in ref or ref.endswith(".lock") or ref.endswith("."):
        return False
    return bool(_REF_RE.match(ref))


# Per-invocation git credential helper. `!<command>` runs the rest as a shell
# command; it reads the PAT from $AGNES_TOKEN — set in the subprocess env only,
# never on the command line — and emits the credential protocol's two key=value
# lines on stdout.
#
# Replaces the previous `https://x-access-token:<PAT>@host/...` URL (2026-08-05
# audit, F-2). That form put the token on argv, where any co-tenant process reads
# it out of /proc/<pid>/cmdline, AND persisted it in plaintext into
# ${DATA_DIR}/marketplaces/<slug>/.git/config — on BOTH the clone and the
# `remote set-url` update path — from where it survives into every backup and
# volume snapshot. Security playbook §7.
# The username is pinned to `x-access-token`, the value the URL-embedded form
# this replaced also sent. Most hosts ignore it for token auth, but GitHub App
# installation tokens are documented as requiring exactly this — and this copy
# talks to arbitrary curator-supplied upstreams, so a bare `x` would have been
# a silent upgrade regression for that credential class (Devin Review on #1180).
_CREDENTIAL_HELPER = '!f() { printf "username=x-access-token\\npassword=%s\\n" "$AGNES_TOKEN"; }; f'


def _git_env(token: Optional[str] = None) -> dict:
    """Environment for git subprocesses: never prompt, optional PAT."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if token:
        env["AGNES_TOKEN"] = token
    return env


def _credential_args(repo_url: str, token: Optional[str]) -> List[str]:
    """``-c`` flags wiring the credential helper for THIS repo's host only.

    Host-scoped rather than global: an unscoped ``credential.helper`` answers
    with the PAT for any host git ends up asking about — including a redirect
    target, since ``http.followRedirects`` is on by default. The URL-embedded
    form this replaces was at least pinned to its own host, so an unscoped port
    would have been a regression on that axis.

    The generic empty reset comes FIRST: git APPENDS helpers, so without it an
    inherited system/global helper (e.g. osxkeychain) answers before ours.
    Verified with ``git credential fill`` — an empty reset placed after the real
    helper wipes it and auth fails.

    Returns ``[]`` for non-HTTPS URLs (``file://``, ``ssh://``) and empty tokens,
    matching the old ``_authenticated_url`` pass-through.
    """
    if not token:
        return []
    parts = urlparse(repo_url)
    if parts.scheme != "https" or not parts.hostname:
        return []
    host = parts.hostname
    if parts.port:
        host = f"{host}:{parts.port}"
    return [
        "-c",
        "credential.helper=",
        "-c",
        f"credential.{parts.scheme}://{host}.helper={_CREDENTIAL_HELPER}",
    ]


def _strip_userinfo(url: str) -> str:
    """``url`` with any ``user:pass@`` removed, structurally.

    Not token-dependent, unlike ``_redact``: the value we most need to sanitise
    is a remote that still embeds a PAT from before this release, and the token
    we would redact against may be rotated or unset by then — leaving redaction
    a no-op and putting the credential somewhere durable. Callers persist error
    text into ``marketplace_registry.last_error``, which the admin UI renders
    (Devin Review on #1180).
    """
    try:
        parts = urlparse(url)
    except ValueError:
        return "<unparseable url>"
    if not parts.hostname:
        return url
    netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
    return f"{parts.scheme}://{netloc}{parts.path}" if parts.scheme else f"{netloc}{parts.path}"


def _redact(s: str, token: str) -> str:
    return s.replace(token, "***") if token and s else s


def _scrub_credentialed_remote(target: Path, url: str) -> None:
    """Reset ``origin`` to the credential-free ``url``.

    Upgrade path: instances that synced before F-2 was fixed already have the PAT
    sitting in ``.git/config``. Resetting the remote on every sync cleans those in
    place on the next scheduled run — no operator action, no migration script.

    Best-effort: a failure here must not fail the sync, which is why it swallows
    rather than propagates.
    """
    try:
        _run_git(["remote", "set-url", "origin", url], cwd=target)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        logger.warning("marketplace: could not reset origin url in %s: %s", target, e)


def _run_git(
    args: List[str],
    cwd: Optional[Path] = None,
    *,
    url: Optional[str] = None,
    token: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """Run git with the PAT supplied via the environment, never on argv."""
    env = _git_env(token)
    args = [*_credential_args(url or "", token), *args]
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SEC,
        check=True,
    )


def _checkout_pinned_sha(
    target: Path,
    sha: str,
    *,
    url: Optional[str] = None,
    token: Optional[str] = None,
) -> None:
    """Resolve a full-length commit-SHA pin into `target`'s working tree.

    Tries a direct shallow fetch of the SHA first (`git fetch --depth 1
    origin <sha>`) — works when the git server enables
    `uploadpack.allowReachableSHA1InWant` / `allowAnySHA1InWant` (GitHub,
    GitLab, and most modern hosts do). Falls back to a full (unshallow)
    fetch of the default branch history when the server rejects direct-SHA
    fetches, then checks the SHA out of that history directly.

    Raises `subprocess.CalledProcessError` (propagated to `_sync_spec`'s
    handler, which turns it into a token-redacted `RuntimeError`) if the SHA
    still isn't reachable after the fallback — a mismatched/nonexistent pin
    fails the sync loudly. Neither the initial `fetch` nor a failed
    `checkout` touch the working tree, so a previously-synced checkout is
    left exactly as it was when this raises.
    """
    try:
        _run_git(["fetch", "--depth", "1", "origin", sha], cwd=target, url=url, token=token)
        _run_git(["checkout", "--detach", "FETCH_HEAD"], cwd=target)
        return
    except subprocess.CalledProcessError:
        pass  # server doesn't support direct-SHA fetch; fall back below

    is_shallow = (target / ".git" / "shallow").exists()
    fetch_args = ["fetch", "origin"]
    if is_shallow:
        fetch_args.insert(1, "--unshallow")
    # Also a network call — a private marketplace 401s here without the token,
    # which is exactly the SHA-pinned path this fallback exists to serve.
    _run_git(fetch_args, cwd=target, url=url, token=token)
    _run_git(["checkout", "--detach", sha], cwd=target)


def _sync_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Perform the clone/update for a single marketplace spec.

    Raises RuntimeError on git failure (with token-redacted message).
    Raises ValueError on invalid slug/ref.

    `ref` (tag name or full 40-char commit SHA) pins the marketplace to a
    fixed point in history — nightly/manual syncs keep resolving that same
    ref even when upstream's default branch moves. Mutually exclusive with
    `branch` (enforced at registration by the admin API's 400; re-checked
    here as defense-in-depth against a row edited directly in the DB).
    A tag pin reuses the exact same `git fetch <ref>` + `reset --hard
    FETCH_HEAD` path as a branch pin — tags and branches are both valid
    refspecs there. A SHA pin needs the special handling in
    `_checkout_pinned_sha` because `git clone --branch` doesn't accept an
    arbitrary commit SHA.
    """
    slug = (spec.get("id") or "").strip()
    name = spec.get("name") or slug
    url = (spec.get("url") or "").strip()
    branch = (spec.get("branch") or "").strip() or None
    ref = (spec.get("ref") or "").strip() or None
    token_env = (spec.get("token_env") or "").strip()
    token = os.environ.get(token_env, "") if token_env else ""

    if not is_valid_slug(slug):
        raise ValueError(f"marketplace id {slug!r} invalid (must match [a-z0-9][a-z0-9_-]{{0,63}})")
    if not url:
        raise ValueError(f"marketplace {slug!r}: url is required")

    target = get_marketplaces_dir() / slug
    is_git = (target / ".git").is_dir()

    # F-2 upgrade path, deliberately ahead of the ref validation below: a spec
    # that fails that validation raises and never reaches the sync body, so a row
    # with a malformed ref would keep a pre-fix credentialed remote in
    # .git/config indefinitely — until an admin noticed and fixed the row.
    if is_git:
        _scrub_credentialed_remote(target, url)

    if branch and ref:
        raise ValueError(f"marketplace {slug!r}: branch and ref are mutually exclusive")
    if ref and not is_valid_ref(ref):
        raise ValueError(f"marketplace {slug!r}: ref {ref!r} is not a valid tag name or 40-character commit SHA")

    pinned_sha = ref if ref and is_full_sha(ref) else None
    pinned_tag = ref if ref and not pinned_sha else None
    # Tags and branches resolve identically via `git fetch origin <name>` +
    # `reset --hard FETCH_HEAD` (and via `clone --branch <name>` on first
    # clone) — this is the single "checkout target" for that shared path.
    checkout_ref = pinned_tag or branch

    action = "update" if is_git else "clone"

    try:
        if not is_git:
            if target.exists():
                shutil.rmtree(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            if pinned_sha:
                # A commit SHA isn't a valid `--branch` name for git clone —
                # shallow-clone the default branch first, then resolve the
                # pin below (shared with the update path).
                _run_git(["clone", "--depth", "1", url, str(target)], url=url, token=token)
                _checkout_pinned_sha(target, pinned_sha, url=url, token=token)
            else:
                clone_args = ["clone", "--depth", "1"]
                if checkout_ref:
                    clone_args += ["--branch", checkout_ref]
                clone_args += [url, str(target)]
                _run_git(clone_args, url=url, token=token)
        else:
            # `remote set-url` ran above — but best-effort, so it may have been
            # skipped by a stale .git/config.lock, a read-only config after a
            # volume restore, or a missing `origin`. Everything below fetches
            # from `origin`, so an unverified assumption here means the sync can
            # report success against the PREVIOUS repository — or against an
            # origin that still embeds a PAT, defeating the very scrub this
            # release advertises. Confirm it, and fail loudly if not
            # (Devin Review on #1180).
            # `config --get`, NOT `remote get-url`: the latter expands
            # `url.<base>.insteadOf` rules (documented behaviour), so on a host
            # with a corporate-mirror rewrite it returns the rewritten URL while
            # `set-url` stored the original — a guaranteed mismatch that would
            # abort every sync on those deployments, even though the fetch would
            # have gone to the right place. We wrote the raw value, so we compare
            # the raw value (Devin Review on #1180).
            current = _run_git(["config", "--get", "remote.origin.url"], cwd=target).stdout.strip()
            if current != url:
                raise RuntimeError(
                    f"git {action} refused: origin is {_strip_userinfo(current)!r}, not the configured "
                    f"{_strip_userinfo(url)!r} — could not re-point the checkout, so a fetch here would "
                    "silently pull from the wrong remote"
                )
            if pinned_sha:
                _checkout_pinned_sha(target, pinned_sha, url=url, token=token)
            else:
                fetch_ref = checkout_ref or "HEAD"
                _run_git(["fetch", "--depth", "1", "origin", fetch_ref], cwd=target, url=url, token=token)
                _run_git(["reset", "--hard", "FETCH_HEAD"], cwd=target)
        sha = _run_git(["rev-parse", "HEAD"], cwd=target).stdout.strip()
    except subprocess.CalledProcessError as e:
        stderr = _redact(e.stderr or "", token).strip()
        raise RuntimeError(f"git {action} failed: {stderr}") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git {action} timed out after {GIT_TIMEOUT_SEC}s") from None

    logger.info("marketplace %s %s -> %s", slug, action, sha)
    return {"id": slug, "name": name, "action": action, "commit": sha, "path": str(target)}


def read_plugins(slug: str) -> List[Dict[str, Any]]:
    """Read the plugin list from a cloned marketplace's manifest.

    Returns the `plugins` array from `.claude-plugin/marketplace.json` at
    the root of the working copy. Returns an empty list if the manifest
    is missing, unreadable, or has no plugins. Malformed JSON is logged
    and treated as empty — a broken manifest must not take the sync
    operation down.
    """
    if not is_valid_slug(slug):
        raise ValueError(f"invalid slug: {slug!r}")
    manifest = get_marketplaces_dir() / slug / PLUGIN_MANIFEST_REL
    if not manifest.is_file():
        return []
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("marketplace %s: unreadable manifest %s: %s", slug, manifest, e)
        return []
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, list):
        return []
    out: List[Dict[str, Any]] = []
    for p in plugins:
        if not isinstance(p, dict):
            continue
        name = p.get("name")
        if not name:
            continue
        # Validate the STRIPPED form: _refresh_plugin_cache strips before writing
        # the row, so that is the value that later becomes a path segment.
        if not is_safe_plugin_name(str(name).strip()):
            logger.warning(
                "marketplace %s: dropping plugin with unsafe name %r (not a single path segment)",
                slug,
                name,
            )
            continue
        source = p.get("source")
        if isinstance(source, str) and not is_safe_plugin_source(source):
            if is_external_plugin_source(source):
                # A remote source is a valid shape with nothing local to serve
                # — same treatment as the object form: keep the catalog entry.
                logger.info(
                    "marketplace %s: plugin %r has remote source %r — kept as a catalog entry, "
                    "no files served from the clone",
                    slug,
                    name,
                    source,
                )
            else:
                logger.warning(
                    "marketplace %s: dropping plugin %r — source %r is not a usable location. "
                    "A string source must be an in-repo relative path (e.g. './' or './plugins/%s'); "
                    "an absolute or '..' path escapes the marketplace clone and is rejected. "
                    "For a plugin hosted elsewhere use a URL or the object form "
                    '({"source": "github", "repo": "..."}), either of which is kept as a '
                    "catalog-only entry.",
                    slug,
                    name,
                    source,
                    name,
                )
                continue
        out.append(p)
    return out


def _refresh_plugin_cache(slug: str, commit_sha: str | None = None) -> int:
    """Reload plugins from disk into marketplace_plugins. Returns plugin count.

    Failures here are logged but never re-raised: the primary sync result
    (git commit) has already succeeded at this point and must still be
    reported.

    Two-channel read:

    * ``.claude-plugin/marketplace.json`` (the Claude Code spec) is the
      authoritative source for plugin existence, source spec, and the bare
      Claude Code-shaped metadata.
    * ``.claude-plugin/marketplace-metadata.json`` (Agnes-only) supplies cover
      photo, video URL, doc links, and category overrides per plugin. Missing
      file → no enrichment, plugins still cached at the bare shape.

    External URLs referenced from marketplace-metadata are fed through the asset
    mirror (`src.marketplace_asset_mirror.sync_assets`) before the DB write
    so the persisted ``cover_photo_url`` / ``doc_links`` already point at the
    final served URL. Mirror failures degrade gracefully — failed external
    URLs surface as plain external links in the served data, never as 404s.
    """
    from src.marketplace_asset_mirror import sync_assets
    from src.marketplace_metadata import (
        collect_all_external_urls,
        read_marketplace_metadata,
        resolve_plugin_metadata,
    )
    from src.marketplace_urls import (
        internal_asset_url,
        internal_doc_url,
        mirrored_url,
    )
    from src.repositories import marketplace_plugins_repo

    # Cache-busting fingerprint baked into every served cover-photo URL.
    # 8 hex chars from the cloned repo's git HEAD — same upstream state →
    # same version → browser keeps cached bytes; git fetch landing a new
    # commit → new version → browser refetches. See
    # ``src/marketplace_urls.py:_with_version`` for the URL shape.
    asset_version = (commit_sha or "")[:8] or None

    try:
        plugins = read_plugins(slug)
    except Exception as e:  # noqa: BLE001
        logger.warning("marketplace %s: plugin read failed: %s", slug, e)
        return 0

    repo_root = get_marketplaces_dir() / slug
    metadata = read_marketplace_metadata(repo_root)

    # Resolve per-plugin enrichment + collect every external URL the mirror
    # needs to fetch this round. Internal references skip the mirror.
    resolved_per_plugin: Dict[str, Dict[str, Any]] = {}
    fetch_requests: List[tuple] = []
    for p in plugins:
        name = (p.get("name") or "").strip()
        if not name:
            continue
        resolved = resolve_plugin_metadata(metadata, name)
        resolved_per_plugin[name] = resolved
        # collect_all_external_urls walks plugin + skills + agents so the
        # mirror caches every external URL, not just plugin-level. Inner-
        # level skill/agent detail enrichment then looks up entries in the
        # same manifest at request time.
        for kind, url in collect_all_external_urls(metadata, name):
            fetch_requests.append((name, kind, url))

    # Mirror external URLs (best-effort — see _refresh_asset_mirror docstring
    # for the failure-mode contract). Keyed by ``(plugin_name, url)`` so two
    # plugins referencing the same external URL each get their own served
    # path under their own plugin subdir — RBAC-safe (a user with grant on
    # plugin B never receives a URL pointing under plugin A's tree).
    served_url_for: Dict[Tuple[str, str], Optional[str]] = {}
    mirror_status: Dict[Tuple[str, str], str] = {}
    if fetch_requests:
        cache_dir = get_marketplace_cache_dir() / slug
        try:
            report = sync_assets(cache_dir=cache_dir, requests=fetch_requests)
            for (plugin_name, url), entry in report.entries.items():
                mirror_status[(plugin_name, url)] = entry.status
                if entry.status == "ok" and entry.local:
                    # /mirrored/{key} where key encodes plugin + kind + filename.
                    # The local relpath is already in the right shape.
                    served_url_for[(plugin_name, url)] = (
                        mirrored_url(
                            slug,
                            entry.plugin_name,
                            entry.local.split("/", 1)[1],
                            version=asset_version,
                        )
                        if "/" in entry.local
                        else mirrored_url(
                            slug,
                            entry.plugin_name,
                            entry.local,
                            version=asset_version,
                        )
                    )
                else:
                    # Failed / rejected → fall back to the original URL so the
                    # frontend can still link out (b1).
                    served_url_for[(plugin_name, url)] = url
            logger.info(
                "marketplace %s: mirror summary fetched=%d not_modified=%d failed=%d rejected=%d removed=%d",
                slug,
                report.fetched,
                report.not_modified,
                report.failed,
                report.rejected,
                report.removed,
            )
        except Exception as e:  # noqa: BLE001 — never abort the sync
            logger.warning("marketplace %s: asset mirror crashed: %s", slug, e)
            # On total mirror crash, every (plugin, url) pair falls back to
            # the original URL so the strict-drop logic downstream marks it
            # as un-served and removes it from the rendered metadata.
            for plugin_name, _, url in fetch_requests:
                served_url_for.setdefault((plugin_name, url), url)
                mirror_status.setdefault((plugin_name, url), "failed_recent")

    # Compose the enriched plugin dicts and write to DB.
    enriched: List[Dict[str, Any]] = []
    for p in plugins:
        name = (p.get("name") or "").strip()
        if not name:
            continue
        merged = dict(p)
        resolved = resolved_per_plugin.get(name) or {}

        # Direct serialization to avoid mutating the frozen DocLinkRef.
        # External docs that mirroring rejected (e.g. HTML page, oversized,
        # SSRF-blocked) or failed to fetch (404, timeout, never seen before)
        # are DROPPED from the served list entirely. Internal links whose
        # path doesn't exist on disk at sync time are dropped too. This
        # matches the operator contract: any doc_link Agnes can't deliver
        # as a real downloadable PDF / Markdown / plain text is treated as
        # if it weren't in marketplace-metadata.json at all.
        serialized_links: List[Dict[str, str]] = []
        for link in resolved.get("doc_links") or []:
            if not hasattr(link, "kind"):
                continue
            if link.kind == "internal":
                local_path = repo_root / link.path
                if not local_path.is_file():
                    logger.info(
                        "marketplace %s plugin=%s: dropping internal doc_link %r (file not found in working tree)",
                        slug,
                        name,
                        link.path,
                    )
                    continue
                serialized_links.append(
                    {
                        "name": link.name,
                        "url": internal_doc_url(slug, name, link.path),
                    }
                )
                continue
            # external — keep ONLY when the mirror succeeded for THIS plugin.
            status = mirror_status.get((name, link.url), "")
            served = served_url_for.get((name, link.url))
            if status != "ok" or not served or served == link.url:
                logger.info(
                    "marketplace %s plugin=%s: dropping external doc_link %r (mirror status=%s)",
                    slug,
                    name,
                    link.url,
                    status or "no_attempt",
                )
                continue
            serialized_links.append(
                {
                    "name": link.name,
                    "url": served,
                }
            )

        # Build the column-shape payload inline — strict-drop semantics
        # need access to mirror status + on-disk existence per reference,
        # which is decided here rather than in a generic translator.
        # Internal covers are dropped when the file doesn't exist on disk;
        # external covers are dropped when mirroring rejected/failed (no
        # successful mirror means the served URL is the original external
        # URL, which we don't trust to render — better to fall through to
        # the gradient placeholder).
        if isinstance(resolved.get("cover_photo_ref"), tuple):
            kind, target = resolved["cover_photo_ref"]
            if kind == "internal":
                local_path = repo_root / target
                if local_path.is_file():
                    merged["cover_photo_url"] = internal_asset_url(
                        slug,
                        name,
                        target,
                        version=asset_version,
                    )
                else:
                    logger.info(
                        "marketplace %s plugin=%s: dropping internal cover_photo %r (file not found in working tree)",
                        slug,
                        name,
                        target,
                    )
            elif kind == "external":
                status = mirror_status.get((name, target), "")
                served = served_url_for.get((name, target))
                if status == "ok" and served and served != target:
                    merged["cover_photo_url"] = served
                else:
                    logger.info(
                        "marketplace %s plugin=%s: dropping external cover_photo %r (mirror status=%s)",
                        slug,
                        name,
                        target,
                        status or "no_attempt",
                    )
        if "video_url" in resolved:
            merged["video_url"] = resolved["video_url"]
        if "category" in resolved:
            # Override marketplace.json category when marketplace-metadata supplies one.
            merged["category"] = resolved["category"]
        if serialized_links:
            merged["doc_links"] = serialized_links

        enriched.append(merged)

    # Backend-aware write — on a Postgres-backed instance the rows must land
    # in Postgres, where the marketplace_plugins_repo() readers (UI + RBAC
    # fanout) look. A raw DuckDB write here is invisible to them → empty
    # marketplace on PG.
    try:
        count = marketplace_plugins_repo().replace_for_marketplace(slug, enriched)
    except Exception as e:  # noqa: BLE001
        logger.warning("marketplace %s: plugin cache write failed: %s", slug, e)
        return 0

    # Curator-side lifecycle: a plugin whose marketplace-metadata.json section
    # carries the literal `"deprecated": true` is admin-disabled right after
    # the cache write, so one upstream commit retires it on every consuming
    # instance. One-way by design — removing the flag later never
    # auto-re-enables; the admin decides whether a retired plugin comes back
    # (and if they re-enable a still-flagged one, the next sync re-disables
    # it). Failures degrade like every other enrichment step: warn, don't
    # abort the sync — the next sync retries.
    try:
        plugins_repo = marketplace_plugins_repo()
        already_disabled = set(plugins_repo.list_admin_disabled(slug))
        for name, resolved in resolved_per_plugin.items():
            if resolved.get("deprecated") is not True or name in already_disabled:
                continue
            if plugins_repo.set_admin_disabled(slug, name, True):
                logger.info(
                    "marketplace %s: plugin %s auto-disabled (deprecated upstream)",
                    slug,
                    name,
                )
    except Exception as e:  # noqa: BLE001
        logger.warning("marketplace %s: deprecated-plugin auto-disable failed: %s", slug, e)

    # v46: attribution tables removed. `MarketplaceItemLookup` resolves
    # skill/agent/command identifiers at usage-event write time by
    # prefix-splitting on `:` and matching the prefix against this same
    # `marketplace_plugins` table — no separate mapping pass needed here.
    return count


def _invalidate_served_caches() -> None:
    """Drop the served-marketplace caches after a successful sync.

    Both sync paths (``sync_one`` and ``sync_marketplaces``) must call this
    on success — a sync that leaves these caches warm reports a fresh
    commit while ``/marketplace.zip`` and the cowork bundle keep serving
    pre-sync bytes until their TTL expires, which is the opposite of what
    an on-demand sync is for. Both caches are global (keyed on the
    resolved plugin set / prefixed name+version, not on marketplace id),
    so there is no narrower scope to invalidate.

    Best-effort — a late import failing during early startup must not fail
    a sync that already succeeded. Late import: keeps this module decoupled
    from the FastAPI app surface.
    """
    try:
        from app.marketplace_server import packager as _packager

        _packager.invalidate_etag_cache()
        from app.marketplace_server import cowork_packager as _cowork

        _cowork.invalidate_cache()
    except ImportError:
        pass


def sync_one(marketplace_id: str) -> Dict[str, Any]:
    """Sync a single marketplace by id. Updates registry row with result.

    Raises:
        MarketplaceNotFound: if the id isn't registered.
        MarketplaceNotSyncable: if the row is built-in (no git remote).
        RuntimeError: if the git operation failed (token-redacted).
    """
    from src.repositories import marketplace_registry_repo

    # Backend-aware: registry rows live in Postgres on a PG instance. A raw
    # DuckDB read here returns an empty registry → "not found" / silent no-sync.
    repo = marketplace_registry_repo()
    spec = repo.get(marketplace_id)
    if not spec:
        raise MarketplaceNotFound(marketplace_id)

    # Built-in rows have a `builtin://` sentinel URL, so `_sync_spec` would
    # hand it to git, which resolves the scheme to a `git-remote-builtin`
    # helper that does not exist. That failure alone would be cosmetic — but
    # the clone path rmtree's the target directory FIRST (the baked tree has
    # no `.git`, so `is_git` is False), which destroys the seeded content
    # before the doomed clone even runs. `agnes-builtin` survives to the next
    # boot re-seed; the contributed marketplace, whose whole contract is
    # durability across restarts and syncs, does not. Refuse before touching
    # the filesystem or the registry row.
    if spec.get("is_builtin"):
        raise MarketplaceNotSyncable(marketplace_id)

    with _lock:
        try:
            result = _sync_spec(spec)
            repo.update_sync_status(
                marketplace_id,
                commit_sha=result["commit"],
                synced_at=datetime.now(timezone.utc),
            )
            result["plugin_count"] = _refresh_plugin_cache(
                marketplace_id,
                commit_sha=result["commit"],
            )
            _invalidate_served_caches()
            return result
        except (RuntimeError, ValueError) as e:
            repo.update_sync_status(
                marketplace_id,
                synced_at=datetime.now(timezone.utc),
                error=str(e),
            )
            raise


def sync_marketplaces() -> Dict[str, Any]:
    """Sync every registered marketplace. Empty registry = no-op.

    Built-in rows (is_builtin=TRUE) are always skipped — they have no remote
    URL to clone; their content is bundled in the wheel and re-baked on boot
    by ``seed_builtin_marketplace()``.

    One failure does not abort the rest; errors are collected per entry.
    """
    from src.repositories import marketplace_registry_repo

    # Backend-aware: on a PG instance the registry lives in Postgres. Reading it
    # through a raw DuckDB conn returned an empty list → "nothing to sync" → the
    # nightly sync silently never ran on Postgres-backed instances.
    # list_non_builtin() already filters out is_builtin=TRUE rows.
    repo = marketplace_registry_repo()
    specs = repo.list_non_builtin()

    if not specs:
        logger.info("No marketplaces registered; nothing to sync.")
        return {"synced": [], "errors": []}

    synced: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    with _lock:
        for spec in specs:
            slug = spec.get("id", "")
            try:
                result = _sync_spec(spec)
                repo.update_sync_status(
                    slug,
                    commit_sha=result["commit"],
                    synced_at=datetime.now(timezone.utc),
                )
                result["plugin_count"] = _refresh_plugin_cache(
                    slug,
                    commit_sha=result["commit"],
                )
                synced.append(result)
            except (RuntimeError, ValueError) as e:
                err = {"id": slug, "error": str(e)}
                errors.append(err)
                logger.error("marketplace %s sync failed: %s", slug, e)
                repo.update_sync_status(
                    slug,
                    synced_at=datetime.now(timezone.utc),
                    error=str(e),
                )

    if synced:
        _invalidate_served_caches()

    return {"synced": synced, "errors": errors}


# ---------------------------------------------------------------------------
# Built-in marketplace seeding
# ---------------------------------------------------------------------------

#: Slug reserved for the built-in marketplace. Matches the registry row
#: seeded by seed_builtin_marketplace() and is therefore excluded from the
#: nightly git-sync path.
BUILTIN_MARKETPLACE_SLUG = "agnes-builtin"

#: Path to the bundled content tree inside the wheel.
_BUILTIN_CONTENT_DIR = Path(__file__).parent / "_builtin_marketplace"

#: Sentinel URL stored in the registry for the built-in row. Never used for
#: git operations; exists only to satisfy the NOT NULL constraint on `url`.
_BUILTIN_SENTINEL_URL = "builtin://agnes-builtin"

#: RBAC seed: (group_name, plugin_name) pairs that must always exist.
_BUILTIN_RBAC_SEEDS = [
    ("Everyone", "agnes-analyst"),
    ("Admin", "agnes-operator"),
]


def seed_builtin_marketplace() -> None:
    """Idempotently seed the built-in marketplace on boot or upgrade.

    1. Upserts a ``marketplace_registry`` row with ``is_builtin=TRUE``
       whose ``url`` is the sentinel (never git-cloned).
    2. Copies the bundled ``_builtin_marketplace/`` tree into the
       marketplaces data directory so the regular plugin-cache reader
       (``_refresh_plugin_cache`` / ``read_plugins``) can find it.
    3. Refreshes the ``marketplace_plugins`` cache from the bundled content.
    4. Seeds ``resource_grants``: Everyone → agnes-analyst, Admin → agnes-operator.

    Safe to call on every startup — all writes are idempotent.
    """
    from src.repositories import (
        marketplace_registry_repo,
        resource_grants_repo,
        user_groups_repo,
    )

    slug = BUILTIN_MARKETPLACE_SLUG
    reg_repo = marketplace_registry_repo()

    # 1. Upsert registry row. curator_name gives the marketplace a visible
    # owner in the admin/browse UI — "Agnes" attributes the built-in content to
    # the platform itself (vendor-neutral), distinct from admin-registered
    # marketplaces which carry their curator's name.
    reg_repo.register(
        id=slug,
        name="Agnes Built-in",
        url=_BUILTIN_SENTINEL_URL,
        description=(
            "First-party guidance that ships with every Agnes instance: how to "
            "use Agnes as an analyst and how to configure it as an operator. "
            "Maintained by Agnes, served to all users (RBAC-scoped per plugin)."
        ),
        registered_by="system:seed",
        curator_name="Agnes",
        is_builtin=True,
    )
    logger.info("built-in marketplace: registry row seeded (slug=%s)", slug)

    # 2. Copy bundled content to the data directory so read_plugins() finds it.
    if _BUILTIN_CONTENT_DIR.is_dir():
        target = get_marketplaces_dir() / slug
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(_BUILTIN_CONTENT_DIR, target)
        logger.info("built-in marketplace: content baked to %s", target)
    else:
        logger.warning(
            "built-in marketplace: content directory not found at %s; plugin cache will be empty",
            _BUILTIN_CONTENT_DIR,
        )

    # 3. Refresh plugin cache from the bundled copy (no git SHA available).
    count = _refresh_plugin_cache(slug)
    logger.info("built-in marketplace: %d plugin(s) cached", count)

    # 4. Seed RBAC grants.
    groups_repo = user_groups_repo()
    grants_repo = resource_grants_repo()
    for group_name, plugin_name in _BUILTIN_RBAC_SEEDS:
        group = groups_repo.get_by_name(group_name)
        if not group:
            logger.warning(
                "built-in marketplace: group %r not found; skipping grant for %r",
                group_name,
                plugin_name,
            )
            continue
        resource_id = f"{slug}/{plugin_name}"
        grants_repo.ensure_grant(
            group_id=group["id"],
            resource_type="marketplace_plugin",
            resource_id=resource_id,
        )
        logger.info(
            "built-in marketplace: RBAC grant seeded: %s -> %s",
            group_name,
            resource_id,
        )


def delete_marketplace_dir(slug: str) -> bool:
    """Remove on-disk working copy + asset-mirror cache for a marketplace.

    Two directories are scoped per marketplace slug:
    * ``${DATA_DIR}/marketplaces/<slug>/``       — git working copy
    * ``${DATA_DIR}/marketplace-cache/<slug>/``  — external-asset mirror

    Removed together so a re-registered slug starts from a clean cache.
    Returns True iff at least one of the directories existed and was removed.
    """
    if not is_valid_slug(slug):
        raise ValueError(f"invalid slug: {slug!r}")
    removed = False
    work_path = get_marketplaces_dir() / slug
    if work_path.exists():
        shutil.rmtree(work_path)
        removed = True
    cache_path = get_marketplace_cache_dir() / slug
    if cache_path.exists():
        shutil.rmtree(cache_path, ignore_errors=True)
        removed = True
    return removed
