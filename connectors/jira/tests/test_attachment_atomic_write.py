"""download_attachment's atomic publish — bounded temp name, atomic replace.

The temp-file publish (torn-read fix on the download endpoint) must not
change WHICH attachments can be stored: a name near the filesystem's
255-byte NAME_MAX saved fine with the old in-place write, so the temp name
is bounded rather than derived from the full name.

Both writers into the ``attachments/<ISSUE>/`` tree are covered: the
webhook path (``JiraService.download_attachment``) and the backfill
script's sibling downloader — the download endpoint serves whichever of
them wrote last, so neither may publish non-atomically.
"""

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from connectors.jira.service import Config, JiraService


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "JIRA_DATA_DIR", tmp_path)
    s = JiraService.__new__(JiraService)
    s.domain = "example.atlassian.net"
    s.email = "svc@example.com"
    s.api_token = "t"
    s.data_dir = tmp_path
    s.attachments_dir = tmp_path / "attachments"
    return s


def _fake_client(body: bytes):
    class _Client:
        def __init__(self, *a, **k): ...
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, auth=None):
            return SimpleNamespace(status_code=200, content=body)

    return _Client


def _attachment(filename: str) -> dict:
    return {
        "id": "77",
        "filename": filename,
        "size": 3,
        "content": "https://example.atlassian.net/rest/api/3/attachment/content/77",
    }


def test_long_filename_still_stores(svc):
    """~250-char basename: storable before the atomic publish, storable after."""
    long_name = "a" * 240 + ".bin"  # 244 chars + '77_' prefix = 247 < 255
    with patch("connectors.jira.service.httpx.Client", _fake_client(b"abc")):
        out = svc.download_attachment(_attachment(long_name), "PROJ-1")
    assert out is not None
    assert out.read_bytes() == b"abc"
    assert out.name == f"77_{long_name}"


def test_publish_is_atomic_and_leaves_no_temp(svc):
    with patch("connectors.jira.service.httpx.Client", _fake_client(b"xyz")):
        out = svc.download_attachment(_attachment("report.pdf"), "PROJ-1")
    assert out is not None and out.read_bytes() == b"xyz"
    leftovers = [p.name for p in out.parent.iterdir() if p.name.startswith(".tmp-")]
    assert leftovers == []


# ---------------------------------------------------------------------------
# Backfill script's sibling downloader — same tree, same contract
# ---------------------------------------------------------------------------


@pytest.fixture
def backfill(tmp_path):
    from connectors.jira.scripts.backfill import Config as BackfillConfig
    from connectors.jira.scripts.backfill import JiraBackfill

    return JiraBackfill(
        BackfillConfig(
            jira_domain="example.atlassian.net",
            jira_email="bf@example.com",
            jira_api_token="t",
            data_dir=tmp_path,
        )
    )


def test_backfill_long_filename_still_stores(backfill):
    """~250-char basename: storable before the atomic publish, storable after."""
    long_name = "b" * 240 + ".bin"  # 244 chars + '77_' prefix = 247 < 255
    with patch("connectors.jira.scripts.backfill.httpx.Client", _fake_client(b"abc")):
        out = backfill.download_attachment(_attachment(long_name), "PROJ-2")
    assert out is not None
    assert out.read_bytes() == b"abc"
    assert out.name == f"77_{long_name}"


def test_backfill_publishes_via_temp_then_replace(backfill, monkeypatch):
    """The final name only ever appears via os.replace of a fully written,
    bounded-name temp in the same directory — never a direct in-place write
    (a webhook-driven transform in another process can catalogue the path
    mid-backfill, and the download endpoint would fstat a partial size)."""
    observed = []
    real_replace = os.replace

    def recording_replace(src, dst):
        observed.append((Path(src).name, Path(dst).name, Path(src).read_bytes(), Path(dst).exists()))
        return real_replace(src, dst)

    monkeypatch.setattr("connectors.jira.scripts.backfill.os.replace", recording_replace)
    with patch("connectors.jira.scripts.backfill.httpx.Client", _fake_client(b"xyz")):
        out = backfill.download_attachment(_attachment("report.pdf"), "PROJ-2")
    assert out is not None and out.read_bytes() == b"xyz"
    # Full body already in the temp at replace time; final name untouched until then.
    assert len(observed) == 1
    tmp_name, final_name, tmp_bytes, final_existed = observed[0]
    import re as _re

    # Staging name: pid + random component + bounded basename prefix — the
    # random part keeps concurrent writers of the SAME attachment on their
    # own staging files (Devin on #1297).
    assert _re.fullmatch(rf"\.tmp-{os.getpid()}-[0-9a-f]{{8}}-77_report\.pdf", tmp_name)
    assert (final_name, tmp_bytes, final_existed) == ("77_report.pdf", b"xyz", False)
    leftovers = [p.name for p in out.parent.iterdir() if p.name.startswith(".tmp-")]
    assert leftovers == []


def test_backfill_crash_mid_write_leaves_no_partial_file(backfill):
    """A failure while writing the body must leave nothing under the final
    name (no torn file for the endpoint to serve) and clean up its temp."""

    class _ExplodingClient:
        def __init__(self, *a, **k): ...

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, auth=None):
            class _Resp:
                status_code = 200

                @property
                def content(self):
                    raise OSError("simulated failure mid-write")

            return _Resp()

    with patch("connectors.jira.scripts.backfill.httpx.Client", _ExplodingClient):
        with pytest.raises(OSError, match="mid-write"):
            backfill.download_attachment(_attachment("report.pdf"), "PROJ-3")
    issue_dir = backfill.attachments_dir / "PROJ-3"
    assert list(issue_dir.iterdir()) == []


def test_published_mode_is_pinned_regardless_of_umask(svc):
    """Devin on #1297 — os.replace publishes the TEMP file's mode (0666 &
    umask), not the previous inode's. Under a restrictive deploy-time umask
    the published attachment would be unreadable to the download endpoint's
    process; the publish pins 0o660 — group rw for the connector's ACL
    deployments, no world-read, matching the sibling issue-JSON writer.
    """
    old_umask = os.umask(0o077)
    try:
        with patch("connectors.jira.service.httpx.Client", _fake_client(b"abc")):
            out = svc.download_attachment(_attachment("modecheck.bin"), "PROJ-1")
    finally:
        os.umask(old_umask)
    assert out is not None
    assert (out.stat().st_mode & 0o777) == 0o660


def test_backfill_published_mode_is_pinned_regardless_of_umask(backfill):
    """Same mode pin for the backfill's sibling publisher."""
    old_umask = os.umask(0o077)
    try:
        with patch("connectors.jira.scripts.backfill.httpx.Client", _fake_client(b"abc")):
            out = backfill.download_attachment(_attachment("modecheck.bin"), "PROJ-2")
    finally:
        os.umask(old_umask)
    assert out is not None
    assert (out.stat().st_mode & 0o777) == 0o660


def test_staging_names_are_unique_per_download(backfill, monkeypatch):
    """Two downloads of the same attachment must never share a staging file:
    with a pid-only temp name, one os.replace() could publish the other's
    half-written bytes (Devin on #1297)."""
    names = []
    real_replace = os.replace

    def recording_replace(src, dst):
        names.append(Path(src).name)
        return real_replace(src, dst)

    monkeypatch.setattr("connectors.jira.scripts.backfill.os.replace", recording_replace)
    with patch("connectors.jira.scripts.backfill.httpx.Client", _fake_client(b"abc")):
        first = backfill.download_attachment(_attachment("dup.bin"), "PROJ-2")
        assert first is not None
        first.unlink()  # the backfill skips an already-present file
        assert backfill.download_attachment(_attachment("dup.bin"), "PROJ-2") is not None
    assert len(names) == 2 and names[0] != names[1]


def test_save_issue_retransforms_after_attachment_download(svc, monkeypatch):
    """Devin on #1297 — the incremental transform deliberately runs BEFORE
    the attachment download (worker-timeout rationale), so a freshly
    attached file is catalogued with local_path=NULL and the download
    endpoint 404s it until some later transform. After a download that
    actually landed files, save_issue must re-transform so the catalogue
    points at the bytes."""
    calls = []
    lock_depth = {"n": 0}

    from contextlib import contextmanager

    import connectors.jira.file_lock as file_lock_mod

    real_lock = file_lock_mod.issue_json_lock

    @contextmanager
    def counting_lock(*a, **kw):
        with real_lock(*a, **kw):
            lock_depth["n"] += 1
            try:
                yield
            finally:
                lock_depth["n"] -= 1

    monkeypatch.setattr(file_lock_mod, "issue_json_lock", counting_lock)
    monkeypatch.setattr(
        "connectors.jira.service.trigger_incremental_transform",
        lambda key, deleted=False, warn_unresolved=True: (
            calls.append((key, lock_depth["n"] > 0, warn_unresolved)) or True
        ),
    )
    monkeypatch.setattr(JiraService, "download_all_attachments", lambda self, data: [Path("stored.bin")])
    out = svc.save_issue({"key": "PROJ-9", "fields": {}})
    assert out is not None
    # Both transforms run, and BOTH under the per-issue lock — the second one
    # races poll_sla's locked read-modify-write otherwise (Devin on #1297).
    #
    # They differ in exactly one way: the second passes `warn_unresolved=False`.
    # It re-transforms a payload the first pass already reported on, so leaving
    # it True logged any missing-`jsdPublic` gap twice — doubling the count an
    # operator sizes the anomaly by, and only for attachment-bearing events,
    # which is worse than a uniform overcount.
    assert calls == [("PROJ-9", True, True), ("PROJ-9", True, False)]

    # And with nothing downloaded, no second transform — so nothing is silenced.
    calls.clear()
    monkeypatch.setattr(JiraService, "download_all_attachments", lambda self, data: [])
    out = svc.save_issue({"key": "PROJ-9", "fields": {}})
    assert out is not None
    assert calls == [("PROJ-9", True, True)]


def test_existing_attachment_is_not_refetched_and_not_reported_new(svc):
    """Devin on #1297 — the webhook downloader had no already-on-disk
    short-circuit, so every event re-fetched every attachment AND the
    save_issue re-transform gate fired on every event for any
    attachment-bearing issue. Jira attachment ids are immutable, so an
    existing <id>_<name> file is already the right bytes: the second call
    must not fetch and must not report a new publish."""
    with patch("connectors.jira.service.httpx.Client", _fake_client(b"abc")):
        first = svc.download_attachment(_attachment("keep.bin"), "PROJ-1")
    assert first is not None and first.read_bytes() == b"abc"

    class _MustNotFetch:
        def __init__(self, *a, **k):
            raise AssertionError("HTTP client constructed for an already-present attachment")

    with patch("connectors.jira.service.httpx.Client", _MustNotFetch):
        again = svc.download_attachment(_attachment("keep.bin"), "PROJ-1")
    assert again is None, "already-present must not count as a NEW publish"
    assert first.read_bytes() == b"abc", "existing bytes untouched"


def test_truncated_existing_attachment_is_refetched(svc):
    """Devin on #1297 — the exists-skip must not trust a short file: a worker
    SIGKILLed mid-write by the pre-atomic writer left truncated bytes under
    the final name, and an existence-only skip would serve them (with a
    self-consistent Content-Length) forever. Size mismatch → re-fetch."""
    with patch("connectors.jira.service.httpx.Client", _fake_client(b"abc")):
        out = svc.download_attachment(_attachment("heal.bin"), "PROJ-1")
    assert out is not None

    out.write_bytes(b"ab")  # simulate a pre-atomic truncated leftover
    with patch("connectors.jira.service.httpx.Client", _fake_client(b"abc")):
        healed = svc.download_attachment(_attachment("heal.bin"), "PROJ-1")
    assert healed is not None, "size mismatch must re-fetch"
    assert healed.read_bytes() == b"abc"


def test_backfill_truncated_existing_attachment_is_refetched(backfill):
    """Same completeness check for the backfill's exists-skip."""
    with patch("connectors.jira.scripts.backfill.httpx.Client", _fake_client(b"abc")):
        out = backfill.download_attachment(_attachment("heal.bin"), "PROJ-2")
    assert out is not None

    out.write_bytes(b"ab")
    with patch("connectors.jira.scripts.backfill.httpx.Client", _fake_client(b"abc")):
        healed = backfill.download_attachment(_attachment("heal.bin"), "PROJ-2")
    assert healed is not None
    assert healed.read_bytes() == b"abc"


def test_stale_staging_files_are_swept_fresh_ones_kept(svc):
    """Devin on #1297 — a SIGKILLed worker leaves its `.tmp-*` staging file
    behind, the retry mints a fresh random name, and nothing else touches
    hidden names: each interrupted download would leak up to
    MAX_ATTACHMENT_SIZE forever. The publisher sweeps stale staging files
    (age-gated so a concurrent writer's live staging file survives)."""
    import time as _time

    issue_dir = svc.attachments_dir / "PROJ-1"
    issue_dir.mkdir(parents=True, exist_ok=True)
    stale = issue_dir / ".tmp-99999-deadbeef-old.bin"
    stale.write_bytes(b"orphan")
    os.utime(stale, times=(_time.time() - 7200, _time.time() - 7200))
    fresh = issue_dir / ".tmp-88888-cafebabe-live.bin"
    fresh.write_bytes(b"in-flight")

    with patch("connectors.jira.service.httpx.Client", _fake_client(b"abc")):
        out = svc.download_attachment(_attachment("sweep.bin"), "PROJ-1")
    assert out is not None
    assert not stale.exists(), "hour-old staging orphan must be swept"
    assert fresh.exists(), "a concurrent writer's live staging file must survive"
