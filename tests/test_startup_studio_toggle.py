"""Static contract for the Studio toggle's Terraform → startup-script plumbing.

Pins the infra contract so a rename or dropped template argument can't
silently break the deployment toggle (same lightweight read-the-template
pattern as ``test_startup_vault_key.py``):

* ``variables.tf`` declares ``studio_enabled`` as a bool defaulting to true;
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


def test_variables_tf_declares_bool_default_true():
    body = (MODULE / "variables.tf").read_text()
    m = re.search(r'variable\s+"studio_enabled"\s*\{(.*?)\}', body, re.DOTALL)
    assert m, "variables.tf must declare studio_enabled"
    block = m.group(1)
    assert re.search(r"type\s*=\s*bool", block)
    assert re.search(r"default\s*=\s*true", block)


def test_main_tf_folds_it_into_the_first_boot_seed_map():
    body = (MODULE / "main.tf").read_text()
    assert re.search(
        r"instance_studio_map\s*=\s*var\.studio_enabled\s*\?\s*\{\}\s*:\s*\{\s*enabled\s*=\s*false\s*\}",
        body,
    ), "main.tf must fold studio_enabled into instance_studio_map (empty when true, {enabled: false} when false)"


def test_tpl_never_writes_the_env_var():
    body = (MODULE / "startup-script.sh.tpl").read_text()
    assert "AGNES_STUDIO_ENABLED" not in body, (
        "startup-script.sh.tpl must not write AGNES_STUDIO_ENABLED — the toggle "
        "reaches the VM only through the first-boot instance.yaml seed"
    )
