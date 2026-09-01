"""Regression tests: a multi-container deployment (app / scheduler / worker,
each its own Python process off the same image) must not need a restart to
observe a config change saved through the admin UI.

``app.instance_config`` caches the merged config in a module global for the
lifetime of the process, cleared explicitly by ``reset_cache()`` — which
``/api/admin/server-config`` calls after a save. That only clears the cache
IN THE PROCESS THAT HANDLED THE REQUEST. A sibling process (e.g. the
extraction worker) keeps serving the pre-save value until it happens to be
recreated, which is invisible and worst for exactly the settings a worker
consumes (extraction toggles, concurrency, model choice, timeouts) — see the
incident this file exists to pin: ``extraction.facts.enabled`` was flipped
on through the UI, the worker never saw it, and a 17-minute crawl over 1549
items reported ``done`` with the ``facts`` stage silently never having run.

The fix is an mtime+size fingerprint on the writable overlay file, checked
on every ``load_instance_config()`` call: a process that did not perform the
save still notices the file changed on disk and reloads, no restart needed.
"""

import os
from pathlib import Path

import pytest
import yaml


@pytest.fixture(autouse=True)
def _clean_instance_config_globals():
    """Full reset, not just ``reset_cache()``.

    ``reset_cache()`` deliberately does NOT clear ``_last_good_config`` /
    ``_loaded_once`` in production (see its docstring) — but these tests
    need to control that history explicitly, the same pattern
    ``tests/test_startup_instance_yaml_perms.py`` uses.
    """
    from app import instance_config as ic

    ic._instance_config = None
    ic._last_good_config = None
    ic._loaded_once = False
    ic._overlay_cache_fingerprint = None
    yield
    ic._instance_config = None
    ic._last_good_config = None
    ic._loaded_once = False
    ic._overlay_cache_fingerprint = None


def _write_overlay(data_dir: Path, payload: dict) -> Path:
    overlay_path = data_dir / "state" / "instance.yaml"
    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    overlay_path.write_text(yaml.dump(payload))
    return overlay_path


def _bump_mtime(path: Path) -> None:
    """Force a mtime strictly later than whatever it is now.

    A same-test double-write can land within one filesystem timestamp tick
    on some platforms/CI runners; asserting the fix works only when the
    clock happens to have ticked would make the test flaky rather than
    pinning the behavior. Production doesn't need this — real saves are
    seconds to hours apart — but the test wants to isolate "did the content
    change" from "did the clock tick enough since the last write".
    """
    st = path.stat()
    new_ns = st.st_mtime_ns + 1_000_000_000
    os.utime(path, ns=(new_ns, new_ns))


class TestOverlayChangeObservedWithoutRestart:
    """A process that never calls ``reset_cache()`` itself still notices an
    overlay rewritten by ANOTHER process, within the same call it would
    otherwise have served stale data from."""

    def test_overlay_edit_from_another_process_is_picked_up(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        overlay_path = _write_overlay(tmp_path, {"extraction": {"facts": {"enabled": False}}})

        from unittest.mock import patch

        from app.instance_config import load_instance_config

        with patch("config.loader.load_instance_config", return_value={}):
            cfg1 = load_instance_config()
        assert cfg1["extraction"]["facts"]["enabled"] is False

        # Simulate the OTHER container's save: rewrite the file on disk.
        # Deliberately no ``reset_cache()`` call here — this process must
        # not need one.
        overlay_path.write_text(yaml.dump({"extraction": {"facts": {"enabled": True}}}))
        _bump_mtime(overlay_path)

        with patch("config.loader.load_instance_config", return_value={}):
            cfg2 = load_instance_config()
        assert cfg2["extraction"]["facts"]["enabled"] is True, (
            "a process that did not perform the save must still observe the "
            "new overlay value once it changes on disk, without a restart"
        )

    def test_unchanged_overlay_returns_the_cached_object(self, tmp_path, monkeypatch):
        """The staleness check must not defeat the cache on every call — only
        an actual on-disk change should trigger a reload."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        _write_overlay(tmp_path, {"instance": {"name": "Stable"}})

        from unittest.mock import patch

        from app.instance_config import load_instance_config

        with patch("config.loader.load_instance_config", return_value={}):
            cfg1 = load_instance_config()
            cfg2 = load_instance_config()
        assert cfg1 is cfg2, (
            "load_instance_config() re-read/rebuilt the config even though the overlay file never changed on disk"
        )

    def test_derived_bq_access_cache_clears_on_auto_detected_overlay_change(
        self,
        tmp_path,
        monkeypatch,
    ):
        """The same class of bug one level down: a config value hot-reloads
        but a derived ``@functools.cache`` reader (``get_bq_access``) keeps
        serving what it resolved from the stale value unless something
        clears it too — ``reset_cache()``'s existing, explicit
        ``get_bq_access.cache_clear()`` call is the precedent. The
        auto-reload path (an out-of-process overlay change, no explicit
        ``reset_cache()`` call from THIS process) must invalidate the same
        derived caches, or a process trades a stale ``instance_config`` for
        a stale ``get_bq_access`` — spied directly rather than inferred from
        BQ project-resolution behavior, which is also (separately) value-
        keyed and would mask a regression here.
        """
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        overlay_path = _write_overlay(tmp_path, {"instance": {"name": "A"}})

        from unittest.mock import MagicMock, patch

        from app.instance_config import load_instance_config

        with patch("config.loader.load_instance_config", return_value={}):
            load_instance_config()  # first good load — establishes the fingerprint

        overlay_path.write_text(yaml.dump({"instance": {"name": "B"}}))
        _bump_mtime(overlay_path)

        fake_get_bq_access = MagicMock()
        with (
            patch("connectors.bigquery.access.get_bq_access", fake_get_bq_access),
            patch("config.loader.load_instance_config", return_value={}),
        ):
            load_instance_config()  # the auto-reload trigger, no explicit reset_cache()

        fake_get_bq_access.cache_clear.assert_called_once()


class TestCorruptOverlayAfterGoodLoad:
    """A corrupt overlay written AFTER a good load (a save from another
    process observed mid-write, or a manual edit gone wrong) must not take
    the process down, and must not silently drop the operator's settings
    down to the built-in/static defaults — it must keep serving the last
    known-good merged config."""

    def test_corrupt_overlay_preserves_last_good_config_and_does_not_raise(
        self,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        overlay_path = _write_overlay(tmp_path, {"instance": {"name": "GoodName"}})

        from unittest.mock import patch

        from app.instance_config import load_instance_config

        with patch("config.loader.load_instance_config", return_value={}):
            good_cfg = load_instance_config()
        assert good_cfg["instance"]["name"] == "GoodName"

        # Corrupt it — e.g. observed mid partial-write — with a distinct
        # mtime so the staleness check actually fires a reload attempt.
        overlay_path.write_text("instance: [unclosed\n")
        _bump_mtime(overlay_path)

        with patch("config.loader.load_instance_config", return_value={}):
            cfg_after_corruption = load_instance_config()  # must not raise

        assert cfg_after_corruption["instance"]["name"] == "GoodName", (
            "a corrupt overlay after a good load must keep serving the last "
            "good merged config, not silently revert to the static base"
        )

    def test_last_good_config_itself_is_not_overwritten_by_the_degraded_read(
        self,
        tmp_path,
        monkeypatch,
    ):
        """The degraded reload must not poison ``_last_good_config`` — the
        thing a NEXT corruption falls back to — with the static-only
        config the failed parse produced."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        overlay_path = _write_overlay(tmp_path, {"instance": {"name": "GoodName"}})

        from unittest.mock import patch

        import app.instance_config as ic

        with patch("config.loader.load_instance_config", return_value={}):
            ic.load_instance_config()
            assert ic._last_good_config["instance"]["name"] == "GoodName"

            overlay_path.write_text("instance: [unclosed\n")
            _bump_mtime(overlay_path)
            ic.load_instance_config()

            assert ic._last_good_config["instance"]["name"] == "GoodName", (
                "_last_good_config must still hold the last GOOD merge after "
                "a corrupt reload, not the degraded static-only config"
            )
