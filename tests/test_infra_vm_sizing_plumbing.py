"""TCRD-296: `variables.tf` defaults + the Postgres side-car compose overlay
consume the VM-derived sizing (`tests/test_startup_vm_sizing.py` covers the
startup-script arithmetic itself).

``docker compose config`` is not reliably available in CI, so the overlay's
structure is validated with PyYAML instead of a real compose merge (see
``tests/test_extraction_overlay_compose_merge.py`` for the docker-available
functional variant used elsewhere in this suite).
"""

from pathlib import Path

import yaml

MODULE = Path("infra/modules/customer-instance")
VARIABLES_TF = MODULE / "variables.tf"
OVERLAY = Path("docker-compose.postgres-host-mount.yml")


class _ComposeTagTolerantLoader(yaml.SafeLoader):
    """SafeLoader that tolerates the Compose-spec merge tags (``!override``,
    ``!reset``) PyYAML has no builtin constructor for — same idiom as
    ``tests/test_compose_overlays_parse.py`` (that file's own duplicate-key
    guard already covers every ``docker-compose*.yml``, this one only needs
    the tags not to blow up ``yaml.safe_load``)."""


def _passthrough_tag(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node):
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)


_ComposeTagTolerantLoader.add_multi_constructor("!", _passthrough_tag)


def _load(path: Path) -> dict:
    return yaml.load(path.read_text(), Loader=_ComposeTagTolerantLoader)


def _block(text: str, variable: str) -> str:
    marker = f'variable "{variable}"'
    assert marker in text, f"{marker} not found in variables.tf"
    return text.split(marker, 1)[1].split("\nvariable ", 1)[0]


def test_prod_instance_mem_limits_default_to_auto():
    tf = VARIABLES_TF.read_text()
    block = _block(tf, "prod_instance")
    assert 'app_mem_limit       = optional(string, "auto")' in block
    assert 'scheduler_mem_limit = optional(string, "auto")' in block
    assert 'extraction_worker_mem_limit = optional(string, "auto")' in block


def test_dev_instances_mem_limits_default_to_auto():
    tf = VARIABLES_TF.read_text()
    block = _block(tf, "dev_instances")
    assert 'app_mem_limit       = optional(string, "auto")' in block
    assert 'scheduler_mem_limit = optional(string, "auto")' in block
    assert 'extraction_worker_mem_limit = optional(string, "auto")' in block


def test_no_stale_fixed_default_survives_on_either_object_type():
    """A leftover `"4g"`/`"2g"` default on one of the two object types would
    silently un-auto that instance type while the docs above claim otherwise."""
    tf = VARIABLES_TF.read_text()
    for block_name in ("prod_instance", "dev_instances"):
        block = _block(tf, block_name)
        assert 'app_mem_limit       = optional(string, "4g")' not in block
        assert 'scheduler_mem_limit = optional(string, "2g")' not in block
        assert 'extraction_worker_mem_limit = optional(string, "4g")' not in block


def test_overlay_is_valid_yaml_with_the_expected_shape():
    doc = _load(OVERLAY)
    postgres = doc["services"]["postgres"]
    assert postgres["shm_size"] == "${AGNES_PG_SHM_SIZE:-1g}"
    assert postgres["command"][0] == "postgres"


def test_overlay_command_sets_every_derived_and_fixed_knob():
    doc = _load(OVERLAY)
    command = doc["services"]["postgres"]["command"]
    # command is ["postgres", "-c", "flag=value", "-c", "flag=value", ...]
    flags = dict(item.split("=", 1) for item in command[2::2])
    assert flags["shared_buffers"] == "${AGNES_PG_SHARED_BUFFERS:-256MB}"
    assert flags["effective_cache_size"] == "${AGNES_PG_EFFECTIVE_CACHE_SIZE:-1GB}"
    assert flags["work_mem"] == "${AGNES_PG_WORK_MEM:-16MB}"
    assert flags["maintenance_work_mem"] == "${AGNES_PG_MAINTENANCE_WORK_MEM:-256MB}"
    assert flags["max_parallel_workers_per_gather"] == "${AGNES_PG_MAX_PARALLEL_WORKERS_PER_GATHER:-2}"
    # Fixed knobs — literals, not env-var indirected, because they never vary
    # with VM size.
    assert flags["max_wal_size"] == "8GB"
    assert flags["wal_compression"] == "on"
    assert flags["random_page_cost"] == "1.1"
    assert flags["jit"] == "off"


def test_overlay_command_is_well_formed_dash_c_pairs():
    doc = _load(OVERLAY)
    command = doc["services"]["postgres"]["command"]
    assert command[0] == "postgres"
    body = command[1:]
    assert len(body) % 2 == 0, "every -c must be followed by exactly one flag=value"
    assert all(flag == "-c" for flag in body[0::2])
    assert all("=" in value for value in body[1::2])


def test_host_bind_mounts_are_unchanged():
    """The pre-existing volumes override (the whole reason this bridge file
    exists) must survive alongside the new sizing keys."""
    doc = _load(OVERLAY)
    services = doc["services"]
    assert services["data-migrate"]["volumes"] == ["/data:/data:ro"]
    assert services["postgres"]["volumes"] == ["/data/postgres:/var/lib/postgresql/data"]


def test_startup_script_writes_the_same_env_var_names_the_overlay_reads():
    """The overlay's `${AGNES_PG_*}` references must match names the startup
    script actually writes into `.env` — a rename on one side alone would
    silently fall through to the overlay's own hardcoded `:-` default on
    every VM."""
    tpl = (MODULE / "startup-script.sh.tpl").read_text()
    doc = _load(OVERLAY)
    postgres = doc["services"]["postgres"]
    referenced = {postgres["shm_size"].split(":-")[0].strip("${}")}
    for item in postgres["command"][2::2]:
        _, value = item.split("=", 1)
        if value.startswith("${"):
            referenced.add(value.split(":-")[0].strip("${}"))
    for name in referenced:
        assert f"{name}=$" in tpl, f"startup script never writes {name} into .env"
