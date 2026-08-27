"""
Knowledge collector for Corporate Memory.

Uses a "full refresh" approach with hash-based change detection:
1. Track MD5 hashes of each user's CLAUDE.local.md in user_hashes.json
2. If no file changed since last run, skip entirely
3. When any file changed, collect ALL users' content + existing catalog
   and send to HAIKU in ONE call for a unified catalog refresh
4. HAIKU preserves existing item IDs (critical for vote stability),
   merges similar knowledge, and tracks source_users per item

TODO(scheduler-v2): In docker-compose.yml this service is a one-shot process
restarted by Docker (`restart: unless-stopped`), which is effectively a tight
boot loop (tracked in #221). The other half of this TODO — wiring into
services/scheduler/__main__.py's JOBS list and exposing an admin endpoint —
has landed: see the "corporate-memory" job kind in the scheduler and
POST /api/admin/run-corporate-memory in app/api/admin.py.

Notifications (#1573): when a collection run leaves new items pending
review, ``_notify_admins_of_pending_items`` below pings every Admin-group
member's desktop notification channel, gated on
``corporate_memory.notify_on_new_items`` (default on).
"""

import hashlib
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.logging_config import setup_logging
from app.utils import local_md_filename, uploaded_local_md_dir
from connectors.llm.exceptions import LLMError

from .confidence import compute_confidence
from .governance import resolve_initial_status
from .prompts import (
    CATALOG_REFRESH_PROMPT,
    CATALOG_REFRESH_SYSTEM,
    SENSITIVITY_CHECK_PROMPT,
    neutralize_untrusted,
)
from .tagger import auto_tag_items

# Fields preserved across re-collections when item already exists
GOVERNANCE_FIELDS = (
    "status",
    "approved_by",
    "approved_at",
    "mandatory_reason",
    "audience",
    "review_by",
    "edited_by",
    "edited_at",
)

# Configuration
CORPORATE_MEMORY_DIR = Path(os.environ.get("CORPORATE_MEMORY_DIR", "/data/corporate-memory"))
KNOWLEDGE_FILE = CORPORATE_MEMORY_DIR / "knowledge.json"
COLLECTION_LOG = CORPORATE_MEMORY_DIR / "collection.log"
USER_HASHES_FILE = CORPORATE_MEMORY_DIR / "user_hashes.json"
# Root of the per-user home directories in the bare-VM layout, where analysts
# work on the server itself and their CLAUDE.local.md is simply on disk. Env
# override so a deployment that lays homes out elsewhere isn't forced to patch
# the module (the sibling constants above have always been overridable; this one
# was hardcoded, which is part of why the Docker layout had no working input
# path at all). The laptop layout is served by `uploaded_local_md_dir()` and
# needs no setting.
HOME_BASE = Path(os.environ.get("CORPORATE_MEMORY_HOME_BASE", "/home"))

# Configure logging
setup_logging(__name__)
logger = logging.getLogger(__name__)

# JSON Schema for catalog refresh structured output
CATALOG_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    # Plain union type without an "enum" key — safe under
                    # strict structured outputs. The documented rejection
                    # applies only to nodes that combine both "enum" AND
                    # "type": [..., "null"]; this field has no enum constraint
                    # so the simple union is accepted.
                    "existing_id": {"type": ["string", "null"]},
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "category": {
                        "type": "string",
                        "enum": [
                            "data_analysis",
                            "api_integration",
                            "debugging",
                            "performance",
                            "workflow",
                            "infrastructure",
                            "business_logic",
                        ],
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "source_users": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": [
                    "existing_id",
                    "title",
                    "content",
                    "category",
                    "tags",
                    "source_users",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["items"],
    "additionalProperties": False,
}

# JSON Schema for sensitivity check structured output
SENSITIVITY_SCHEMA = {
    "type": "object",
    "properties": {
        "safe": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["safe"],
    "additionalProperties": False,
}


def _read_json(path: Path) -> dict:
    """Read a JSON file, return empty structure if not found or invalid."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning(f"Could not read {path}: {e}")
        return {}


def _write_json(path: Path, data: dict) -> None:
    """Write JSON data to file atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp_path, 0o660)  # group-readable for data-ops
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _generate_id(content: str) -> str:
    """Generate a stable ID from content hash."""
    h = hashlib.sha256(content.encode()).hexdigest()[:12]
    return f"km_{h}"


def _known_user_emails() -> list[str]:
    """Emails of all known users, or ``[]`` when the user store is unreachable.

    Needed because the uploaded layout stores files under a HASH of the email
    (see ``app.utils.local_md_filename``), which cannot be reversed — so the
    only way to enumerate uploaded files is to hash forward from every known
    user. Best-effort by design: the collector must degrade to the on-disk
    layout rather than crash when it runs without a reachable app DB.
    """
    try:
        from src.repositories import users_repo

        return [u["email"] for u in users_repo().list_all() if u.get("email")]
    except Exception as e:  # noqa: BLE001 — best-effort, mirrors the rest of this module
        logger.warning(f"Cannot enumerate users for uploaded CLAUDE.local.md scan: {e}")
        return []


def _home_dir_owner_email(dir_name: str) -> str | None:
    """Resolve a ``<HOME_BASE>/<dir_name>`` owner to their email, if known.

    Same convention as ``services/session_pipeline/runner.resolve_user_identity``:
    the home-directory name is the email local-part in the deployments that use
    this layout. Used ONLY to suppress a duplicate uploaded file for a user
    whose home directory was already picked up — never to rename the emitted
    username, so hash keys and ``source_user`` values on existing bare-VM
    instances stay exactly as they are.
    """
    try:
        from src.repositories import users_repo

        row = users_repo().get_by_email_prefix(dir_name)
        return row["email"] if row else None
    except Exception:  # noqa: BLE001 — best-effort; absence just means no dedup
        return None


def _find_claude_local_files() -> list[tuple[str, Path]]:
    """Find every analyst's CLAUDE.local.md across BOTH deployment layouts.

    Returns a list of ``(username, path)`` tuples, where ``username`` is the key
    for ``user_hashes.json`` and the ``source_user`` recorded on extracted items.

    Two layouts exist, and an instance can have either or both:

    * **bare VM** — analysts work on the server, so the file is simply at
      ``<HOME_BASE>/<name>/CLAUDE.local.md``. Emitted with ``username = <name>``,
      unchanged from before this function learned about the second layout.
    * **laptop + upload** — analysts work locally and ``agnes push`` uploads the
      file to ``POST /api/upload/local-md``, which stores it under
      ``${DATA_DIR}/user_local_md/`` (``app.utils.uploaded_local_md_dir``).
      Emitted with ``username = <email>``, the same canonical identity the
      session pipeline uses.

    Scanning only ``HOME_BASE`` meant that on any deployment which does not
    populate ``/home`` — Docker Compose, where analysts run Claude Code on their
    own laptops — the collector found zero files and returned ``skipped`` on
    every run indefinitely. The upload arrived and was never read, so corporate
    memory silently had no ``claude_local_md`` input at all.

    A user present in BOTH layouts is emitted once: the ``HOME_BASE`` copy wins
    (it is the layout where the analyst is actually working), mirroring the
    dual-layout precedence in ``app/api/admin_user_sessions._user_session_dirs``.
    """
    files: list[tuple[str, Path]] = []
    home_owner_emails: set[str] = set()

    # --- Layout 1: home directories on the server ---------------------------
    if HOME_BASE.exists():
        for user_dir in sorted(HOME_BASE.iterdir()):
            if not user_dir.is_dir():
                continue

            claude_local = user_dir / "CLAUDE.local.md"
            if claude_local.exists() and claude_local.is_file():
                username = user_dir.name
                files.append((username, claude_local))
                logger.info(f"Found CLAUDE.local.md for user: {username}")
    else:
        # Expected on Docker deployments; the uploaded layout below is the
        # input path there, so this is debug-level rather than a per-run
        # WARNING that operators learned to ignore.
        logger.debug(f"Home base directory {HOME_BASE} does not exist")

    # --- Layout 2: files uploaded by `agnes push` ---------------------------
    uploaded_dir = uploaded_local_md_dir()
    if uploaded_dir.is_dir():
        if files:
            # Only pay for the resolution when there is something to dedup
            # against.
            home_owner_emails = {email for email in (_home_dir_owner_email(name) for name, _ in files) if email}
        for email in sorted(_known_user_emails()):
            if email in home_owner_emails:
                logger.debug(f"Skipping uploaded CLAUDE.local.md for {email}; home-directory copy wins")
                continue
            path = uploaded_dir / local_md_filename(email)
            if path.is_file():
                files.append((email, path))
                logger.info(f"Found uploaded CLAUDE.local.md for user: {email}")
    else:
        logger.debug(f"Uploaded CLAUDE.local.md directory {uploaded_dir} does not exist")

    return files


def _check_for_changes() -> tuple[bool, dict[str, tuple[str, str]]]:
    """Check if any user's CLAUDE.local.md has changed since last run.

    Reads all CLAUDE.local.md files, computes MD5 hashes, and compares
    with stored hashes in user_hashes.json.

    Returns:
        (has_changes, user_files) where user_files maps
        username -> (content, md5_hash)
    """
    files = _find_claude_local_files()

    if not files:
        logger.info("No CLAUDE.local.md files found")
        return False, {}

    # Read all files and compute hashes
    user_files: dict[str, tuple[str, str]] = {}
    for username, filepath in files:
        try:
            content = filepath.read_text(encoding="utf-8")
            md5_hash = hashlib.md5(content.encode()).hexdigest()
            user_files[username] = (content, md5_hash)
        except Exception as e:
            logger.error(f"Failed to read {filepath}: {e}")
            continue

    if not user_files:
        return False, {}

    # Load stored hashes
    stored_hashes = _read_json(USER_HASHES_FILE)

    # Compare: check if any file changed, was added, or was removed
    current_hashes = {user: h for user, (_, h) in user_files.items()}
    stored = stored_hashes.get("hashes", {})

    if current_hashes == stored:
        logger.info("No changes detected in any CLAUDE.local.md files")
        return False, user_files

    # Log what changed
    for user, h in current_hashes.items():
        if user not in stored:
            logger.info(f"New user file detected: {user}")
        elif stored[user] != h:
            logger.info(f"Changed file detected: {user}")
    for user in stored:
        if user not in current_hashes:
            logger.info(f"Removed user file detected: {user}")

    return True, user_files


def _format_existing_catalog(existing: dict) -> str:
    """Format existing knowledge items for the HAIKU prompt.

    Returns a text block listing each existing item with its ID, title,
    content, category, tags, and source_users.
    """
    items = existing.get("items", {})
    if not items:
        return "(No existing items - this is a fresh catalog)"

    lines = []
    for item_id, item in items.items():
        tags_str = ", ".join(item.get("tags", []))
        users_str = ", ".join(item.get("source_users", []))
        lines.append(
            f"- ID: {item_id}\n"
            f"  Title: {item.get('title', 'Untitled')}\n"
            f"  Content: {item.get('content', '')}\n"
            f"  Category: {item.get('category', 'workflow')}\n"
            f"  Tags: {tags_str}\n"
            f"  Source users: {users_str}"
        )

    return "\n".join(lines)


def _format_user_files(user_files: dict[str, tuple[str, str]]) -> str:
    """Format all user CLAUDE.local.md contents for the HAIKU prompt.

    Returns a text block with each user's content clearly labeled.
    """
    sections = []
    for username, (content, _) in sorted(user_files.items()):
        # Defang the trust-boundary sentinels so a note body can't forge/close
        # the <untrusted_notes> wrapper and smuggle curator instructions.
        safe = neutralize_untrusted(content.strip())
        sections.append(f"### User: {username}\n```\n{safe}\n```")

    return "\n\n".join(sections)


def _process_catalog_response(
    response_items: list[dict],
    existing: dict,
    initial_status: str = "approved",
    confidence: float | None = None,
) -> dict[str, dict]:
    """Map HAIKU's response back to real IDs, preserving existing ones.

    For items with existing_id: keep that ID, update fields, preserve governance.
    For new items (existing_id is null): generate SHA256 ID from title+content.

    Args:
        response_items: Items returned by the LLM extractor.
        existing: Current knowledge.json data (with "items" dict).
        initial_status: Status to assign to new items ("approved" or "pending").
        confidence: Confidence score to stamp on new items (#1573) — the
            same score that decided ``initial_status`` when approval_mode is
            "threshold", persisted so the review queue can show *why* an
            item auto-published or was queued. Not applied to preserved
            existing items — their confidence (if any) is untouched.

    Returns dict of items keyed by ID.
    """
    existing_items = existing.get("items", {})
    existing_ids = set(existing_items.keys())
    now = datetime.now(timezone.utc).isoformat()

    result: dict[str, dict] = {}

    for item in response_items:
        existing_id = item.get("existing_id")

        if existing_id and existing_id in existing_ids:
            # Preserve existing item with updated fields
            old_item = existing_items[existing_id]
            result[existing_id] = {
                "id": existing_id,
                "title": item["title"],
                "content": item["content"],
                "category": item["category"],
                "tags": item["tags"],
                "source_users": item["source_users"],
                "extracted_at": old_item.get("extracted_at", now),
                "updated_at": now,
            }
            # Preserve governance fields from old item
            for field in GOVERNANCE_FIELDS:
                result[existing_id][field] = old_item.get(field)
        else:
            # New item - generate ID from title+content
            content_hash = item["title"] + item["content"]
            item_id = _generate_id(content_hash)

            # Handle collision with existing ID (unlikely but safe)
            if item_id in result:
                content_hash += now
                item_id = _generate_id(content_hash)

            result[item_id] = {
                "id": item_id,
                "title": item["title"],
                "content": item["content"],
                "category": item["category"],
                "tags": item["tags"],
                "source_users": item["source_users"],
                "extracted_at": now,
                "updated_at": now,
                "status": initial_status,
                "confidence": confidence,
                "approved_by": None,
                "approved_at": None,
                "mandatory_reason": None,
                "audience": "all",
                "review_by": None,
                "edited_by": None,
                "edited_at": None,
            }

    return result


def check_sensitivity(extractor, item: dict) -> bool:
    """Check if a knowledge item is safe to share.

    Returns True if safe, False if contains sensitive data.
    """
    prompt = SENSITIVITY_CHECK_PROMPT.format(
        title=item.get("title", ""),
        content=item.get("content", ""),
        tags=", ".join(item.get("tags", [])),
    )

    try:
        result = extractor.extract_json(
            prompt,
            max_tokens=256,
            json_schema=SENSITIVITY_SCHEMA,
            schema_name="sensitivity_check",
        )

        if not result.get("safe", False):
            reason = result.get("reason", "unknown")
            logger.info("Filtered sensitive item id=%s - %s", item.get("id", "unknown"), reason)
            return False

        return True

    except LLMError as e:
        logger.warning("Sensitivity check failed, assuming unsafe: %s", type(e).__name__)
        return False


def _notify_admins_of_pending_items(new_pending_count: int) -> None:
    """Best-effort desktop notification to every Admin-group member when a
    collection run leaves new items awaiting review (#1573 finding 2).

    ``new_pending_count`` is the number of ``pending`` rows *this run*
    inserted into ``knowledge_items`` — never the size of the whole queue,
    and never a count taken off the catalog file. Two reasons, in that
    order:

    * The catalog is rebuilt by full refresh on every run, so items an
      earlier run queued are carried over into ``final_items`` still marked
      ``pending``; counting those too would re-notify every admin about the
      entire backlog whenever any watched file changed, and call all of it
      "new" (which is exactly what the message below says).
    * The queue admins actually open is ``repo.list_items(statuses=
      ["pending"])`` on ``/admin/corporate-memory``, i.e. the DB. A count
      taken off ``final_items`` announces items whose insert failed —
      sending admins to a queue that does not contain them — and then goes
      silent on the retry run that finally lands the row, because by then
      the item is a *preserved* catalog entry rather than a new one.

    The backlog gauge lives in ``stats["items_pending"]`` and the catalog's
    own new-item count in ``stats["items_pending_new"]``; neither is what
    gets published here.

    Mirrors ``app.services.sync_notifier.notify_sync_completed``'s fan-out
    pattern: ``publish_notification`` only reaches a member with a live
    desktop WebSocket (``app/api/notifications_ws.py``), keyed on
    ``users.id`` — the same best-aligned-but-unverified channel key
    documented there. A dropped notification (no live socket, coordination
    backend down, no Admin group) is an acceptable degradation — the review
    queue is still there next time an admin opens
    ``/admin/corporate-memory`` — so this never raises into the caller.
    """
    if new_pending_count <= 0:
        return
    try:
        from app.notifications import publish_notification
        from src.db import SYSTEM_ADMIN_GROUP
        from src.repositories import user_group_members_repo, user_groups_repo

        admin_group = user_groups_repo().get_by_name(SYSTEM_ADMIN_GROUP)
        if not admin_group:
            return
        members = user_group_members_repo().list_members_for_group(admin_group["id"])
        message = (
            "1 new knowledge item awaiting review"
            if new_pending_count == 1
            else f"{new_pending_count} new knowledge items awaiting review"
        )
        for member in members:
            if not member.get("active", True):
                continue
            try:
                publish_notification(
                    member["id"],
                    {
                        "kind": "corporate_memory_pending",
                        "title": "Corporate Memory review queue",
                        "message": message,
                        "new_pending_count": new_pending_count,
                        "url": "/admin/corporate-memory",
                    },
                )
            except Exception:
                logger.warning("pending-items notification dropped for admin %s", member.get("id"))
    except Exception:
        logger.exception("pending-items notifier failed")


def collect_all(dry_run: bool = False) -> dict:
    """Main collection routine using full-refresh approach.

    1. Check if any CLAUDE.local.md file changed (MD5 hash comparison)
    2. If no changes, skip entirely
    3. If changes found, send ALL user files + existing catalog to HAIKU
    4. HAIKU produces updated catalog preserving existing IDs
    5. Run sensitivity check on NEW items only
    6. Save updated knowledge.json and user_hashes.json

    Args:
        dry_run: If True, don't write to knowledge.json, just return results.

    Returns:
        Statistics about the collection run.
    """
    stats: dict[str, Any] = {
        "users_scanned": 0,
        "files_found": 0,
        "items_extracted": 0,
        "items_filtered": 0,
        "items_preserved": 0,
        "items_new": 0,
        "items_pending": 0,
        "items_pending_new": 0,
        "items_pending_queued": 0,
        "skipped": False,
        "errors": [],
        "items_db_inserted": 0,
        "items_db_updated": 0,
        "items_db_errors": 0,
    }

    # Step 1: Check for changes
    has_changes, user_files = _check_for_changes()
    stats["files_found"] = len(user_files)
    stats["users_scanned"] = len(user_files)

    if not user_files:
        logger.info("No user files found, skipping collection")
        stats["skipped"] = True
        return stats

    if not has_changes:
        logger.info("No changes detected, skipping collection")
        stats["skipped"] = True
        return stats

    # Step 2: Initialize AI extractor.
    # Fail-fast (#176): no silent skip on missing ai: block. The factory
    # falls back to ANTHROPIC_API_KEY / LLM_API_KEY env vars and raises a
    # clear ValueError if neither config nor env is available — propagate
    # the ValueError so the scheduler / admin endpoint surface the
    # actionable misconfiguration message instead of swallowing it into
    # stats["errors"]. FileNotFoundError on the static config path is fine
    # to swallow because the factory's env fallback can still satisfy.
    #
    # Use the overlay-aware loader (#179 review fix) so an ai: block written
    # by /api/admin/configure to DATA_DIR/state/instance.yaml actually flows
    # through to the factory; config.loader.load_instance_config reads the
    # static config dir only and would silently miss the overlay.
    from app.instance_config import load_instance_config
    from connectors.llm import create_extractor_from_env_or_config

    try:
        instance_config = load_instance_config()
    except (ValueError, FileNotFoundError):
        instance_config = {}
    ai_config = instance_config.get("ai") if instance_config else None
    extractor = create_extractor_from_env_or_config(ai_config)

    # Determine initial status for new items based on approval mode (#1573:
    # shared with POST /api/memory via resolve_initial_status so the two
    # ingestion paths can't drift). Every item from this collector shares
    # one confidence score — the source-level base for "claude_local_md"
    # (configurable via corporate_memory.confidence.base, unrelated to the
    # per-item detection_type variance the user_verification path has) — so
    # it's computed once per run, not per item.
    governance_config = instance_config.get("corporate_memory", {})
    item_confidence = compute_confidence("claude_local_md") if governance_config else None
    initial_status = resolve_initial_status(governance_config, confidence=item_confidence)

    # Step 3: Load existing catalog
    existing = _read_json(KNOWLEDGE_FILE)
    if not existing:
        existing = {"items": {}, "metadata": {}}

    existing_ids = set(existing.get("items", {}).keys())

    # Step 4: Format prompt inputs
    catalog_text = _format_existing_catalog(existing)
    users_text = _format_user_files(user_files)

    # Step 5: Call HAIKU with full context
    prompt = CATALOG_REFRESH_PROMPT.format(
        existing_catalog=catalog_text,
        user_files=users_text,
    )

    logger.info(
        f"Sending catalog refresh to HAIKU with {len(user_files)} user files "
        f"and {len(existing.get('items', {}))} existing items"
    )

    try:
        response_data = extractor.extract_json(
            prompt,
            max_tokens=8192,
            json_schema=CATALOG_SCHEMA,
            schema_name="catalog_refresh",
            system=CATALOG_REFRESH_SYSTEM,
        )
        response_items = response_data.get("items", [])
        stats["items_extracted"] = len(response_items)
        logger.info(f"Extractor returned {len(response_items)} catalog items")

    except LLMError as e:
        logger.error("LLM extraction error: %s", type(e).__name__)
        stats["errors"].append(f"LLM error: {type(e).__name__}")
        return stats

    # Step 6: Process response - map to existing IDs
    processed_items = _process_catalog_response(
        response_items, existing, initial_status=initial_status, confidence=item_confidence
    )

    # Step 7: Run sensitivity check on NEW items only
    # Items with IDs that existed before already passed the check
    final_items: dict[str, dict] = {}

    for item_id, item in processed_items.items():
        if item_id in existing_ids:
            # Existing item - already passed sensitivity check before
            final_items[item_id] = item
            stats["items_preserved"] += 1
        else:
            # New item - run sensitivity check
            if check_sensitivity(extractor, item):
                final_items[item_id] = item
                stats["items_new"] += 1
                logger.info("Added new knowledge item: id=%s", item_id)
            else:
                stats["items_filtered"] += 1

    # Two different questions, two different numbers. ``items_pending`` is the
    # size of the whole review queue after this refresh (the gauge the CLI
    # prints and POST /api/admin/run-corporate-memory returns);
    # ``items_pending_new`` is what *this run* added to it — preserved items
    # keep their old status through GOVERNANCE_FIELDS, so the two only
    # coincide on a first run. Both describe the catalog file; what admins
    # actually review is the DB, so a third number, ``items_pending_queued``,
    # is counted during the sync below and is the one that gets notified.
    stats["items_pending"] = sum(1 for item in final_items.values() if item.get("status") == "pending")
    stats["items_pending_new"] = sum(
        1 for item_id, item in final_items.items() if item_id not in existing_ids and item.get("status") == "pending"
    )

    # Step 8: Auto-tag new items with topic vocabulary (best-effort)
    new_items = [item for item_id, item in final_items.items() if item_id not in existing_ids]
    if new_items:
        try:
            topic_assignments = auto_tag_items(new_items, extractor)
            for item_id, topics in topic_assignments.items():
                if item_id in final_items and topics:
                    existing_tags = final_items[item_id].get("tags") or []
                    # Prepend topics before free-form keywords (dedup, preserve order)
                    seen: set[str] = set()
                    merged: list[str] = []
                    for t in topics + existing_tags:
                        if t not in seen:
                            seen.add(t)
                            merged.append(t)
                    final_items[item_id]["tags"] = merged
            logger.info("Auto-tagged %d new item(s) with topics", len(topic_assignments))
        except Exception as e:
            logger.warning("auto_tag_items failed (non-fatal): %s", e)

    # Step 9: Build updated knowledge.json
    updated = {
        "items": final_items,
        "metadata": {
            "last_collection": datetime.now(timezone.utc).isoformat(),
            "total_users": stats["users_scanned"],
        },
    }

    # Step 10: Save knowledge.json unless dry run
    if not dry_run:
        _write_json(KNOWLEDGE_FILE, updated)
        logger.info(
            f"Knowledge base updated: {stats['items_preserved']} preserved, "
            f"{stats['items_new']} new, {stats['items_filtered']} filtered"
        )

        # Step 11: Sync final_items into the DB knowledge_items table.
        # Lazy import keeps this module importable without a DB connection
        # (unit tests mock the file I/O layer and never need a real DB).
        from src.repositories import knowledge_repo as _knowledge_repo

        repo = _knowledge_repo()
        inserted = updated_count = errors = 0
        # What the admin alert must count. ``/admin/corporate-memory`` renders
        # ``repo.list_items(statuses=["pending"])`` — the DB, never
        # knowledge.json — so an item only joins the review queue on the run
        # whose ``create()`` actually succeeds. Counting the catalog instead
        # got both ends of that wrong: a failed insert still alerted (queue
        # shows nothing), and the retry that finally landed the row was by
        # then a *preserved* catalog item, so it alerted nobody, ever.
        pending_queued = 0
        for item_id, item in final_items.items():
            try:
                existing = repo.get_by_id(item_id)
                if existing:
                    repo.update(
                        item_id,
                        title=item["title"],
                        content=item["content"],
                        category=item.get("category"),
                        tags=json.dumps(item.get("tags") or []),
                        source_user=(item.get("source_users") or [""])[0],
                        # status intentionally omitted — preserve any admin
                        # approval/rejection set through the UI or API
                    )
                    updated_count += 1
                else:
                    repo.create(
                        id=item_id,
                        title=item["title"],
                        content=item["content"],
                        category=item.get("category"),
                        source_user=(item.get("source_users") or [""])[0],
                        tags=item.get("tags") or [],
                        status=item.get("status", "pending"),
                        confidence=item.get("confidence"),
                        source_type="claude_local_md",
                        sensitivity=item.get("sensitivity", "internal"),
                        is_personal=item.get("is_personal", False),
                    )
                    inserted += 1
                    if item.get("status", "pending") == "pending":
                        pending_queued += 1
            except Exception as exc:
                logger.warning("DB sync error for item %s: %s", item_id, exc)
                errors += 1
        logger.info(
            "DB sync: %d inserted, %d updated, %d errors",
            inserted,
            updated_count,
            errors,
        )
        stats["items_db_inserted"] = inserted
        stats["items_db_updated"] = updated_count
        stats["items_db_errors"] = errors
        stats["items_pending_queued"] = pending_queued

        # #1573: notify admins that this run queued something new to triage,
        # unless the instance opted out. Defaults on to match the schema
        # default. Deliberately items_pending_queued, not items_pending — the
        # knob is named notify_on_new_items, and re-announcing the standing
        # backlog on every run is how a notification channel gets muted. An
        # item is announced exactly once, on the run that inserts its row:
        # every later run takes the ``update()`` branch and cannot re-count
        # it, so the muting loop cannot come back through this path either.
        if governance_config.get("notify_on_new_items", True):
            _notify_admins_of_pending_items(pending_queued)

        # Save user hashes only after DB sync — if every item failed to sync,
        # skip the hash write so the next scheduled run retries rather than
        # treating this run as up-to-date and skipping entirely.
        db_total_failure = len(final_items) > 0 and errors == len(final_items)
        if db_total_failure:
            logger.warning(
                "DB sync total failure (%d/%d errors) — skipping user hash update so the next run retries",
                errors,
                len(final_items),
            )
        else:
            current_hashes = {user: h for user, (_, h) in user_files.items()}
            _write_json(
                USER_HASHES_FILE,
                {
                    "hashes": current_hashes,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            logger.info("User hashes updated")
    else:
        logger.info(
            f"Dry run - would preserve {stats['items_preserved']}, "
            f"add {stats['items_new']}, filter {stats['items_filtered']}"
        )

    return stats


def reset_knowledge() -> None:
    """Clear all knowledge data for a fresh recalculation.

    Removes knowledge.json, user_hashes.json, and votes.json so the next
    collection run starts completely fresh. Does NOT remove collection.log.
    """
    for path in [KNOWLEDGE_FILE, USER_HASHES_FILE]:
        if path.exists():
            path.unlink()
            logger.info(f"Removed {path}")

    # Clear votes too - item IDs will change after reset
    votes_file = CORPORATE_MEMORY_DIR / "votes.json"
    if votes_file.exists():
        votes_file.unlink()
        logger.info(f"Removed {votes_file}")

    # Clean up all user .claude_rules directories (stale rules for old IDs)
    for user_dir in HOME_BASE.iterdir():
        if not user_dir.is_dir():
            continue
        rules_dir = user_dir / ".claude_rules"
        if rules_dir.exists():
            for rule_file in rules_dir.glob("km_*.md"):
                rule_file.unlink()
                logger.info(f"Removed stale rule: {rule_file}")


def main() -> int:
    """CLI entry point for the collector."""
    import argparse

    parser = argparse.ArgumentParser(description="Collect knowledge from CLAUDE.local.md files")
    parser.add_argument("--dry-run", action="store_true", help="Don't write changes")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear all data and recalculate from scratch (also clears votes)",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Configure file logging
    CORPORATE_MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(COLLECTION_LOG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logging.getLogger().addHandler(file_handler)

    if args.reset:
        print("Resetting Corporate Memory (clearing all data)...")
        reset_knowledge()
        print("Data cleared. Running fresh collection...\n")

    logger.info("Starting knowledge collection...")
    try:
        stats = collect_all(dry_run=args.dry_run)
    except ValueError as e:
        # collect_all() now fail-fasts on missing ai: config + env (#176).
        # Print the actionable message instead of a raw traceback.
        print(f"\nCorporate Memory cannot run: {e}", file=sys.stderr)
        return 1

    print("\nCollection complete:")
    if stats["skipped"]:
        print("  Status: SKIPPED (no changes detected)")
    print(f"  Users scanned: {stats['users_scanned']}")
    print(f"  Files found: {stats['files_found']}")
    print(f"  Items extracted: {stats['items_extracted']}")
    print(f"  Items preserved: {stats['items_preserved']}")
    print(f"  Items new: {stats['items_new']}")
    print(f"  Items filtered (sensitive): {stats['items_filtered']}")
    if stats.get("items_pending"):
        print(f"  Items pending review: {stats['items_pending']}")
        print(f"    ...of which new this run: {stats.get('items_pending_new', 0)}")

    if stats["errors"]:
        print(f"\nErrors ({len(stats['errors'])}):")
        for error in stats["errors"]:
            print(f"  - {error}")
        return 1

    return 0


if __name__ == "__main__":
    exit(main())
