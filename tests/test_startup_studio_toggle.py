"""Static contract for the Studio toggle's Terraform → startup-script plumbing.

Pins the infra contract so a rename or dropped template argument can't
silently break the deployment toggle (same lightweight read-the-template
pattern as ``test_startup_vault_key.py``):

* ``variables.tf`` declares ``studio_enabled`` as a bool defaulting to false;
* ``main.tf`` folds it into the ``instance_studio_map`` local (D1, 2026-08:
  moved off an always-wins ``.env`` line into the first-boot-only
  ``instance.yaml`` seed — see ``test_startup_ui_config_ownership.py`` for
  the full seed-vs-env contract);
* ``startup-script.sh.tpl`` never writes ``AGNES_STUDIO_ENABLED`` into the
  app ``.env`` at all — the toggle reaches the VM only through the seed.
"""

import re
from pathlib import Path

MODULE = Path("infra/modules/customer-instance")


def test_variables_tf_default_matches_the_apps_own_default():
    """False since the admin cleanup retired the Studio surface.

    The rule this pins is not the polarity but the AGREEMENT: the variable's
    default has to be whatever `get_studio_enabled()` returns with nothing
    configured, because `main.tf` seeds `instance.yaml` only when the operator
    asks for something OTHER than the default. Disagree, and the seed either
    writes a redundant block on every fresh VM or — the way this broke — omits
    the block for an operator who asked for `true` and hands them a disabled
    Studio.
    """
    body = (MODULE / "variables.tf").read_text()
    m = re.search(r'variable\s+"studio_enabled"\s*\{(.*?)\n\}', body, re.DOTALL)
    assert m, "variables.tf must declare studio_enabled"
    block = m.group(1)
    assert re.search(r"type\s*=\s*bool", block)
    assert re.search(r"default\s*=\s*false", block), (
        "studio_enabled must default to false, matching app.instance_config.get_studio_enabled()'s own default"
    )


def test_the_terraform_default_and_the_app_default_agree():
    """The agreement above, asserted against the app rather than restated.

    A regex over `variables.tf` cannot notice that the app moved; this can, and
    it is the assertion that would have caught the inversion when the app's
    default flipped.
    """
    from app.switches import get_switch

    body = (MODULE / "variables.tf").read_text()
    m = re.search(r'variable\s+"studio_enabled"\s*\{(.*?)\n\}', body, re.DOTALL)
    assert m
    tf_default = re.search(r"default\s*=\s*(true|false)", m.group(1)).group(1) == "true"
    assert tf_default is get_switch("studio").default, (
        "infra/modules/customer-instance/variables.tf's studio_enabled default "
        "disagrees with the `studio` switch's default — the first-boot seed only "
        "writes a block when the operator asks for something other than the app "
        "default, so a disagreement silently inverts the toggle"
    )


def test_main_tf_seeds_only_when_the_operator_departs_from_the_default():
    """`{enabled = true}` when asked for true, nothing otherwise.

    This condition inverted with the app default: it used to read
    `var.studio_enabled ? {} : {enabled = false}` back when the app defaulted
    ON, and leaving it that way would have seeded nothing for an operator
    asking for `true` — the exact silent inversion the test above now guards.
    """
    body = (MODULE / "main.tf").read_text()
    assert re.search(
        r"instance_studio_map\s*=\s*var\.studio_enabled\s*\?\s*\{\s*enabled\s*=\s*true\s*\}\s*:\s*\{\}",
        body,
    ), "main.tf must fold studio_enabled into instance_studio_map ({enabled: true} when true, empty when false)"


def test_tpl_never_writes_the_env_var():
    body = (MODULE / "startup-script.sh.tpl").read_text()
    assert "AGNES_STUDIO_ENABLED" not in body, (
        "startup-script.sh.tpl must not write AGNES_STUDIO_ENABLED — the toggle "
        "reaches the VM only through the first-boot instance.yaml seed"
    )
