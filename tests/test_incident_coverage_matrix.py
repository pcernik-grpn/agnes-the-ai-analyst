"""Incident-coverage ratchet (Task 10 of the 2026-07-14
chat-sandbox-secret-broker plan, spec §7.4).

Maps each incident-closure / guarantee acceptance-criteria (AC) id from the
design spec to the real ``path::test_name`` node that satisfies it, and
asserts that node actually exists and is not disabled via a bare
``@pytest.mark.skip`` (the sandbox-tier tests are legitimately
``@pytest.mark.skipif(not AGNES_E2E_DOCKER, ...)``-gated — that's a manual
operator gate, not a disabled test, so skipif is fine).

This is a ratchet, not a full spec sweep: if a future edit renames or
deletes one of these tests without updating ``REQUIRED``, this file fails
loudly instead of silently losing coverage for a closed incident.
"""

from __future__ import annotations

import ast
from pathlib import Path

# AC id -> "path::test_name" of the real test that satisfies it. Node names
# were confirmed against the committed test files (grep -n "def test_"), not
# copied from the plan's illustrative names.
REQUIRED: dict[str, str] = {
    # Sandbox-tier adversarial suite (Task 11) — manual AGNES_E2E_DOCKER
    # gate (docker-provider sandboxes; ported from the e2b tier).
    # AC-F3 and AC-F4c were removed here 2026-08: they proved the E2B
    # VM-level ``network.allow_out`` egress firewall specifically, and that
    # mechanism was deleted with the e2b provider. The docker analogue
    # (chat.docker_egress_mode none/allowlist) is covered by
    # tests/test_chat_docker_provider.py + the services/egress_proxy tests.
    "AC-F-nosecret": "tests/e2e/test_adversarial.py::test_no_secret_anywhere",
    "AC-F-allowed-sink": "tests/e2e/test_adversarial.py::test_no_exfil_via_allowlisted_host",
    # PreToolUse hook hardening (Task 3) — unit-tier, per-PR.
    "AC-G-schemeless": "tests/test_pre_tool_use_hook.py::test_schemeless_curl_denied",
    # Broker routes (Task 6) — app-tier, per-PR.
    "AC-G-ticket-reuse": "tests/test_broker_routes.py::test_expired_ticket_401",
    "AC-G-rbac-fidelity": "tests/test_broker_routes.py::test_agnes_api_replay_uses_live_rbac",
    # §11: admin mutations are gated by the target route's require_admin
    # dependency, not a path prefix — a require_admin route off /api/admin/
    # (e.g. /api/sync/trigger) is rejected even for an admin identity.
    "AC-G-admin": "tests/test_broker_routes.py::test_admin_route_off_admin_prefix_rejected",
    # §11: a co-session's broker replay mints a co_session JWT (live
    # grant-intersection), never the single stored owner's identity.
    "AC-G-cosession": "tests/test_broker_routes.py::test_cosession_ticket_mints_cosession_jwt",
    # §11 / RBAC review #849: absolute-URL, protocol-relative, backslash and
    # %2f-encoded path smuggling cannot make the ASGI replay dispatch to an
    # admin route past the gate — the gate and dispatch share one normalized path.
    "AC-G-smuggle": "tests/test_broker_routes.py::test_admin_route_path_smuggling_rejected",
    # Manager spawn-env static guard (Task 9) — unit-tier, per-PR.
    "AC-G-noinject": "tests/test_backend_split_guard.py::test_no_real_secret_in_sandbox_spawn_env",
    # Route-auth guard (Task 10, this task) — app-tier, per-PR.
    "AC-G-route-auth": "tests/test_route_auth_guard.py::test_all_routes_authenticated",
}


def _find_test_function(path: Path, name: str) -> ast.FunctionDef | None:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _decorator_dotted_name(dec: ast.expr) -> str:
    """Render a decorator expression's dotted call target, e.g. a
    ``@pytest.mark.skipif(...)`` decorator renders as ``pytest.mark.skipif``
    (args are ignored — only the callable identity matters here)."""
    target = dec.func if isinstance(dec, ast.Call) else dec
    parts: list[str] = []
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if isinstance(target, ast.Name):
        parts.append(target.id)
    return ".".join(reversed(parts))


def _is_bare_skip(fn: ast.FunctionDef) -> bool:
    """True if decorated with an unconditional ``@pytest.mark.skip`` (as
    opposed to ``@pytest.mark.skipif``, which is a legitimate manual-gate
    marker used by the sandbox-tier tests)."""
    return any(_decorator_dotted_name(dec) == "pytest.mark.skip" for dec in fn.decorator_list)


def test_every_required_ac_has_a_test():
    missing: list[str] = []
    skipped: list[str] = []
    for ac, node in REQUIRED.items():
        path_str, _, name = node.partition("::")
        path = Path(path_str)
        if not path.exists():
            missing.append(f"{ac}: no such file {path_str} (node: {node})")
            continue
        fn = _find_test_function(path, name)
        if fn is None:
            missing.append(f"{ac}: no such test function {node}")
            continue
        if _is_bare_skip(fn):
            skipped.append(f"{ac}: {node} is @pytest.mark.skip-decorated (unconditionally disabled)")

    assert not missing, "Incident-coverage gap(s) — required test node(s) not found:\n  " + "\n  ".join(missing)
    assert not skipped, "Incident-coverage gap(s) — required test node(s) unconditionally skipped:\n  " + "\n  ".join(
        skipped
    )
