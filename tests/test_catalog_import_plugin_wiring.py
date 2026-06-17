"""Generic plugin hook is actually wired into app bootstrap (main.py + web/router.py).

Companion to test_plugins.py (which unit-tests the resolver). Here we prove create_app()
includes a configured plugin admin router, and the web template loader picks up configured
plugin template dirs — both driven by the operator's instance.yaml `plugins:` block.
"""
import sys
import types

import pytest
from fastapi import APIRouter


def _register_probe_router(path: str) -> str:
    """Register a synthetic module exposing a router with a unique route; return its spec."""
    mod = types.ModuleType("catalog_import_probe_mod")
    probe = APIRouter()

    @probe.get(path)
    def _probe():  # pragma: no cover - never called, only its presence is asserted
        return {"ok": True}

    mod.router = probe
    sys.modules["catalog_import_probe_mod"] = mod
    return "catalog_import_probe_mod:router"


def _set_config(monkeypatch, cfg: dict):
    monkeypatch.setattr("app.instance_config.load_instance_config", lambda: cfg, raising=False)
    from app.instance_config import reset_cache
    reset_cache()


def test_configured_plugin_admin_router_is_mounted(monkeypatch):
    spec = _register_probe_router("/api/admin/_catalog_import_probe")
    _set_config(monkeypatch, {"plugins": {"admin_routers": [spec]}})
    try:
        from app.main import create_app
        app = create_app()
        paths = {r.path for r in app.routes}
        assert "/api/admin/_catalog_import_probe" in paths
    finally:
        from app.instance_config import reset_cache
        reset_cache()


def test_no_plugins_config_still_builds_app(monkeypatch):
    _set_config(monkeypatch, {})  # no plugins block at all
    try:
        from app.main import create_app
        app = create_app()
        paths = {r.path for r in app.routes}
        assert "/api/health" in paths  # core app intact; default=[] path is a no-op
    finally:
        from app.instance_config import reset_cache
        reset_cache()


def test_template_directories_includes_plugin_dirs(tmp_path, monkeypatch):
    extra = tmp_path / "plugin_templates"
    extra.mkdir()
    _set_config(monkeypatch, {"plugins": {"template_dirs": [str(extra), str(tmp_path / "missing")]}})
    try:
        from app.web.router import _template_directories, TEMPLATES_DIR
        dirs = _template_directories()
        assert dirs[0] == str(TEMPLATES_DIR)        # built-in first
        assert str(extra) in dirs                    # existing plugin dir added
        assert str(tmp_path / "missing") not in dirs  # missing dir dropped
    finally:
        from app.instance_config import reset_cache
        reset_cache()


def test_template_directories_default_is_builtin_only(monkeypatch):
    _set_config(monkeypatch, {})
    try:
        from app.web.router import _template_directories, TEMPLATES_DIR
        assert _template_directories() == [str(TEMPLATES_DIR)]
    finally:
        from app.instance_config import reset_cache
        reset_cache()
