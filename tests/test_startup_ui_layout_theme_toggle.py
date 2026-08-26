"""Static contract for the chrome toggles' Terraform → startup-script plumbing.

`ui_layout` / `theme` decide which chrome the web UI renders. Until they
existed on the module, the only way to set them on a provisioned VM was
`instance.ui_layout` / `instance.theme` in `/data/state/instance.yaml` — which
survives a VM recreate (it lives on the data disk) but is invisible to
Terraform, so the deployment's own config could not state what chrome it runs.

D1 (config-ownership remediation, 2026-08): `theme` no longer writes an
always-wins `AGNES_INSTANCE_THEME` `.env` line — that permanently shadowed
the admin UI's own control of the same knob on every boot. It now rides the
first-boot-only `instance.yaml` seed instead (see
`test_startup_ui_config_ownership.py` for the full seed-vs-env contract), so
a fresh VM still gets the day-1 palette and `/admin/server-config` owns it
from day 2 onward. `ui_layout` stays exactly as before — it was already
retired to "declared but never plumbed", unaffected by this change.

Same lightweight read-the-template pattern as `test_startup_studio_toggle.py`,
with one difference that matters: these are **per-VM** fields, not module-wide,
because the redesign is rolled out dev-first.
"""

import re
from pathlib import Path

MODULE = Path("infra/modules/customer-instance")


def _object_type_blocks(body: str) -> list[str]:
    """Return the prod_instance + dev_instances object-type declarations."""
    blocks = []
    for var in ("prod_instance", "dev_instances"):
        m = re.search(rf'variable\s+"{var}"\s*\{{', body)
        assert m, f"variables.tf must declare {var}"
        depth, i = 1, m.end()
        while i < len(body) and depth:
            if body[i] == "{":
                depth += 1
            elif body[i] == "}":
                depth -= 1
            i += 1
        blocks.append(body[m.start() : i])
    return blocks


def test_both_object_types_declare_the_chrome_fields():
    """Terraform silently DROPS attributes absent from the object type, so a
    field declared on only one of the two would reach the module for prod and
    vanish for dev (or vice versa) with no error anywhere.

    `ui_layout` is in this list even though it is inert (the rail chrome is
    unconditional since Wave 0, 2026-08) — precisely BECAUSE of that silent
    drop. Undeclaring it would let a customer root that still pins
    `ui_layout = "topnav"` apply cleanly and get the rail with nothing saying
    why; declared, the validation below turns it into a plan-time error.
    """
    body = (MODULE / "variables.tf").read_text()
    for block in _object_type_blocks(body):
        for field in ("ui_layout", "theme"):
            assert re.search(rf'{field}\s*=\s*optional\(string,\s*""\)', block), (
                f'{field} must be declared as optional(string, "") on both prod_instance and dev_instances'
            )


def test_main_tf_folds_theme_into_the_first_boot_seed_per_vm():
    body = (MODULE / "main.tf").read_text()
    assert re.search(r'theme\s*=\s*try\(inst\.theme,\s*""\)', body), (
        "main.tf must fold theme per-VM (inst.theme) into the "
        "instance_brand_scalars first-boot seed map, not forward it as a "
        "raw templatefile() var"
    )


def test_ui_layout_is_declared_but_not_plumbed():
    """The other half of the retirement: accepted at the module boundary so it
    can be REJECTED with a message, and carried no further.

    A forwarded-but-unused template var is the failure this pins — it would
    read as "the layout still reaches the VM" to the next person to touch the
    module, and invite the env line back.
    """
    assert not re.search(r"ui_layout\s*=\s*each\.value\.ui_layout", (MODULE / "main.tf").read_text()), (
        "ui_layout must not be forwarded into the startup-script template — the chrome is unconditional"
    )
    tpl = (MODULE / "startup-script.sh.tpl").read_text()
    assert "AGNES_UI_LAYOUT" not in tpl, (
        "startup-script.sh.tpl must not write AGNES_UI_LAYOUT — get_ui_layout() ignores it and warns"
    )
    assert "${ui_layout}" not in tpl, "startup-script.sh.tpl must not reference the retired ui_layout template var"


def test_tpl_never_writes_the_theme_env_line():
    body = (MODULE / "startup-script.sh.tpl").read_text()
    assert "AGNES_INSTANCE_THEME" not in body, (
        "startup-script.sh.tpl must not write AGNES_INSTANCE_THEME — the "
        "palette reaches the VM only through the first-boot instance.yaml seed"
    )


# ---------------------------------------------------------------------------
# The Terraform allow-lists must track the app's own resolvers
# ---------------------------------------------------------------------------


def _tf_allowed(field: str) -> set[str]:
    """The value set `variables.tf` accepts for `field`, read from the
    `contains([...], ...)` conditions rather than restated here."""
    import re
    from pathlib import Path

    tf = (Path(__file__).resolve().parents[1] / "infra/modules/customer-instance/variables.tf").read_text()
    found: set[str] = set()
    for m in re.finditer(r"contains\(\[([^\]]*)\],\s*[^)]*\.%s\)" % re.escape(field), tf):
        found |= {v.strip().strip('"') for v in m.group(1).split(",") if v.strip()}
    assert found, f"no contains() validation found for {field} — the guard below would pass vacuously"
    return found


def _app_allowed(func_src: str) -> set[str]:
    """The value set the app's resolver accepts, from its own `not in (...)`."""
    import re

    m = re.search(r"if value not in \(([^)]*)\):", func_src)
    assert m, "resolver shape changed — this guard reads its `value not in (...)` line"
    return {v.strip().strip('"').strip("'") for v in m.group(1).split(",") if v.strip()}


def _resolver_src(name: str) -> str:
    import inspect

    import app.instance_config as ic

    return inspect.getsource(getattr(ic, name))


def test_tf_ui_layout_allowlist_matches_the_app(monkeypatch):
    """The retired half of the drift guard, re-aimed at what the app now does.

    `get_ui_layout()` no longer has a value set to compare against — it hard-
    wires the rail. So the contract Terraform must track is: the ONLY non-empty
    value the module accepts is the one chrome the app can render. A root
    pinning "topnav" has to fail the plan, because applying it would hand back
    the rail with nothing anywhere saying why (the app only warns, on the first
    render, into the server log nobody is reading during an apply).
    """
    from app.instance_config import get_ui_layout

    assert _tf_allowed("ui_layout") - {""} == {"rail"}

    # The app half of the same statement: whatever is configured, rail comes out.
    for configured in ("topnav", "rail", "nonsense"):
        monkeypatch.setenv("AGNES_UI_LAYOUT", configured)
        assert get_ui_layout() == "rail", f"{configured!r} must resolve to the rail chrome"
    monkeypatch.delenv("AGNES_UI_LAYOUT", raising=False)
    assert get_ui_layout() == "rail"


def test_tf_theme_allowlist_matches_the_app():
    assert _tf_allowed("theme") - {""} == _app_allowed(_resolver_src("get_instance_theme"))
