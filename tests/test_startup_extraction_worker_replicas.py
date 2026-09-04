"""``extraction_worker_replicas`` (TCRD-296 gap #76): the customer-instance
module's Terraform → startup-script plumbing for running more than one
``extraction-worker`` compose replica, and the Postgres side-car sizing that
has to grow with it.

Live finding: an operator ran ``docker compose up -d --scale
extraction-worker=6`` by hand on a 64-vCPU/252 GiB VM to keep up with a
facts-extraction backlog. Nothing in Terraform remembered that scale — the
next recreate silently dropped back to one — and six replicas each sizing a
Postgres connection pool from the SAME per-process runtime hint
(``src/db_pg.py::_extraction_worker_pool_size_hint``, designed for ONE
replica) exhausted the side-car's stock 100-connection cap ("FATAL: sorry,
too many clients already").

``tests/test_startup_vm_sizing.py`` covers the pure-bash sizing arithmetic
(``agnes_pg_max_connections`` and friends) and ``tests/test_infra_vm_sizing_
plumbing.py`` covers the compose overlay's ``max_connections`` flag;
``tests/test_extraction_overlay_compose_merge.py`` covers the merged
compose config's pool-env shape. This file pins the Terraform variable
declarations, the ``main.tf`` forwarding, and — the part a static grep on
one call site would miss — that EVERY ``docker compose up`` that could
recreate the extraction-worker service threads the same ``--scale``, so a
routine recreate (boot sequence, agnes-auto-upgrade tick) cannot silently
collapse a multi-replica lane back to one container.
"""

import re
from pathlib import Path

MODULE = Path("infra/modules/customer-instance")
VARIABLES_TF = MODULE / "variables.tf"
MAIN_TF = MODULE / "main.tf"
TPL = MODULE / "startup-script.sh.tpl"
AUTO_UPGRADE = Path("scripts/ops/agnes-auto-upgrade.sh")


def _object_type_blocks(body: str) -> list[str]:
    """Return the prod_instance + dev_instances object-type declarations —
    same helper as test_startup_extraction_lane_toggle.py."""
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


def test_both_object_types_declare_the_field_default_1():
    body = VARIABLES_TF.read_text()
    for block in _object_type_blocks(body):
        assert re.search(r"extraction_worker_replicas\s*=\s*optional\(number,\s*1\)", block), (
            "extraction_worker_replicas must default to 1 — a module bump alone must "
            "never change an existing VM's replica count"
        )


def test_both_object_types_bound_the_field():
    """Terraform-side sanity bound (1..32) — a typo'd huge value fails the
    plan instead of an apply that tries to scale a service to a number that
    will exhaust the host."""
    body = VARIABLES_TF.read_text()
    assert re.search(
        r"var\.prod_instance\.extraction_worker_replicas\s*>=\s*1\s*&&\s*"
        r"var\.prod_instance\.extraction_worker_replicas\s*<=\s*32",
        body,
    )
    assert re.search(
        r"i\.extraction_worker_replicas\s*>=\s*1\s*&&\s*i\.extraction_worker_replicas\s*<=\s*32",
        body,
    )


def test_main_tf_forwards_the_field():
    body = MAIN_TF.read_text()
    assert re.search(r"extraction_worker_replicas\s*=\s*each\.value\.extraction_worker_replicas", body)


def test_tpl_resolves_the_replica_count_as_a_plain_int():
    """No "auto" concept here (unlike the mem-limit fields) — replicas is a
    plain operator-set count, resolved once and reused everywhere below."""
    tpl = TPL.read_text()
    assert 'RESOLVED_EXTRACTION_WORKER_REPLICAS="${extraction_worker_replicas}"' in tpl


def test_tpl_writes_the_env_lines():
    tpl = TPL.read_text()
    assert "AGNES_EXTRACTION_WORKER_REPLICAS=$RESOLVED_EXTRACTION_WORKER_REPLICAS" in tpl
    assert "AGNES_PG_MAX_CONNECTIONS=$AGNES_PG_MAX_CONNECTIONS" in tpl


def test_tolerant_bring_up_carries_the_scale_flag():
    tpl = TPL.read_text()
    assert (
        "docker compose $COMPOSE_PROFILES_ARG up -d --scale "
        '"extraction-worker=$RESOLVED_EXTRACTION_WORKER_REPLICAS" extraction-worker'
    ) in tpl


def test_startup_script_has_exactly_one_unscaled_bare_up():
    """The startup script's boot-sequence bare `up -d` (the strict phase, in
    the retry loop) deliberately runs with BOTH the kai and extraction
    overlays stripped from COMPOSE_FILE (see the `%{ if kai_agent_enabled ~}`
    / `%{ if extraction_worker_enabled ~}` suffix-strips right above it —
    covered by test_startup_extraction_lane_toggle.py::
    test_kai_overlay_stays_the_compose_file_suffix), so extraction-worker is
    not part of that resolved compose config at all and --scale would error
    "no such service" if it carried one. The ONLY site that (re)creates
    extraction-worker itself is the targeted tolerant-phase bring-up,
    asserted separately above — and it must be the SOLE occurrence of a bare
    `up -d` that touches the extraction overlay."""
    tpl = TPL.read_text()
    # A TRULY bare `docker compose $COMPOSE_PROFILES_ARG up -d` — nothing
    # (no service name, no --scale) between it and the next `;`/line-
    # continuation/end of line. Every OTHER `up -d $COMPOSE_PROFILES_ARG`
    # occurrence in this file names a specific service (redis,
    # extraction-worker, kai-agent), so this pattern matches only the
    # strict-phase retry loop.
    matches = re.findall(r"docker compose \$COMPOSE_PROFILES_ARG up -d[ \t]*(?=;|\\|$)", tpl, re.M)
    assert len(matches) == 1, (
        f"expected exactly one unscaled bare `up -d` (the strict-phase retry loop, overlays "
        f"already stripped before it — see test_kai_overlay_stays_the_compose_file_suffix), "
        f"found {len(matches)}"
    )


def test_auto_upgrade_every_bare_up_carries_the_scale():
    """agnes-auto-upgrade.sh's recreate path (drift-gated, runs on the
    recurring 5-min tick) has THREE bare `up -d` call sites (base stack /
    kai-restored / non-kai else-branch) — unlike the startup script's strict
    phase, none of these strip the extraction overlay out of COMPOSE_FILE,
    so all three would otherwise silently collapse a multi-replica
    extraction lane back to one container on the very next tick after a
    routine image or config drift recreate."""
    body = AUTO_UPGRADE.read_text()
    scaled = 'up -d ${SCALE_ARGS[@]+"${SCALE_ARGS[@]}"}'
    assert body.count(scaled) == 3, (
        f"expected 3 bare `up -d` sites carrying --scale threading, found {body.count(scaled)}"
    )
    # No OTHER bare (no trailing service name) `up -d ...` site lacking it.
    unscaled_bare = re.findall(
        r"docker compose \$\{PROFILE_ARGS\[@\]\+\"\$\{PROFILE_ARGS\[@\]\}\"\} up -d(?!\s*\$\{SCALE_ARGS)[ \t]*(?:\\)?\s*$",
        body,
        re.M,
    )
    assert not unscaled_bare, f"bare up -d missing --scale threading: {unscaled_bare!r}"


def test_auto_upgrade_reads_the_replica_count_and_gates_on_the_overlay():
    body = AUTO_UPGRADE.read_text()
    assert 'EXTRACTION_WORKER_REPLICAS="$(_env_get AGNES_EXTRACTION_WORKER_REPLICAS)"' in body
    assert "SCALE_ARGS=()" in body
    assert (
        'if [[ ":$COMPOSE_FILE:" == *":docker-compose.extraction.yml:"* ]]; then\n'
        '    SCALE_ARGS=( --scale "extraction-worker=${EXTRACTION_WORKER_REPLICAS:-1}" )'
    ) in body


def test_targeted_up_calls_are_left_alone():
    """Sites that name a specific, non-extraction-worker service must NOT
    carry --scale — SCALE_ARGS is irrelevant there and adding it would just
    be noise (or, worse, error if the extraction overlay isn't loaded at
    all on that VM)."""
    body = AUTO_UPGRADE.read_text()
    for needle in (
        "up -d --no-deps worker gateway",
        'up -d --no-deps "$svc"',
        "up -d kai-agent",
    ):
        assert needle in body, f"expected the targeted call site {needle!r} to still exist"
        idx = body.index(needle)
        line = body[body.rfind("\n", 0, idx) + 1 : body.index("\n", idx)]
        assert "SCALE_ARGS" not in line, f"targeted call site must not carry --scale: {line!r}"
