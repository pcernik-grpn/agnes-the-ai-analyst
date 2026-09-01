"""Static contract for the extraction lane's Terraform → startup plumbing.

``extraction_worker_enabled`` engages, per VM, the two halves that only work
together: a ``redis`` compose service (the multi-process coordination backend
the startup guard requires the moment ``AGNES_ROLE=worker`` exists) and the
``extraction-worker`` service, which by DEFAULT follows the app's own image
(``extraction_worker_image`` unset) and can be deliberately pinned to a
different tag when set (a canary, holding the worker back mid-rollout).
Default OFF — a module bump alone must never move the existing fleet.

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
    # extraction_worker_image is module-level (like kai_agent_image), still
    # forwarded and still able to pin the worker away from the app image —
    # see test_main_tf_forwards_and_validates below for the (now optional,
    # not required) plumbing.
    assert re.search(r'variable\s+"extraction_worker_image"\s*\{', body)
    # Both extraction_worker_image and extraction_producer_command are
    # DEPRECATED (external-producer mode removed, 2026-09-01): they must
    # stay DECLARED so existing tfvars keep planning, must say so, and must
    # default to the inert empty string — a revived meaningful default
    # would silently resurrect dead config.
    m = re.search(r'variable\s+"extraction_producer_command"\s*\{', body)
    assert m, "variables.tf must keep the deprecated extraction_producer_command declared (tfvars compat)"
    block_end = body.index("\n}\n", m.end())
    block = body[m.end() : block_end]
    assert "deprecated" in block.lower() and "ignored" in block.lower()
    assert re.search(r'default\s*=\s*""', block), "the deprecated variable must default to the inert empty string"


def test_main_tf_forwards_and_validates():
    body = (MODULE / "main.tf").read_text()
    assert re.search(r"extraction_worker_enabled\s*=\s*each\.value\.extraction_worker_enabled", body)
    assert re.search(r"extraction_worker_mem_limit\s*=\s*each\.value\.extraction_worker_mem_limit", body)
    assert re.search(r"extraction_worker_cpus\s*=\s*each\.value\.extraction_worker_cpus", body)
    # extraction_worker_image IS still forwarded — it is an optional,
    # deliberate override (a canary, holding the worker back), not dead
    # config like extraction_producer_command.
    assert re.search(r"extraction_worker_image\s*=\s*var\.extraction_worker_image", body)
    assert not re.search(r"extraction_producer_command\s*=\s*var\.extraction_producer_command", body)
    assert "extraction_producer_command" not in body
    # There is nothing to validate at plan time any more: empty is a valid,
    # DEFAULT value (the overlay renders no `image:` key at all), so the old
    # "requires extraction_worker_image on the module" precondition — which
    # made the override mandatory rather than optional — is gone.
    assert not re.search(
        r"!each\.value\.extraction_worker_enabled\s*\|\|\s*var\.extraction_worker_image\s*!=\s*\"\"",
        body,
    )
    assert "requires extraction_worker_image on the module" not in body


def _matching_endif(body: str, if_pos: int) -> int:
    """The `%{ endif ~}` that closes the `%{ if ... ~}` starting at
    `if_pos`, correctly skipping past any NESTED if/endif pairs in between
    (extraction_worker_image's own conditionals now nest inside this one)."""
    depth = 0
    for m in re.finditer(r"%\{\s*if\b.*?~\}|%\{\s*endif\s*~\}", body[if_pos:]):
        if m.group(0).lstrip("%{ ").startswith("if"):
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return if_pos + m.start()
    raise AssertionError(f"no matching %{{ endif ~}} found for the if at {if_pos}")


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
        "AGNES_EXTRACTION_WORKER_MEM_LIMIT=${extraction_worker_mem_limit}",
        "AGNES_EXTRACTION_WORKER_CPUS=${extraction_worker_cpus}",
        # TCRD-259 follow-up: the app-side gate (the sharepoint switch) must
        # also ride .env, or the TF flag alone never activates the
        # corpus-extraction job kind — it would still need the per-VM
        # instance.yaml SSH edit this env plumbing exists to avoid.
        "AGNES_SHAREPOINT_ENABLED=1",
    ):
        idx = body.index(needle)
        # The needle must sit inside SOME extraction_worker_enabled block:
        # the nearest guard above it must be ours, unclosed at that point.
        # extraction_worker_image nests its OWN if/endif pairs inside this
        # block now, so the true closing endif has to be nesting-aware.
        opening = body.rindex(guard, 0, idx)
        closing = _matching_endif(body, opening)
        assert opening < idx < closing, f"{needle!r} must be gated on extraction_worker_enabled"


def test_tpl_writes_no_producer_env_line():
    """The external-producer mode is gone: AGNES_EXTRACTION_PRODUCER_COMMAND
    must never be written into the VM's .env — it would be dead config that
    misleads the next operator into thinking a producer exists to point at.
    (extraction_worker_image is a different story — see
    test_overlay_and_env_image_line_are_conditional below: it is a live,
    optional override, not a producer-era leftover.)"""
    body = (MODULE / "startup-script.sh.tpl").read_text()
    assert "AGNES_EXTRACTION_PRODUCER_COMMAND" not in body
    assert "extraction_producer_command" not in body


def test_overlay_and_env_image_line_are_conditional():
    """The target shape (course-corrected from an earlier, stricter draft
    of this fix): extraction_worker_image is a legitimate, OPTIONAL override
    — an operator may deliberately want the worker on a different tag than
    the app (a canary, holding the worker back mid-rollout) — but it must
    default to OFF. Empty (the default) -> no `image:` key at all, so the
    service inherits docker-compose.prod.yml's own AGNES_IMAGE_REPO/
    AGNES_TAG pin, which is what actually stops the worker drifting behind
    the database's migrations. Set -> the overlay pins the worker to that
    ref, exactly as before this fix."""
    body = (MODULE / "startup-script.sh.tpl").read_text()
    guard = '%{ if extraction_worker_image != "" ~}'
    endif = "%{ endif ~}"
    assert body.count(guard) == 3, (
        "expected exactly three guarded blocks: registry auth, the overlay's "
        "image: line, and the .env line"
    )

    # 1. The overlay's image: line, inside the extraction-worker service.
    overlay = _extraction_overlay_heredoc(body)
    worker = _extraction_worker_block(overlay)
    assert guard in worker, "the extraction-worker image: line must be gated on extraction_worker_image != \"\""
    opening = worker.index(guard)
    closing = worker.index(endif, opening)
    image_line = "image: $${AGNES_EXTRACTION_WORKER_IMAGE}"
    assert image_line in worker, "the extraction-worker service must be ABLE to carry a pinned image"
    assert opening < worker.index(image_line) < closing, "the image: line must sit inside its own guard"
    # No stray, unconditional `image:` line elsewhere in the service.
    unconditional = worker[: opening] + worker[closing + len(endif) :]
    yaml_lines = [ln for ln in unconditional.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    assert not any(re.match(r"^\s*image:", ln) for ln in yaml_lines), (
        "outside its own guard, the extraction-worker service must carry no "
        "unconditional image override"
    )

    # 2. The .env line, inside the outer extraction_worker_enabled block.
    env_guard_idx = body.index(guard, body.index("AGNES_EXTRACTION_WORKER_CPUS=${extraction_worker_cpus}"))
    env_closing = body.index(endif, env_guard_idx)
    env_line = "AGNES_EXTRACTION_WORKER_IMAGE=${extraction_worker_image}"
    assert env_guard_idx < body.index(env_line, env_guard_idx) < env_closing

    # 3. The registry-auth block (best-effort gcloud configure-docker),
    # gated the same way — it only matters when a pin actually exists.
    assert 'EXTRACTION_IMAGE="${extraction_worker_image}"' in body
    auth_guard_idx = body.index(guard, 0, env_guard_idx)
    auth_closing = body.index(endif, auth_guard_idx)
    assert auth_guard_idx < body.index('EXTRACTION_IMAGE="${extraction_worker_image}"') < auth_closing


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


def _extraction_worker_block(overlay: str) -> str:
    """Isolate just the ``extraction-worker:`` service mapping from the
    overlay YAML, so an assertion about its shape can't accidentally match
    the sibling ``redis:`` service (which legitimately sets ``image:``)."""
    m = re.search(r"^  extraction-worker:\n(.*?)(?=^  \S|\Z)", overlay, re.MULTILINE | re.DOTALL)
    assert m, "extraction-worker service block not found in the overlay"
    return m.group(1)


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

    worker = _extraction_worker_block(overlay)
    # The image line's presence/absence is a separate, dedicated test below
    # (test_overlay_and_env_image_line_are_conditional) — it is legitimately
    # conditional now, not simply absent.
    assert "profiles: !reset []" in worker
    assert re.search(r"depends_on:\s*\n\s*redis:\s*\n\s*condition:\s*service_healthy", worker), (
        "extraction-worker must additively depend_on redis: service_healthy"
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


def test_tpl_renders_no_dead_producer_env():
    """The external-producer mode is gone: rendering
    AGNES_EXTRACTION_PRODUCER_COMMAND into .env would be dead config that
    misleads the next operator into thinking a producer exists to point at."""
    body = (MODULE / "startup-script.sh.tpl").read_text()
    assert "AGNES_EXTRACTION_PRODUCER_COMMAND" not in body
