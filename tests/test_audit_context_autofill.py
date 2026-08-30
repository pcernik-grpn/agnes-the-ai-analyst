"""Request-context autofill for audit rows (F0 — Task 1, step 5-6).

``AuditRepository.log()`` (both backends) fills ``client_ip``,
``correlation_id`` and ``client_kind`` from the ``src.audit_context``
contextvars when the caller passed ``None`` — explicit kwargs always win.
"""

from src import audit_context


def test_log_autofills_context_fields(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.repositories import audit_repo

    audit_context.set_request_meta(client_ip="203.0.113.7", correlation_id="rid-abc123")
    audit_context.set_client_kind("mcp")
    audit_repo().log(user_id="u1", action="catalog.list")
    rows, _ = audit_repo().query(action="catalog.list", limit=1)
    assert rows[0]["client_ip"] == "203.0.113.7"
    assert rows[0]["correlation_id"] == "rid-abc123"
    assert rows[0]["client_kind"] == "mcp"


def test_explicit_kwargs_beat_context(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.repositories import audit_repo

    audit_context.set_request_meta(client_ip="203.0.113.7", correlation_id="rid-abc123")
    audit_repo().log(user_id="u1", action="catalog.list", client_ip="198.51.100.9")
    rows, _ = audit_repo().query(action="catalog.list", limit=1)
    assert rows[0]["client_ip"] == "198.51.100.9"


def test_audit_written_marker(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.repositories import audit_repo

    # The counter is per-request: the middleware installs one at the start of
    # every HTTP request. Outside a request (scheduler, worker, CLI) there is
    # nothing to duplicate, so counting is deliberately a no-op.
    audit_context.begin_request_write_tracking()
    before = audit_context.audit_written_count()
    audit_repo().log(user_id="u1", action="catalog.list")
    assert audit_context.audit_written_count() == before + 1


def test_audit_written_marker_is_a_noop_outside_a_request(tmp_path, monkeypatch, shared_app):
    """A scheduler/worker write must not need — or fake — a request scope."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.repositories import audit_repo

    audit_context._reset_for_tests()
    audit_repo().log(user_id=None, action="run_audit_prune")
    assert audit_context.audit_written_count() == 0
