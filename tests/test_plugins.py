"""Generic extension points — load admin routers + extra template dirs from config."""
import pytest

from app.plugins import load_routers, extra_template_dirs


def test_load_routers_imports_module_attr():
    routers = load_routers(["app.api.health:router"])
    assert len(routers) == 1
    assert hasattr(routers[0], "routes")  # a FastAPI APIRouter


def test_load_routers_defaults_attr_to_router():
    routers = load_routers(["app.api.health"])  # no ":attr" -> defaults to 'router'
    assert hasattr(routers[0], "routes")


def test_load_routers_empty():
    assert load_routers([]) == []


def test_load_routers_bad_spec_raises():
    with pytest.raises((ImportError, AttributeError)):
        load_routers(["app.api.health:does_not_exist"])


def test_extra_template_dirs_filters_to_existing(tmp_path):
    real = tmp_path / "tpl"
    real.mkdir()
    out = extra_template_dirs([str(real), str(tmp_path / "missing")])
    assert [str(p) for p in out] == [str(real)]  # missing dir dropped
