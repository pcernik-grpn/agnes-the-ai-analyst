"""Generic extension points for deployment-specific add-ons.

A deployment can mount additional FastAPI admin routers and extra Jinja template
directories without forking the app, by listing them in ``instance.yaml``::

    plugins:
      admin_routers:
        - "my_overlay.api.catalog_import:router"   # module[:attr], attr defaults to 'router'
      template_dirs:
        - "/opt/overlay/templates"

This module only RESOLVES the configured specs/paths; ``app.main`` includes the
returned routers and ``app.web.router`` adds the dirs to the Jinja loader. The
mechanism is vendor-agnostic — what gets mounted is the operator's private config,
never shipped here.
"""
import importlib
from pathlib import Path
from typing import List


def load_routers(specs: List[str]) -> list:
    """Resolve ``["module.path:attr", ...]`` to router objects.

    ``attr`` defaults to ``router`` when omitted. Raises ImportError/AttributeError
    on a bad spec — a misconfigured plugin should fail loudly at startup, not be
    silently skipped.
    """
    routers = []
    for spec in specs:
        module_path, _, attr = spec.partition(":")
        module = importlib.import_module(module_path)
        routers.append(getattr(module, attr or "router"))
    return routers


def extra_template_dirs(dirs: List[str]) -> List[Path]:
    """Return the existing directories from the configured list (missing ones dropped)."""
    return [Path(d) for d in dirs if Path(d).is_dir()]
