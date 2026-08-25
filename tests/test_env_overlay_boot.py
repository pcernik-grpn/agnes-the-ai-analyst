"""The ``.env_overlay`` boot-load must OVERRIDE image-baked env vars.

The overlay is the admin's persisted runtime configuration — secrets set via
``/api/admin/configure`` and the chat "configure secrets" UI (ANTHROPIC_API_KEY,
ANTHROPIC_API_KEY, marketplace PATs). ``persist_overlay_token`` writes both the file
and ``os.environ[k] = v`` (a hard override) in the live process. The boot-load
in ``create_app`` must apply the SAME precedence, otherwise a stale baked key
already occupying ``os.environ`` shadows the overlay and, on the next restart,
a UI-rotated key is silently discarded — chat then breaks with a 401.
"""

from __future__ import annotations


def test_env_overlay_overrides_baked_env_on_boot(e2e_env, monkeypatch):
    from app.main import create_app

    # A stale/invalid value already in the container env (image-baked / .env).
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-stale-baked-invalid")

    # The admin's persisted overlay carries the freshly-rotated key.
    overlay = e2e_env["data_dir"] / "state" / ".env_overlay"
    overlay.write_text("ANTHROPIC_API_KEY=sk-overlay-rotated-valid\n", encoding="utf-8")

    create_app()

    import os

    assert os.environ["ANTHROPIC_API_KEY"] == "sk-overlay-rotated-valid"


def test_env_overlay_sets_key_absent_from_baked_env(e2e_env, monkeypatch):
    """Sanity: overlay still populates a key that isn't in the baked env."""
    from app.main import create_app

    monkeypatch.delenv("SENDGRID_API_KEY", raising=False)
    overlay = e2e_env["data_dir"] / "state" / ".env_overlay"
    overlay.write_text("SENDGRID_API_KEY=sg-from-overlay\n", encoding="utf-8")

    create_app()

    import os

    assert os.environ["SENDGRID_API_KEY"] == "sg-from-overlay"
