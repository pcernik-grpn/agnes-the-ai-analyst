"""Static contract for placing `docker-compose.gcp-logging.yml` on a VM.

Before this fix, the overlay's own header claimed "the customer infra
Terraform startup script" places the file, but nothing in
`infra/modules/customer-instance/` actually did — the resolver
(`scripts/ops/agnes-compose-file.sh::agnes_resolve_compose_file`) only
appends it when the file is physically present on disk, and it never was.

The real mechanism, pinned here:

* `Dockerfile` bakes the overlay into `/opt/agnes-host/` alongside every
  other host artifact (same contract as `docker-compose.tls.yml` etc.).
* `startup-script.sh.tpl`'s existing recursive
  `docker cp .../opt/agnes-host/. $APP_DIR/` extracts it onto every VM
  unconditionally.
* A new per-module Terraform variable, `enable_gcp_logging` (default
  **true**), gates whether the script then REMOVES the extracted file —
  removal is what turns the resolver's file-presence check off for VMs that
  opt out.
"""

import re
from pathlib import Path

MODULE = Path("infra/modules/customer-instance")
OVERLAY = "docker-compose.gcp-logging.yml"


def test_dockerfile_ships_the_overlay_to_agnes_host():
    body = Path("Dockerfile").read_text()
    start = body.index("RUN mkdir -p /opt/agnes-host")
    end = body.index("\n\n", start)
    block = body[start:end]
    assert OVERLAY in block, (
        f"Dockerfile must bake {OVERLAY} into /opt/agnes-host/ (inside the RUN "
        "block that copies every other host artifact) so the startup script "
        "can extract it via `docker cp`"
    )


def test_variables_tf_declares_enable_gcp_logging_default_true():
    body = (MODULE / "variables.tf").read_text()
    m = re.search(r'variable\s+"enable_gcp_logging"\s*\{([^}]*)\}', body, re.DOTALL)
    assert m, 'variables.tf must declare variable "enable_gcp_logging"'
    block = m.group(1)
    assert re.search(r"type\s*=\s*bool", block)
    assert re.search(r"default\s*=\s*true", block)
    assert "description" in block


def test_main_tf_forwards_enable_gcp_logging_into_templatefile():
    body = (MODULE / "main.tf").read_text()
    assert re.search(r"enable_gcp_logging\s*=\s*var\.enable_gcp_logging", body), (
        "main.tf must forward var.enable_gcp_logging into templatefile(...)"
    )


def test_tpl_gates_overlay_placement_on_the_tf_var():
    body = (MODULE / "startup-script.sh.tpl").read_text()
    assert OVERLAY in body, "startup-script.sh.tpl must reference the overlay filename"
    assert "%{ if !enable_gcp_logging ~}" in body, (
        "the overlay's placement must be gated on the enable_gcp_logging TF var "
        "(the recursive docker cp extracts it unconditionally; disabling the "
        "var must remove it again)"
    )
    guard = body.index("%{ if !enable_gcp_logging ~}")
    endif = body.index("%{ endif ~}", guard)
    gated_block = body[guard:endif]
    assert OVERLAY in gated_block, "the gated block must act on the overlay file"
    assert "rm -f" in gated_block or "rm " in gated_block


def test_tpl_placement_runs_after_the_extraction_that_ships_it():
    """The gate must run AFTER the recursive `docker cp .../opt/agnes-host/.`
    that actually puts the file on disk — gating before it would be a no-op."""
    body = (MODULE / "startup-script.sh.tpl").read_text()
    extract_idx = body.index('docker cp "$EXTRACT_CONTAINER:/opt/agnes-host/." "$APP_DIR/"')
    gate_idx = body.index("%{ if !enable_gcp_logging ~}")
    assert extract_idx < gate_idx


def test_overlay_header_describes_the_real_mechanism():
    text = Path(OVERLAY).read_text()
    assert "Dockerfile" in text
    assert "enable_gcp_logging" in text
    # The old, inaccurate claim ("the customer infra Terraform startup
    # script... runs exclusively on GCE" places the file) must be gone.
    assert "runs exclusively on GCE" not in text
