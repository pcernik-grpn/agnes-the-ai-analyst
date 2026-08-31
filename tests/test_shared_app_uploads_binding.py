"""Ratchet for the one thing the session-shared ``seeded_app`` cannot give you.

``_shared_seeded_app`` is session-scoped and does not depend on ``e2e_env``, so
``create_app()`` runs before any test's DATA_DIR monkeypatch. Almost nothing
cares — DB access resolves DATA_DIR at call time — but ``/uploads`` is mounted
as ``StaticFiles(directory=${DATA_DIR}/uploads)`` with the path frozen into the
mount at construction. A ``seeded_app`` test that writes a file into its own
DATA_DIR and then GETs it back would read a different directory and miss.

That is invisible today because no such test exists. This guard is what makes
it stay visible: the moment one is written, this fails and points at
``seeded_app_fresh``, which builds the app after ``e2e_env`` has run.
"""

from __future__ import annotations

import re
from pathlib import Path

TESTS = Path(__file__).parent


def test_no_seeded_app_test_fetches_from_the_uploads_mount():
    offenders = []
    for path in sorted(TESTS.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        src = path.read_text(encoding="utf-8", errors="replace")
        # Negative lookahead excludes `seeded_app_fresh` -- the sanctioned
        # alternative this assertion itself recommends below -- so a file
        # that uses only the fresh fixture doesn't trip its own fix.
        if not re.search(r"seeded_app(?!_fresh)", src):
            continue
        # A GET/HEAD against the mount, however the client is spelled.
        if re.search(r"""\.(get|head)\(\s*f?["']/uploads/""", src):
            offenders.append(path.name)

    assert not offenders, (
        "these tests use the session-shared `seeded_app` and fetch from the "
        f"/uploads mount: {offenders}. That mount is frozen to whatever "
        "DATA_DIR was active when pytest first built the shared app, not to "
        "the test's own tmp_path, so the read misses. Use `seeded_app_fresh`."
    )


def test_the_shared_fixture_docstring_still_names_the_binding():
    """The docstring is the only place this constraint is explained. If the
    fixture is rewritten and the explanation dropped, the next person re-learns
    it from a mysteriously failing upload test."""
    conftest = (TESTS / "conftest.py").read_text(encoding="utf-8")
    block = conftest[conftest.index("def _shared_seeded_app") :]
    block = block[: block.index("from app.main import create_app")]
    assert "/uploads" in block
    assert "session secret" in block
    assert "seeded_app_fresh" in block
