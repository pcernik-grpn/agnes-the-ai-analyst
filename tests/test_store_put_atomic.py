"""PUT /api/store/entities/{id} atomicity (#2 from PR #233 review).

Pre-fix: the bake wrote into the live `${DATA_DIR}/store/<id>/plugin/`
path BEFORE running guardrail checks. A concurrent GET during the
window saw partial / unverified content, and a failed check left the
on-disk tree in a partially-overwritten state until the rollback
copytree finished.

Post-fix: bake into a sibling `plugin.staging-<rand>/` dir, run checks
there, then atomic rename onto the live path. Failed checks leave the
live tree byte-for-byte intact.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from src.db import close_system_db, get_system_db
from src.repositories.users import UserRepository


# Strong default description for the content guardrail.
_OK_DESC = "Use when validating PUT atomicity against the content guardrail tier"


@pytest.fixture
def web_client(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    (tmp_path / "state").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "extracts").mkdir()
    close_system_db()
    app = shared_app
    yield TestClient(app)
    close_system_db()


def _create_user(client, email, password="UserPass1!"):
    ph = PasswordHasher()
    conn = get_system_db()
    user_id = email.split("@")[0]
    UserRepository(conn).create(
        id=user_id, email=email, name=user_id, password_hash=ph.hash(password),
    )
    conn.close()
    r = client.post("/auth/token", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return user_id, {"access_token": r.json()["access_token"]}


def _make_skill_zip(skill_name: str, body: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            f"{skill_name}/SKILL.md",
            f"---\nname: {skill_name}\ndescription: Use when validating atomic PUT semantics on the store entities upload endpoint\n---\n\n"
            + body,
        )
    return buf.getvalue()


def _make_evil_zip(skill_name: str) -> bytes:
    """A skill containing a static-security violation (eval) — fails
    inline checks during PUT, so the live tree must NOT be touched."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            f"{skill_name}/SKILL.md",
            f"---\nname: {skill_name}\ndescription: Use when validating PUT writes new content body successfully end to end\n---\n\nBody. " * 30,
        )
        zf.writestr(f"{skill_name}/run.sh", "#!/bin/sh\neval $1\n")
    return buf.getvalue()


def _hash_tree(root: Path) -> str:
    """Stable digest of the on-disk plugin tree (path + content)."""
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix().encode()
        h.update(rel + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


def _plugin_dir_for(entity_id: str) -> Path:
    """Mirror app/api/store.py:_plugin_dir without importing private."""
    from app.utils import get_store_dir
    return Path(get_store_dir()) / entity_id / "plugin"


class TestPutAtomicity:
    def test_failed_inline_check_leaves_live_tree_intact(self, web_client):
        """The live `plugin/` tree must be byte-for-byte identical
        before and after a PUT whose bundle fails inline checks."""
        owner_id, owner_cookies = _create_user(web_client, "ownerA@x.com")
        clean_zip = _make_skill_zip("atomic-skill", "Clean body. " * 80)
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", clean_zip, "application/zip")},
            data={"type": "skill", "description": _OK_DESC}, cookies=owner_cookies,
        )
        assert c.status_code == 201, c.text
        eid = c.json()["id"]

        plugin_dir = _plugin_dir_for(eid)
        before_hash = _hash_tree(plugin_dir)
        assert before_hash, "expected non-empty plugin tree"

        # PUT with a bundle that will fail static_security (contains eval).
        evil_zip = _make_evil_zip("atomic-skill")
        u = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("evil.zip", evil_zip, "application/zip")},
            cookies=owner_cookies,
        )
        # Static-security failures hard-reject with the security_blocked
        # code — no submission row, no version dir, no DB writes.
        assert u.status_code == 422, u.text
        assert u.json()["detail"]["code"] == "security_blocked"

        after_hash = _hash_tree(plugin_dir)
        assert after_hash == before_hash, (
            "live plugin tree changed after a failed-check PUT — "
            "atomic-rename invariant broken"
        )

        # Sibling staging dirs must not be left behind.
        entity_root = plugin_dir.parent
        leftovers = [
            p for p in entity_root.iterdir()
            if p.name.startswith("plugin.staging-")
            or p.name.startswith("plugin.backup-")
        ]
        assert not leftovers, (
            f"staging/backup dirs leaked on disk: {leftovers}"
        )

    def test_successful_put_atomically_replaces_tree(self, web_client):
        """Successful PUT swaps the live tree to the new bundle without
        leaving a staging dir behind."""
        owner_id, owner_cookies = _create_user(web_client, "ownerB@x.com")
        v1 = _make_skill_zip("swap-skill", "First body. " * 80)
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("v1.zip", v1, "application/zip")},
            data={"type": "skill", "description": _OK_DESC}, cookies=owner_cookies,
        )
        assert c.status_code == 201, c.text
        eid = c.json()["id"]
        plugin_dir = _plugin_dir_for(eid)
        before_hash = _hash_tree(plugin_dir)

        v2 = _make_skill_zip("swap-skill", "Second different body. " * 80)
        u = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )
        assert u.status_code == 200, u.text

        after_hash = _hash_tree(plugin_dir)
        assert after_hash != before_hash, "PUT didn't change live tree"

        entity_root = plugin_dir.parent
        leftovers = [
            p for p in entity_root.iterdir()
            if p.name.startswith("plugin.staging-")
            or p.name.startswith("plugin.backup-")
        ]
        assert not leftovers, (
            f"staging/backup dirs leaked on disk after success: {leftovers}"
        )

    def test_inline_check_failure_during_put_does_not_pollute_tree(
        self, web_client, monkeypatch,
    ):
        """Force a check failure mid-bake by monkey-patching
        run_inline_checks. Live tree must still be intact."""
        from src.store_guardrails.runner import InlineResult

        owner_id, owner_cookies = _create_user(web_client, "ownerC@x.com")
        clean_zip = _make_skill_zip("monkey-skill", "Body. " * 80)
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("v1.zip", clean_zip, "application/zip")},
            data={"type": "skill", "description": _OK_DESC}, cookies=owner_cookies,
        )
        assert c.status_code == 201, c.text
        eid = c.json()["id"]
        plugin_dir = _plugin_dir_for(eid)
        before_hash = _hash_tree(plugin_dir)

        # Force the PUT path to see a failed inline result without
        # actually relying on a static_security regex match.
        def fake_inline(*args, **kwargs):
            return InlineResult(
                manifest={"status": "fail", "issues": ["forced"]},
                static_security={"status": "pass", "findings": []},
                quality={"status": "pass", "issues": [],
                         "template_placeholders": 0,
                         "template_recommendation": None},
            )
        monkeypatch.setattr(
            "app.api.store.run_inline_checks", fake_inline,
        )

        v2 = _make_skill_zip("monkey-skill", "Different. " * 80)
        u = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )
        assert u.status_code == 422, u.text

        assert _hash_tree(plugin_dir) == before_hash, (
            "monkey-patched check failure polluted the live tree"
        )
