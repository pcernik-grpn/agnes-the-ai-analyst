"""CLI configuration — token storage, server URL, sync state."""

import json
import os
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator, Optional


# In-process override for `get_token()`. Used by `agnes init --token X` and
# `agnes auth import-token` to force a specific token for the duration of a
# scoped block, EVEN WHEN `~/.config/agnes/token.json` already holds a
# different (possibly stale) token. Without this override, `get_token()`
# reads the on-disk token first and the explicit `--token` argument is
# silently ignored — the bug Devin Review caught at cli/commands/init.py:99.
#
# A ContextVar is used (not a plain global) so concurrent callers — async
# tasks, threads — each see their own override, and a leaked override in
# one task can't corrupt another. `_token_override.set(...)` returns a
# token used to reset; the `_with_token_override` context manager scopes it.
_token_override: ContextVar[Optional[str]] = ContextVar(
    "agnes_cli_token_override",
    default=None,
)


@contextmanager
def _with_token_override(token: Optional[str]) -> Iterator[None]:
    """Set `_token_override` for the duration of the block.

    `get_token()` checks the override BEFORE reading `token.json`, so any
    in-block call returns the supplied token regardless of on-disk state.
    Restores the prior override (if any) on exit so nested overrides nest
    correctly.
    """
    if not token:
        yield
        return
    reset_token = _token_override.set(token)
    try:
        yield
    finally:
        _token_override.reset(reset_token)


def _config_dir() -> Path:
    d = Path(os.environ.get("AGNES_CONFIG_DIR", os.path.expanduser("~/.config/agnes")))
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_server_url() -> str:
    config = load_config()
    return os.environ.get("AGNES_SERVER", config.get("server", "http://localhost:8000"))


def resolve_server_url(explicit: Optional[str] = None) -> Optional[str]:
    """Explicit flag → ``AGNES_SERVER`` → saved config. ``None`` when unknown.

    The counterpart to `get_server_url()` for commands that must not INVENT a
    server. `get_server_url()` falls back to ``http://localhost:8000``, which
    is right for a local dev loop and wrong for anything that then acts on it:
    `agnes auth login` used to open a browser at that default, so a user who
    forgot ``--server`` saw only "localhost refused to connect" with nothing
    naming the real cause. Commands that need a REAL server resolve here and
    refuse on ``None``.

    Trailing slashes are stripped so a pasted ``https://host/`` and a typed
    ``https://host`` persist identically.
    """
    if explicit:
        return explicit.rstrip("/")
    env = os.environ.get("AGNES_SERVER")
    if env:
        return env.rstrip("/")
    saved = (load_config() or {}).get("server")
    if saved:
        return str(saved).rstrip("/")
    return None


def get_token() -> Optional[str]:
    # In-process override wins over BOTH the on-disk file and the env var.
    # Set by `_with_token_override(...)`; used by `agnes init --token X`
    # to force the explicit arg through the verify call even when a stale
    # `~/.config/agnes/token.json` exists.
    if (override := _token_override.get()) is not None:
        return override
    # Chat-sandbox context: AGNES_SESSION_ID is set only by the chat
    # runner's spawn env, alongside a FRESHLY-minted short-lived session
    # JWT in AGNES_TOKEN (ChatManager._spawn_runner re-mints on every
    # spawn/respawn). Any token.json found here — e.g. written by an
    # in-session `agnes init`, or replayed workspace state — is by
    # definition staler than the env credential and must not shadow it,
    # or a respawned runner 401s with the previous spawn's expired token.
    # Empty/unset env (e.g. the AGNES_SESSION_JWT_SEED fallback minted
    # "") still falls through to the file rather than returning a blank
    # credential. Analyst laptops (no AGNES_SESSION_ID) keep the
    # historical file-over-env order — token.json written by `agnes
    # init` stays canonical there.
    if os.environ.get("AGNES_SESSION_ID"):
        if env_token := os.environ.get("AGNES_TOKEN"):
            return env_token
    token_file = _config_dir() / "token.json"
    if token_file.exists():
        data = json.loads(token_file.read_text(encoding="utf-8"))
        return data.get("access_token")
    return os.environ.get("AGNES_TOKEN")


def save_token(token: str, email: str, role: Optional[str] = None):
    """Persist token + email to ~/.config/agnes/token.json.

    The ``role`` parameter is accepted for back-compat with older callers
    but is no longer written — authorization derives from group memberships
    server-side, not from a CLI-cached label. Old token.json files with a
    ``role`` field are still readable; the field is simply ignored.

    The file holds a plaintext bearer token, so it is written with mode
    ``0o600`` (owner read/write only) rather than left at the ambient umask
    (commonly ``0o644`` — world-readable). The chmod happens on a temp file
    in the same dir *before* the atomic rename, so a reader never observes a
    world-readable transient state. ``os.fchmod`` is best-effort and
    Windows-safe: on filesystems / platforms that don't honor it the token
    still lands and native ACLs apply (mirrors
    ``cli/lib/initial_workspace.py``).
    """
    body = json.dumps(
        {
            "access_token": token,
            "email": email,
        },
        indent=2,
    )
    config_dir = _config_dir()
    token_file = config_dir / "token.json"
    fd, tmp_str = tempfile.mkstemp(prefix=".token.", dir=str(config_dir))
    tmp_path = Path(tmp_str)
    try:
        try:
            os.write(fd, body.encode("utf-8"))
            try:
                os.fchmod(fd, 0o600)
            except (AttributeError, NotImplementedError, OSError):
                # Windows / filesystem that doesn't honor fchmod — native
                # ACLs apply, token contents still land.
                pass
        finally:
            os.close(fd)
        os.replace(tmp_path, token_file)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def clear_token():
    token_file = _config_dir() / "token.json"
    if token_file.exists():
        token_file.unlink()


def load_config() -> dict:
    config_file = _config_dir() / "config.yaml"
    if config_file.exists():
        import yaml

        loaded = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        # A non-mapping config.yaml (hand-edited to a scalar/list) must not
        # propagate a list/str to callers that do `config.get(...)`.
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _workspace_sync_state_file(workspace: Path) -> Path:
    """`<workspace>/.claude/agnes/sync_state.json` — the Agnes-owned,
    per-workspace scratch dir also used for the connector-params `.env`
    (`cli/lib/initial_workspace.py`) and the `agnes update` run log
    (`cli/commands/update.py`); workspace templates that ship a
    `.gitignore` already exclude `.claude/agnes/`.

    Deliberately NOT `<workspace>/.claude/sync_state.json` — that path is
    step 8's own state file (`cli/lib/pull_sync.py::_read_sync_state` /
    `_write_sync_state`, keyed by `direct_tables` / `data_packages` /
    `memory_domains`), a different schema owned by a different module.
    """
    return Path(workspace) / ".claude" / "agnes" / "sync_state.json"


def _get_workspace_sync_state(workspace: Path) -> dict:
    """Read-only: the workspace's own state if it exists, else a one-time
    migration seed from the legacy machine-global file (never persisted
    here — see `get_sync_state`'s docstring for why)."""
    state_file = _workspace_sync_state_file(workspace)
    if state_file.exists():
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    legacy_file = _config_dir() / "sync_state.json"
    if legacy_file.exists():
        try:
            legacy_state = json.loads(legacy_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            legacy_state = None
        if isinstance(legacy_state, dict):
            return legacy_state
    return {}


def get_sync_state(workspace: Optional[Path] = None) -> dict:
    """Read the sync-state blob `run_pull` uses for its download-gate hash
    comparison (and whatever else a caller stashes alongside ``tables``).

    ``workspace=None`` (the default) preserves the historical machine-global
    behavior — ``~/.config/agnes/sync_state.json`` (or ``$AGNES_CONFIG_DIR``)
    — unchanged, so every caller that predates workspace scoping (e.g.
    `agnes diagnose system --local`) keeps working exactly as before.

    Passing ``workspace`` reads the WORKSPACE-scoped file instead
    (`_workspace_sync_state_file`) — issue #1311: the machine-global file let
    a second workspace on the same machine inherit the first workspace's
    fresh download hashes and wrongly conclude its own stale parquet was
    already up to date (the existence guard in `cli/lib/pull.py`'s download
    gate catches an ABSENT file, not a stale one keyed by a hash that
    matches only because another workspace wrote it).

    One-time migration, and read-only: if the workspace has no scoped state
    yet but the legacy machine-global file does, this returns a seed built
    from it so the first pull after upgrading doesn't re-download every
    table — but does NOT write the seed to disk itself. That keeps a bare
    read (e.g. under `run_pull(dry_run=True)`, which reads sync state before
    its own dry-run short-circuit) side-effect-free; the seed becomes
    durable only once a caller round-trips it through `save_sync_state(...,
    workspace)`, exactly like `run_pull`'s normal read-then-save flow. The
    legacy file itself is never modified or deleted, so an older CLI
    version — or a second, not-yet-migrated workspace — can still read it.
    """
    if workspace is not None:
        return _get_workspace_sync_state(Path(workspace))
    state_file = _config_dir() / "sync_state.json"
    if state_file.exists():
        return json.loads(state_file.read_text(encoding="utf-8"))
    return {}


def save_sync_state(state: dict, workspace: Optional[Path] = None):
    """Persist the sync-state blob. ``workspace=None`` writes the legacy
    machine-global file (unchanged default, see `get_sync_state`); passing
    ``workspace`` writes `_workspace_sync_state_file` instead, creating
    `<workspace>/.claude/agnes/` on demand."""
    if workspace is not None:
        state_file = _workspace_sync_state_file(Path(workspace))
        state_file.parent.mkdir(parents=True, exist_ok=True)
    else:
        state_file = _config_dir() / "sync_state.json"
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")


def save_config(data: dict):
    """Persist server URL and other config to config.yaml.

    Merges into any existing mapping so a re-run never clobbers unrelated keys
    (e.g. ``workspace_root``). If the existing file is unparseable YAML or a
    non-mapping (corrupted, or hand-edited to a scalar/list) it cannot be
    merged — back it up to a ``.corrupt`` sibling and write a fresh mapping
    rather than crash. ``agnes config set-server`` is the install-time repair
    path, so it must recover a broken config, not raise on it.
    """
    import yaml

    config_file = _config_dir() / "config.yaml"
    existing: dict = {}
    if config_file.exists():
        raw = config_file.read_text(encoding="utf-8")
        try:
            loaded = yaml.safe_load(raw)
        except yaml.YAMLError:
            loaded = None
        if isinstance(loaded, dict):
            existing = loaded
        elif raw.strip():
            # Non-empty but unparseable / non-mapping — preserve it for
            # forensics before overwriting with a clean mapping.
            backup = config_file.with_name(f"config.yaml.corrupt.{os.getpid()}")
            try:
                backup.write_text(raw, encoding="utf-8")
            except OSError:
                pass
    existing.update(data)
    body = yaml.dump(existing, default_flow_style=False)
    config_dir = _config_dir()
    fd, tmp_str = tempfile.mkstemp(prefix=".config.", dir=str(config_dir))
    tmp_path = Path(tmp_str)
    try:
        try:
            os.write(fd, body.encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp_path, config_file)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def get_workspace_root() -> Optional[str]:
    """Absolute path of the analyst workspace anchored at `agnes init`.

    This is the single source of truth for where Claude Code session
    transcripts live: `agnes push` encodes it to the projects-dir folder
    name and scans that folder. Written by `agnes init` and back-filled by
    `agnes self-upgrade` for clients installed before the config key existed.
    Returns ``None`` when unset (push then uploads nothing).
    """
    config = load_config()
    val = config.get("workspace_root")
    return val or None


def set_workspace_root(path: str) -> None:
    """Persist the workspace root to config.yaml (merges, doesn't clobber)."""
    save_config({"workspace_root": str(path)})
