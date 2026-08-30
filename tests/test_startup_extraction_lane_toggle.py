"""Static contract for the extraction lane's Terraform → startup plumbing.

``extraction_worker_enabled`` engages, per VM, the two halves that only work
together: a ``redis`` compose service (the multi-process coordination backend
the startup guard requires the moment ``AGNES_ROLE=worker`` exists) and the
``extraction-worker`` service re-pinned to the operator's producer-bundled
image. Default OFF — a module bump alone must never move the existing fleet.

Same read-the-template pattern as ``test_startup_chat_provider_toggle.py``.
The invariants pinned here are each a real failure mode found while doing the
deployment by hand first (TCRD-259):

- The coordination declaration must ride ``.env`` (env > yaml in
  app/coordination/factory.py), NEVER the applier-owned
  ``/data/state/instance.yaml`` — a second server-side writer of that file is
  the config-erasure class TCRD-226 hit.
- The overlay must not override the ``app``/``scheduler`` services:
  agnes-state-applier rebuilds its COMPOSE_FILE from the managed resolver
  list (no reconcile with .env), so its ``up -d --no-deps --force-recreate
  app scheduler`` would silently strip anything an unmanaged overlay added
  to those two services.
- The overlay must stay OUT of the resolver's AGNES_MANAGED_OVERLAYS:
  reconcile keeps unmanaged candidate entries, which is exactly how the
  overlay survives every auto-upgrade tick.
- The kai-agent overlay must stay the COMPOSE_FILE suffix (both boot-time
  strict-pull strips are suffix-based), so the extraction append has to land
  before kai's.
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


def _extraction_overlay_heredoc(body: str) -> str:
    """The docker-compose.extraction.yml content the startup script writes."""
    m = re.search(r"<<'EXTRYAML'\n(.*?)\nEXTRYAML\n", body, re.DOTALL)
    assert m, "startup-script.sh.tpl must write the extraction overlay via an EXTRYAML heredoc"
    return m.group(1)


def test_both_object_types_declare_the_fields_default_off():
    body = (MODULE / "variables.tf").read_text()
    for block in _object_type_blocks(body):
        # Default OFF is the fleet-safety pin: bumping the module tag on an
        # instance repo must render a byte-identical startup script for every
        # VM that did not opt in.
        assert re.search(r"extraction_worker_enabled\s*=\s*optional\(bool,\s*false\)", block)
        assert re.search(r'extraction_worker_mem_limit\s*=\s*optional\(string,\s*"4g"\)', block)
        assert re.search(r'extraction_worker_cpus\s*=\s*optional\(string,\s*"2.0"\)', block)
    # The image is module-level (like kai_agent_image), not per-VM.
    assert re.search(r'variable\s+"extraction_worker_image"\s*\{', body)


def test_main_tf_forwards_and_validates():
    body = (MODULE / "main.tf").read_text()
    assert re.search(r"extraction_worker_enabled\s*=\s*each\.value\.extraction_worker_enabled", body)
    assert re.search(r"extraction_worker_image\s*=\s*var\.extraction_worker_image", body)
    assert re.search(r"extraction_worker_mem_limit\s*=\s*each\.value\.extraction_worker_mem_limit", body)
    assert re.search(r"extraction_worker_cpus\s*=\s*each\.value\.extraction_worker_cpus", body)
    # Plan-time catch: enabled without an image would render an empty
    # `image:` and fail the whole boot at `docker compose up`.
    assert re.search(
        r"!each\.value\.extraction_worker_enabled\s*\|\|\s*var\.extraction_worker_image\s*!=\s*\"\"",
        body,
    )


def test_tpl_gates_everything_on_the_flag():
    body = (MODULE / "startup-script.sh.tpl").read_text()
    guard = "%{ if extraction_worker_enabled ~}"
    assert body.count(guard) == 4, (
        "expected exactly four guarded blocks: overlay write + COMPOSE_FILE "
        "append, .env lines, strict-boot strip, tolerant bring-up"
    )
    for needle in (
        'COMPOSE_FILE_VALUE="$COMPOSE_FILE_VALUE:docker-compose.extraction.yml"',
        "AGNES_COORDINATION_BACKEND=redis",
        "AGNES_REDIS_URL=redis://redis:6379/0",
        "AGNES_EXTRACTION_WORKER_IMAGE=${extraction_worker_image}",
        "AGNES_EXTRACTION_WORKER_MEM_LIMIT=${extraction_worker_mem_limit}",
        "AGNES_EXTRACTION_WORKER_CPUS=${extraction_worker_cpus}",
    ):
        idx = body.index(needle)
        # The needle must sit inside SOME extraction_worker_enabled block:
        # the nearest guard above it must be ours, unclosed at that point.
        opening = body.rindex(guard, 0, idx)
        closing = body.index("%{ endif ~}", opening)
        assert opening < idx < closing, f"{needle!r} must be gated on extraction_worker_enabled"


def test_coordination_rides_env_not_instance_yaml():
    """env > yaml (app/coordination/factory.py) is what lets the module
    declare redis without touching the applier-owned instance.yaml — the
    startup script must never grow a coordination write into $INSTANCE_YAML."""
    body = (MODULE / "startup-script.sh.tpl").read_text()
    for m in re.finditer(r"coordination:", body):
        window = body[max(0, m.start() - 400) : m.end() + 200]
        assert "INSTANCE_YAML" not in window, (
            "coordination.backend must not be written into instance.yaml — "
            "declare it via AGNES_COORDINATION_BACKEND in .env instead"
        )


def test_overlay_shape():
    body = (MODULE / "startup-script.sh.tpl").read_text()
    overlay = _extraction_overlay_heredoc(body)
    # The always-on switch: overlay presence in COMPOSE_FILE clears the base
    # compose's profile gate — no --profile plumbing through startup/upgrade/
    # applier scripts (which disagree about profile handling).
    assert "profiles: !reset []" in overlay
    assert re.search(r"^  redis:$", overlay, re.MULTILINE)
    assert re.search(r"^  extraction-worker:$", overlay, re.MULTILINE)
    # Coordination data is reconstructible; nothing may land on a disk.
    assert '"--save", "", "--appendonly", "no"' in overlay
    # Redis stays compose-network-internal.
    assert "ports:" not in overlay
    # The applier-strip invariant: agnes-state-applier force-recreates
    # app+scheduler from the MANAGED overlay list only, so anything this
    # overlay contributed to those services would be silently dropped there.
    assert not re.search(r"^  app:$", overlay, re.MULTILINE), (
        "the extraction overlay must never override the app service — the "
        "state-applier's force-recreate would strip the override"
    )
    assert not re.search(r"^  scheduler:$", overlay, re.MULTILINE), (
        "the extraction overlay must never override the scheduler service — "
        "the state-applier's force-recreate would strip the override"
    )


def test_overlay_stays_unmanaged_in_the_resolver():
    """agnes_compose_file_reconcile keeps candidate entries it does not
    manage — that is the entire survival mechanism for this overlay across
    auto-upgrade ticks. Adding it to AGNES_MANAGED_OVERLAYS would make the
    resolver drop it on every tick instead."""
    resolver = Path("scripts/ops/agnes-compose-file.sh").read_text()
    m = re.search(r'AGNES_MANAGED_OVERLAYS="([^"]*)"', resolver)
    assert m, "resolver must declare AGNES_MANAGED_OVERLAYS"
    assert "docker-compose.extraction.yml" not in m.group(1)


def test_kai_overlay_stays_the_compose_file_suffix():
    """Both boot-time strict-pull strips are `${VAR%:overlay.yml}` suffix
    removals: kai's runs first and must find its overlay last in the list,
    then the extraction strip must find ITS overlay last. That only holds if
    the extraction append sits between the dispatcher's and kai's."""
    body = (MODULE / "startup-script.sh.tpl").read_text()
    extraction_append = body.index(':docker-compose.extraction.yml"')
    kai_append = body.index(':docker-compose.kai-agent.yml"')
    assert extraction_append < kai_append, (
        "the extraction overlay must be appended to COMPOSE_FILE_VALUE before "
        "the kai-agent overlay (kai's strict-boot strip is suffix-based)"
    )
    kai_strip = body.index("%:docker-compose.kai-agent.yml}")
    extraction_strip = body.index("%:docker-compose.extraction.yml}")
    assert kai_strip < extraction_strip, (
        "the strict-boot strips must run kai first, extraction second — the "
        "reverse order leaves the kai overlay in the middle of the list where "
        "its suffix strip cannot remove it"
    )


def test_watchdog_covers_the_lane():
    body = (MODULE / "files/agnes-watchdog.sh").read_text()
    m = re.search(r"ROLE_CONTAINER_RE='([^']*)'", body)
    assert m, "watchdog must declare ROLE_CONTAINER_RE"
    assert re.match(m.group(1), "extraction-worker"), (
        "the extraction-worker service runs the app image with "
        "AGNES_ROLE=worker and must be scanned as a role container"
    )
    # Redis coordination declared via the module's .env form must arm the
    # CoordinationUnavailable signature exactly like the yaml form.
    assert "AGNES_COORDINATION_BACKEND=redis" in body
