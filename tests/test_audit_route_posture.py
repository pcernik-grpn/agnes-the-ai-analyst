"""Route-posture ratchet (F1 — audit-full-coverage plan, Task 2).

Every mutating route registered on the app must declare its audit posture
in ``src/audit_posture.py`` — a real cataloged action, ``"fallback"``, or
``"exempt:<reason>"`` — so a NEW route can never silently reopen the
196/352-audited gap this plan closes. See ``src/audit_posture.py``'s module
docstring for the full contract.
"""

from __future__ import annotations

from src.audit_events import is_cataloged
from src.audit_posture import MUTATING, POSTURE


def _mutating_routes(app):
    out = set()
    for r in app.routes:
        methods = getattr(r, "methods", None) or set()
        for m in methods & set(MUTATING):
            out.add(f"{m} {r.path}")
    return out


def test_every_mutating_route_declares_posture(shared_app):
    routes = _mutating_routes(shared_app)
    undeclared = sorted(routes - POSTURE.keys())
    stale = sorted(POSTURE.keys() - routes)
    assert not undeclared, (
        "New mutating routes must declare audit posture in src/audit_posture.py "
        f"(a real action name, 'fallback', or 'exempt:<reason>'): {undeclared}"
    )
    assert not stale, f"Prune POSTURE, routes gone: {stale}"


def test_posture_values_are_valid():
    for key, v in POSTURE.items():
        assert v == "fallback" or v.startswith("exempt:") or is_cataloged(v), (key, v)


def test_mutating_is_the_standard_http_write_verbs():
    assert set(MUTATING) == {"POST", "PUT", "PATCH", "DELETE"}
