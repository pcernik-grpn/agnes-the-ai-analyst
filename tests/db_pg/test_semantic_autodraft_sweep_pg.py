"""``POST /api/admin/semantic-auto-draft-sweep`` (semantic-phase5, wave 2) —
PG-backend behavior.

A3 PG-first ratchet: the sweep's dedup flag
(``table_registry.mark_semantic_draft_pending`` / ``clear_semantic_draft_
pending``) is a Postgres-only column, so ``app/api/semantic_models.py``
gates the whole endpoint PG-only (``if not use_pg(): raise
RequiresPostgresBackend(...)``) — every behavior this file pins (dedup,
batch limiting, concurrency-cap degradation, applied/no_apply_call
detection, system-identity/surface wiring) can therefore only be exercised
against the Postgres backend. The DuckDB-side clean-501 behavior is covered
by ``tests/test_semantic_autodraft_sweep.py``.

Uses ``state_backend`` + ``seeded_app_both`` (the dual-backend endpoint
harness) and skips the DuckDB param per the fixture's own documented
pattern for PG-only assertions — matches
``tests/db_pg/test_semantic_autodraft_pg.py``.

Two mocking strategies, by design (unchanged from the pre-A3 file):

- Most tests here mock ``app.chat.headless.run_one_shot`` itself (the same
  seam ``tests/test_agent_responses_api.py`` uses) — they exercise the
  sweep's OWN logic: dedup, batch limiting, concurrency-cap degradation,
  applied/no_apply_call detection.
- ``TestSystemIdentityAndSurface`` instead installs a minimal fake
  ``ChatManager`` and lets the REAL ``run_one_shot`` run against it, so the
  user_email/surface/profile wiring is exercised through production code
  rather than re-asserted against a mock's captured kwargs.
"""

from __future__ import annotations

import pytest

from app.auth.system_users import SEMANTIC_DRAFTER_USER_EMAIL
from app.chat.manager import ConcurrencyCapHit, set_current_chat_manager
from app.chat.types import Surface


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register_uncovered(id_: str) -> None:
    from src.repositories import table_registry_repo

    table_registry_repo().register(id=id_, name=id_, source_type="local", query_mode="local")


def _skip_unless_pg(state_backend) -> None:
    if state_backend != "pg":
        pytest.skip("PG-only")


def _backdate_stamp(table_id: str, seconds: float) -> None:
    """Age a table's ``semantic_draft_pending_at`` by ``seconds``.

    Written straight to the column (rather than through a repo method) on
    purpose: no production code ever backdates a stamp, so a repo setter
    for it would exist only for tests. The passage of time is what the
    re-eligibility TTL is about, and this is the only way to simulate it
    without sleeping for days.
    """
    import datetime as _dt

    import sqlalchemy as sa

    from src.db_pg import get_engine

    with get_engine().begin() as conn:
        conn.execute(
            sa.text("UPDATE table_registry SET semantic_draft_pending_at = :ts WHERE id = :id"),
            {
                "ts": _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=seconds),
                "id": table_id,
            },
        )


@pytest.fixture
def no_chat_manager():
    """Ensure no manager is installed — the sweep's chat-disabled path."""
    set_current_chat_manager(None)
    yield
    set_current_chat_manager(None)


@pytest.fixture
def fake_chat_manager():
    """A non-None sentinel manager — enough to pass the ``manager is None``
    gate when ``run_one_shot`` itself is mocked and never touches it."""
    manager = object()
    set_current_chat_manager(manager)
    yield manager
    set_current_chat_manager(None)


def _patch_run_one_shot(
    monkeypatch, *, raise_cap_for=(), apply_for=(), raise_error_for=(), timeout_for=(), calls=None
):
    """Mock ``app.chat.headless.run_one_shot`` — the sweep endpoint imports
    it locally per-request, so patching the module attribute (not any
    importer's copy) is the correct seam, mirroring
    ``tests/test_agent_responses_api.py``.

    ``raise_cap_for`` / ``apply_for`` / ``raise_error_for`` / ``timeout_for``
    are iterables of substrings matched against the rendered prompt (which
    always embeds the table id) to decide, per call, whether to raise
    ``ConcurrencyCapHit``, simulate an ``apply_semantic_model`` call by
    writing a real ``authoring_suggestions`` row, raise an arbitrary non-cap
    error (a broker/LLM/spawn failure), or RETURN ``timed_out=True`` —
    which ``run_one_shot`` does without raising, the sandbox still churning
    on the turn after it returns.
    """
    from app.chat import headless

    calls = calls if calls is not None else []

    async def _fake(manager, *, user_email, agent_id, prompt, timeout_s, owner_user_id=None, profile=None):
        calls.append(
            {
                "user_email": user_email,
                "agent_id": agent_id,
                "prompt": prompt,
                "timeout_s": timeout_s,
                "profile": profile,
            }
        )
        if any(marker in prompt for marker in raise_cap_for):
            raise ConcurrencyCapHit("cap")
        if any(marker in prompt for marker in raise_error_for):
            raise RuntimeError("broker exploded")
        if any(marker in prompt for marker in apply_for):
            from src.repositories import authoring_suggestions_repo

            authoring_suggestions_repo().create(
                domain="semantic-layer",
                payload={"slug": "x", "document": "irrelevant"},
                created_by=user_email,
            )
        if any(marker in prompt for marker in timeout_for):
            return {"chat_id": "chat-fake", "answer": "", "timed_out": True}
        return {"chat_id": "chat-fake", "answer": "ok", "timed_out": False}

    monkeypatch.setattr(headless, "run_one_shot", _fake)
    return calls


class TestNoChatManager:
    def test_chat_disabled_triggers_nothing(self, state_backend, seeded_app_both, no_chat_manager):
        _skip_unless_pg(state_backend)
        _register_uncovered("t1")
        c = seeded_app_both["client"]
        r = c.post("/api/admin/semantic-auto-draft-sweep", headers=_auth(seeded_app_both["admin_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body == {
            "triggered": 0,
            "applied": 0,
            "no_apply_call": 0,
            "timed_out": 0,
            "skipped_cap": 0,
            "errored": 0,
            "remaining": 1,
        }

        from src.repositories import table_registry_repo

        assert table_registry_repo().get("t1")["semantic_draft_pending_at"] is None


class TestDedup:
    def test_covered_table_is_never_triggered(self, state_backend, seeded_app_both, fake_chat_manager, monkeypatch):
        _skip_unless_pg(state_backend)
        from datetime import datetime, timezone

        from src.repositories import semantic_model_repo, table_registry_repo

        table_registry_repo().register(id="hidden", name="hidden", source_type="local")
        _register_uncovered("visible")
        semantic_model_repo().upsert(
            id="manual/_/m",
            slug="m",
            name="m",
            description=None,
            document="",
            document_json={"semantic_model": [{"name": "m", "datasets": [{"name": "hidden", "source": "hidden"}]}]},
            spec_version="0.2.0.dev0",
            content_hash="h",
            source="manual",
            source_ref=None,
            status="valid",
            validation_errors=None,
            validated_at=datetime.now(timezone.utc),
        )
        calls = _patch_run_one_shot(monkeypatch)

        c = seeded_app_both["client"]
        r = c.post("/api/admin/semantic-auto-draft-sweep", headers=_auth(seeded_app_both["admin_token"]))
        assert r.status_code == 200, r.text
        assert r.json()["triggered"] == 1
        assert len(calls) == 1
        assert "visible" in calls[0]["prompt"]
        assert "hidden" not in calls[0]["prompt"]

    def test_already_pending_table_is_skipped(self, state_backend, seeded_app_both, fake_chat_manager, monkeypatch):
        _skip_unless_pg(state_backend)
        from src.repositories import table_registry_repo

        _register_uncovered("t1")
        table_registry_repo().mark_semantic_draft_pending("t1")
        calls = _patch_run_one_shot(monkeypatch)

        c = seeded_app_both["client"]
        r = c.post("/api/admin/semantic-auto-draft-sweep", headers=_auth(seeded_app_both["admin_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["triggered"] == 0
        assert body["remaining"] == 0
        assert calls == []

    def test_selected_table_is_stamped_pending_before_the_session_runs(
        self, state_backend, seeded_app_both, fake_chat_manager, monkeypatch
    ):
        """The stamp must be visible while the session is running — that is
        what stops a concurrent tick picking the same table up. Asserted
        from INSIDE the mocked session rather than after the response,
        because a session that files no suggestion has its stamp cleared
        again on the way out (``TestNoApplyCallDegradation``)."""
        _skip_unless_pg(state_backend)
        from app.chat import headless
        from src.repositories import table_registry_repo

        _register_uncovered("t1")
        seen: list = []

        async def _fake(manager, *, user_email, agent_id, prompt, timeout_s, owner_user_id=None, profile=None):
            seen.append(table_registry_repo().get("t1")["semantic_draft_pending_at"])
            return {"chat_id": "chat-fake", "answer": "ok", "timed_out": False}

        monkeypatch.setattr(headless, "run_one_shot", _fake)

        c = seeded_app_both["client"]
        r = c.post("/api/admin/semantic-auto-draft-sweep", headers=_auth(seeded_app_both["admin_token"]))
        assert r.status_code == 200, r.text
        assert seen and seen[0] is not None


class TestBatchLimit:
    def test_only_first_n_are_triggered_rest_are_remaining(
        self, state_backend, seeded_app_both, fake_chat_manager, monkeypatch
    ):
        _skip_unless_pg(state_backend)
        import app.api.semantic_models as sm

        for i in range(sm._SWEEP_BATCH_SIZE + 2):
            _register_uncovered(f"t{i}")
        calls = _patch_run_one_shot(monkeypatch)

        c = seeded_app_both["client"]
        r = c.post("/api/admin/semantic-auto-draft-sweep", headers=_auth(seeded_app_both["admin_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["triggered"] == sm._SWEEP_BATCH_SIZE
        assert body["remaining"] == 2
        assert len(calls) == sm._SWEEP_BATCH_SIZE


class TestConcurrencyCapDegradation:
    def test_cap_hit_is_counted_not_raised(self, state_backend, seeded_app_both, fake_chat_manager, monkeypatch):
        _skip_unless_pg(state_backend)
        _register_uncovered("capped")
        _register_uncovered("fine")
        _patch_run_one_shot(monkeypatch, raise_cap_for=("capped",))

        c = seeded_app_both["client"]
        r = c.post("/api/admin/semantic-auto-draft-sweep", headers=_auth(seeded_app_both["admin_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["skipped_cap"] == 1
        assert body["triggered"] == 1
        assert body["no_apply_call"] == 1

    def test_cap_hit_table_stays_eligible_for_a_later_tick(
        self, state_backend, seeded_app_both, fake_chat_manager, monkeypatch
    ):
        """A capped table's dedup stamp must be cleared again on the way out.

        The cap is enforced inside ``ChatManager.create_session``, before the
        prompt is ever sent, so a capped table's session never started and no
        ``authoring_suggestions`` row will ever exist to clear its flag on
        resolution. Left stamped, the table is filtered out of every later
        tick's candidates and is never drafted again without an admin
        clearing it by hand (Devin review).
        """
        _skip_unless_pg(state_backend)
        from src.repositories import table_registry_repo

        _register_uncovered("capped")
        _register_uncovered("fine")
        _patch_run_one_shot(monkeypatch, raise_cap_for=("capped",), apply_for=("fine",))

        c = seeded_app_both["client"]
        r = c.post("/api/admin/semantic-auto-draft-sweep", headers=_auth(seeded_app_both["admin_token"]))
        assert r.status_code == 200, r.text
        assert r.json()["skipped_cap"] == 1

        registry = table_registry_repo()
        assert registry.get("capped")["semantic_draft_pending_at"] is None
        # The table whose session FILED a suggestion keeps its stamp — that
        # one has an admin resolution coming to clear it. (A session that
        # files nothing is un-stamped too; see TestNoApplyCallDegradation.)
        assert registry.get("fine")["semantic_draft_pending_at"] is not None

        # And it really is picked up again: a second tick re-triggers it.
        calls = _patch_run_one_shot(monkeypatch)
        r2 = c.post("/api/admin/semantic-auto-draft-sweep", headers=_auth(seeded_app_both["admin_token"]))
        assert r2.status_code == 200, r2.text
        assert r2.json()["triggered"] == 1
        assert len(calls) == 1 and "capped" in calls[0]["prompt"]


class TestSessionErrorDegradation:
    """A non-cap session failure is the same stuck-flag hazard, one step
    wider: nothing would ever clear the stamp, so the table would be
    filtered out of every later tick silently — and one bad table would
    500 the whole request, abandoning the rest of the batch (Devin review).
    """

    def test_session_error_is_counted_not_raised_and_does_not_strand_the_batch(
        self, state_backend, seeded_app_both, fake_chat_manager, monkeypatch
    ):
        _skip_unless_pg(state_backend)
        from src.repositories import table_registry_repo

        _register_uncovered("boom")
        _register_uncovered("fine")
        # `fine` FILES a suggestion so its stamp survives the tick — a
        # silent session is un-stamped too (TestNoApplyCallDegradation),
        # which would blur what this test is pinning.
        calls = _patch_run_one_shot(monkeypatch, raise_error_for=("boom",), apply_for=("fine",))

        c = seeded_app_both["client"]
        r = c.post("/api/admin/semantic-auto-draft-sweep", headers=_auth(seeded_app_both["admin_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["errored"] == 1
        # The failure did not abandon the rest of the batch.
        assert body["triggered"] == 1
        assert len(calls) == 2

        registry = table_registry_repo()
        assert registry.get("boom")["semantic_draft_pending_at"] is None
        assert registry.get("fine")["semantic_draft_pending_at"] is not None

    def test_errored_table_is_retried_on_a_later_tick(
        self, state_backend, seeded_app_both, fake_chat_manager, monkeypatch
    ):
        _skip_unless_pg(state_backend)
        _register_uncovered("boom")
        _patch_run_one_shot(monkeypatch, raise_error_for=("boom",))

        c = seeded_app_both["client"]
        h = _auth(seeded_app_both["admin_token"])
        assert c.post("/api/admin/semantic-auto-draft-sweep", headers=h).json()["errored"] == 1

        calls = _patch_run_one_shot(monkeypatch)
        r2 = c.post("/api/admin/semantic-auto-draft-sweep", headers=h)
        assert r2.status_code == 200, r2.text
        assert r2.json()["triggered"] == 1
        assert len(calls) == 1 and "boom" in calls[0]["prompt"]


class TestAppliedDetection:
    def test_a_session_that_applies_counts_as_applied(
        self, state_backend, seeded_app_both, fake_chat_manager, monkeypatch
    ):
        _skip_unless_pg(state_backend)
        _register_uncovered("drafts_ok")
        _register_uncovered("asks_a_question")
        _patch_run_one_shot(monkeypatch, apply_for=("drafts_ok",))

        c = seeded_app_both["client"]
        r = c.post("/api/admin/semantic-auto-draft-sweep", headers=_auth(seeded_app_both["admin_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["triggered"] == 2
        assert body["applied"] == 1
        assert body["no_apply_call"] == 1


class _FakeSession:
    def __init__(self, id_: str) -> None:
        self.id = id_


class _FakeManager:
    """Minimal ChatManager double driving the REAL
    app.chat.headless.run_one_shot end to end, so the sweep's system-
    identity/surface wiring is exercised through production code rather
    than re-implemented in the test."""

    def __init__(self) -> None:
        self.create_session_calls: list[dict] = []
        self._sinks: dict[str, object] = {}
        self._next_id = 0

    async def create_session(self, *, user_email, surface, agent_id=None, profile=None):
        self.create_session_calls.append(
            {"user_email": user_email, "surface": surface, "agent_id": agent_id, "profile": profile}
        )
        self._next_id += 1
        return _FakeSession(f"chat-{self._next_id}")

    async def attach(self, chat_id, sink, is_primary=True):
        self._sinks[chat_id] = sink

    async def send_user_message(self, chat_id, prompt, sender_email=None):
        sink = self._sinks[chat_id]
        await sink.send_json({"type": "assistant_message", "content": "drafted"})
        await sink.send_json({"type": "done"})

    async def detach_sink(self, chat_id, sink):
        self._sinks.pop(chat_id, None)


class TestSystemIdentityAndSurface:
    def test_run_one_shot_is_invoked_with_the_drafter_identity_and_api_surface(self, state_backend, seeded_app_both):
        _skip_unless_pg(state_backend)
        _register_uncovered("t1")
        manager = _FakeManager()
        set_current_chat_manager(manager)
        try:
            c = seeded_app_both["client"]
            r = c.post("/api/admin/semantic-auto-draft-sweep", headers=_auth(seeded_app_both["admin_token"]))
            assert r.status_code == 200, r.text
        finally:
            set_current_chat_manager(None)

        assert len(manager.create_session_calls) == 1
        call = manager.create_session_calls[0]
        assert call["user_email"] == SEMANTIC_DRAFTER_USER_EMAIL
        assert call["surface"] == Surface.API
        assert call["profile"] == "semantic-model-builder"
        # No named agent — this is a raw profile session, not an agent-as-API run.
        assert call["agent_id"] is None

    def test_ensures_the_drafter_user_row_exists(self, state_backend, seeded_app_both):
        _skip_unless_pg(state_backend)
        _register_uncovered("t1")
        manager = _FakeManager()
        set_current_chat_manager(manager)
        try:
            c = seeded_app_both["client"]
            r = c.post("/api/admin/semantic-auto-draft-sweep", headers=_auth(seeded_app_both["admin_token"]))
            assert r.status_code == 200, r.text
        finally:
            set_current_chat_manager(None)

        from src.repositories import users_repo

        assert users_repo().get_by_email(SEMANTIC_DRAFTER_USER_EMAIL) is not None


class TestNoApplyCallDegradation:
    """A session that ran cleanly but produced no suggestion (the
    ``no_apply_call`` branch) KEEPS its stamp — and the stamp's age, not an
    immediate clear, is what makes the table eligible again.

    An immediate clear made the table eligible on the very next tick. With
    ``list_all()``'s stable order and a batch of ``_SWEEP_BATCH_SIZE``, a
    handful of tables the drafter keeps declining then occupy the whole
    batch forever and no table behind them is ever reached (head-of-line
    blocking). Keeping the stamp and treating one older than
    ``_SWEEP_STAMP_RETRY_AFTER_S`` as eligible again gets both: a declined
    table does retry eventually, and it does not starve fresh ones
    meanwhile.
    """

    def test_a_session_that_files_no_suggestion_keeps_its_stamp(
        self, state_backend, seeded_app_both, fake_chat_manager, monkeypatch
    ):
        _skip_unless_pg(state_backend)
        from src.repositories import table_registry_repo

        _register_uncovered("quiet")
        _register_uncovered("drafts_ok")
        _patch_run_one_shot(monkeypatch, apply_for=("drafts_ok",))

        c = seeded_app_both["client"]
        r = c.post("/api/admin/semantic-auto-draft-sweep", headers=_auth(seeded_app_both["admin_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["applied"] == 1
        assert body["no_apply_call"] == 1

        registry = table_registry_repo()
        assert registry.get("quiet")["semantic_draft_pending_at"] is not None
        assert registry.get("drafts_ok")["semantic_draft_pending_at"] is not None

    def test_the_table_is_not_retried_on_the_very_next_tick(
        self, state_backend, seeded_app_both, fake_chat_manager, monkeypatch
    ):
        """Head-of-line blocking, pinned from the declined table's side."""
        _skip_unless_pg(state_backend)
        _register_uncovered("quiet")
        _patch_run_one_shot(monkeypatch)

        c = seeded_app_both["client"]
        h = _auth(seeded_app_both["admin_token"])
        assert c.post("/api/admin/semantic-auto-draft-sweep", headers=h).json()["no_apply_call"] == 1

        calls = _patch_run_one_shot(monkeypatch)
        r2 = c.post("/api/admin/semantic-auto-draft-sweep", headers=h)
        assert r2.status_code == 200, r2.text
        assert r2.json()["triggered"] == 0
        assert calls == []

    def test_the_table_is_eligible_again_once_the_stamp_is_older_than_the_ttl(
        self, state_backend, seeded_app_both, fake_chat_manager, monkeypatch
    ):
        _skip_unless_pg(state_backend)
        import app.api.semantic_models as sm

        _register_uncovered("quiet")
        _patch_run_one_shot(monkeypatch)

        c = seeded_app_both["client"]
        h = _auth(seeded_app_both["admin_token"])
        assert c.post("/api/admin/semantic-auto-draft-sweep", headers=h).json()["no_apply_call"] == 1

        _backdate_stamp("quiet", sm._SWEEP_STAMP_RETRY_AFTER_S + 60)

        calls = _patch_run_one_shot(monkeypatch)
        r2 = c.post("/api/admin/semantic-auto-draft-sweep", headers=h)
        assert r2.status_code == 200, r2.text
        assert r2.json()["triggered"] == 1
        assert len(calls) == 1 and "quiet" in calls[0]["prompt"]

    def test_a_stamp_just_under_the_ttl_is_still_ineligible(
        self, state_backend, seeded_app_both, fake_chat_manager, monkeypatch
    ):
        """The boundary, from the other side — otherwise "eligible after the
        TTL" would also pass for an implementation that ignores the stamp."""
        _skip_unless_pg(state_backend)
        import app.api.semantic_models as sm

        _register_uncovered("quiet")
        _patch_run_one_shot(monkeypatch)

        c = seeded_app_both["client"]
        h = _auth(seeded_app_both["admin_token"])
        assert c.post("/api/admin/semantic-auto-draft-sweep", headers=h).json()["no_apply_call"] == 1

        _backdate_stamp("quiet", sm._SWEEP_STAMP_RETRY_AFTER_S - 3600)

        calls = _patch_run_one_shot(monkeypatch)
        r2 = c.post("/api/admin/semantic-auto-draft-sweep", headers=h)
        assert r2.status_code == 200, r2.text
        assert r2.json()["triggered"] == 0
        assert calls == []


class TestTimedOutSession:
    """``run_one_shot`` reports a wait timeout by RETURNING
    ``{"timed_out": True}`` — it does not raise, and the sandbox keeps
    processing the turn after it returns (``app/chat/headless.py``).

    The sweep used to discard that return and read its pending-suggestion
    diff immediately, so every session slower than
    ``_SWEEP_SESSION_TIMEOUT_S`` looked like "filed nothing", got
    un-stamped, and then filed its suggestion in the background — leaving
    the table eligible again and re-drafted on every following tick, one
    duplicate pending suggestion per tick.
    """

    def test_a_timed_out_session_keeps_its_stamp(
        self, state_backend, seeded_app_both, fake_chat_manager, monkeypatch
    ):
        _skip_unless_pg(state_backend)
        from src.repositories import table_registry_repo

        _register_uncovered("slow")
        _patch_run_one_shot(monkeypatch, timeout_for=("slow",))

        c = seeded_app_both["client"]
        r = c.post("/api/admin/semantic-auto-draft-sweep", headers=_auth(seeded_app_both["admin_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["timed_out"] == 1
        assert body["no_apply_call"] == 0
        assert body["errored"] == 0

        assert table_registry_repo().get("slow")["semantic_draft_pending_at"] is not None

    def test_a_timed_out_table_is_not_re_drafted_on_the_next_tick(
        self, state_backend, seeded_app_both, fake_chat_manager, monkeypatch
    ):
        """The duplicate-suggestion loop, end to end: the background session
        may still file, so a second tick must not start a second one."""
        _skip_unless_pg(state_backend)
        _register_uncovered("slow")
        _patch_run_one_shot(monkeypatch, timeout_for=("slow",))

        c = seeded_app_both["client"]
        h = _auth(seeded_app_both["admin_token"])
        assert c.post("/api/admin/semantic-auto-draft-sweep", headers=h).json()["timed_out"] == 1

        calls = _patch_run_one_shot(monkeypatch, timeout_for=("slow",))
        r2 = c.post("/api/admin/semantic-auto-draft-sweep", headers=h)
        assert r2.status_code == 200, r2.text
        assert r2.json()["triggered"] == 0
        assert calls == []

    def test_a_timed_out_session_that_already_filed_counts_as_applied(
        self, state_backend, seeded_app_both, fake_chat_manager, monkeypatch
    ):
        """Filing happens mid-turn, so a suggestion can land before the wait
        times out. That is an ``applied``, not a bare timeout."""
        _skip_unless_pg(state_backend)
        _register_uncovered("slow")
        _patch_run_one_shot(monkeypatch, timeout_for=("slow",), apply_for=("slow",))

        c = seeded_app_both["client"]
        r = c.post("/api/admin/semantic-auto-draft-sweep", headers=_auth(seeded_app_both["admin_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["applied"] == 1
        assert body["timed_out"] == 0


class TestCandidateOrdering:
    """Fresh (never-stamped) tables take the batch ahead of tables whose
    stamp merely aged past the TTL — the other half of the head-of-line fix.
    Without it, a backlog of repeatedly-declined tables would reclaim the
    batch the moment their stamps expired, ahead of tables never tried once.
    """

    def test_fresh_tables_are_drafted_before_stale_stamped_ones(
        self, state_backend, seeded_app_both, fake_chat_manager, monkeypatch
    ):
        _skip_unless_pg(state_backend)
        import app.api.semantic_models as sm

        # `a_*` sort ahead of `z_*` in list_all()'s stable order, so a naive
        # "stamp is old enough → eligible" filter WITHOUT ordering would put
        # the stale ones first and starve the fresh ones.
        from src.repositories import table_registry_repo

        for i in range(sm._SWEEP_BATCH_SIZE):
            _register_uncovered(f"a_stale_{i}")
            table_registry_repo().mark_semantic_draft_pending(f"a_stale_{i}")
            _backdate_stamp(f"a_stale_{i}", sm._SWEEP_STAMP_RETRY_AFTER_S + 60)
        for i in range(sm._SWEEP_BATCH_SIZE):
            _register_uncovered(f"z_fresh_{i}")

        calls = _patch_run_one_shot(monkeypatch)
        c = seeded_app_both["client"]
        r = c.post("/api/admin/semantic-auto-draft-sweep", headers=_auth(seeded_app_both["admin_token"]))
        assert r.status_code == 200, r.text
        assert r.json()["triggered"] == sm._SWEEP_BATCH_SIZE

        drafted = [call["prompt"] for call in calls]
        assert all("z_fresh" in prompt for prompt in drafted), drafted

    def test_stale_stamped_tables_are_drafted_once_no_fresh_ones_are_left(
        self, state_backend, seeded_app_both, fake_chat_manager, monkeypatch
    ):
        _skip_unless_pg(state_backend)
        import app.api.semantic_models as sm

        from src.repositories import table_registry_repo

        _register_uncovered("a_stale")
        table_registry_repo().mark_semantic_draft_pending("a_stale")
        _backdate_stamp("a_stale", sm._SWEEP_STAMP_RETRY_AFTER_S + 60)

        calls = _patch_run_one_shot(monkeypatch)
        c = seeded_app_both["client"]
        r = c.post("/api/admin/semantic-auto-draft-sweep", headers=_auth(seeded_app_both["admin_token"]))
        assert r.status_code == 200, r.text
        assert r.json()["triggered"] == 1
        assert len(calls) == 1 and "a_stale" in calls[0]["prompt"]

    def test_a_stale_stamped_table_is_re_stamped_when_it_is_retried(
        self, state_backend, seeded_app_both, fake_chat_manager, monkeypatch
    ):
        """Re-stamping is what stops a retried table from being eligible
        forever once its first stamp expires."""
        _skip_unless_pg(state_backend)
        import datetime as dt

        import app.api.semantic_models as sm

        from src.repositories import table_registry_repo

        _register_uncovered("a_stale")
        table_registry_repo().mark_semantic_draft_pending("a_stale")
        _backdate_stamp("a_stale", sm._SWEEP_STAMP_RETRY_AFTER_S + 60)

        _patch_run_one_shot(monkeypatch)
        c = seeded_app_both["client"]
        r = c.post("/api/admin/semantic-auto-draft-sweep", headers=_auth(seeded_app_both["admin_token"]))
        assert r.status_code == 200, r.text

        stamp = table_registry_repo().get("a_stale")["semantic_draft_pending_at"]
        assert stamp is not None
        age = (dt.datetime.now(dt.timezone.utc) - stamp).total_seconds()
        assert age < 60, age
