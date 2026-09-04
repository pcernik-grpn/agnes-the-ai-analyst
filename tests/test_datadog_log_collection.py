"""Which collector gets the container logs, and what is stripped on the way out.

`tests/test_datadog_module_files.py` asserts the SHAPE of the rendered
datadog.yaml. This file covers the two things that shape cannot express: how
`container_logs_destination` resolves against the two permit switches, and
whether the redaction rules actually redact.

The masking tests run each pattern in BOTH directions on purpose. A rule that
fails to match is a leak; a rule that over-matches is silent data loss, which
is the worse of the two because nothing ever reports it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _tf_template import render_template  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
MODULE = REPO / "infra/modules/customer-instance"
VARIABLES_TF = (MODULE / "variables.tf").read_text()
MAIN_TF = (MODULE / "main.tf").read_text()
TPL = (MODULE / "startup-script.sh.tpl").read_text()

DESTINATIONS = ("", "auto", "cloud_logging", "datadog", "none")


def _var_block(name: str) -> str:
    start = VARIABLES_TF.index(f'variable "{name}"')
    nxt = VARIABLES_TF.find('\nvariable "', start + 1)
    return VARIABLES_TF[start:] if nxt == -1 else VARIABLES_TF[start:nxt]


# --------------------------------------------------------------------------
# The destination selector
# --------------------------------------------------------------------------


def test_the_destination_variable_is_declared_and_defaults_to_auto():
    block = _var_block("container_logs_destination")
    assert re.search(r"type\s*=\s*string", block)
    assert re.search(r'default\s*=\s*""', block), (
        "the default must resolve automatically — a hardcoded destination "
        "would make a module bump a behaviour change for VMs that set neither"
    )
    for value in DESTINATIONS:
        assert f'"{value}"' in block, f"the validation must accept {value!r}"


def test_the_resolver_prefers_datadog_when_the_agent_is_installed():
    """Empty resolves to Datadog when it is on, else Cloud Logging when it is,
    else nothing — an operator with the metrics and the monitors in Datadog
    wants the logs beside them."""
    start = MAIN_TF.index("container_logs_destination = (")
    expr = MAIN_TF[start : MAIN_TF.index("cloud_logging_logs_active", start)]
    assert "var.container_logs_destination" in expr
    assert 'var.enable_datadog ? "datadog"' in expr
    assert 'var.enable_gcp_logging ? "cloud_logging" : "none"' in expr
    # An explicit value must win over the derivation, so it has to be tested
    # for membership BEFORE the ternary chain.
    assert expr.index("contains(") < expr.index("var.enable_datadog")


@pytest.mark.parametrize(
    "local_name, destination",
    [("cloud_logging_logs_active", "cloud_logging"), ("datadog_logs_active", "datadog")],
)
def test_each_destination_has_exactly_one_boolean_local(local_name: str, destination: str):
    assert f'{local_name} = local.container_logs_destination == "{destination}"' in re.sub(r"[ \t]+", " ", MAIN_TF)


@pytest.mark.parametrize(
    "destination, permit",
    [("datadog", "var.enable_datadog"), ("cloud_logging", "var.enable_gcp_logging")],
)
def test_a_destination_without_its_collector_fails_at_plan_time(destination: str, permit: str):
    """The failure this catches is silent: the VM boots, the agent reports
    healthy, and nothing ships. Terraform's variable validation cannot see a
    second variable, so the cross-check is a lifecycle precondition."""
    condition = f'local.container_logs_destination != "{destination}" || {permit}'
    assert condition in MAIN_TF, f"main.tf must refuse {destination!r} without {permit}"
    # ...and it must sit in the instance's lifecycle block with the others.
    assert MAIN_TF.index("lifecycle {") < MAIN_TF.index(condition)


# --------------------------------------------------------------------------
# What the destination does to the log driver
# --------------------------------------------------------------------------


def test_any_destination_but_cloud_logging_puts_the_vm_back_on_json_file():
    """Removing the overlay is what makes the Datadog destination work at all:
    the containers fall back to the daemon's default json-file driver, which is
    the one the agent's Docker API tailer is supported against."""
    gate = TPL.index("%{ if !cloud_logging_logs_active ~}")
    block = TPL[gate : TPL.index("%{ endif ~}", gate)]
    assert "rm -f" in block and "docker-compose.gcp-logging.yml" in block


def test_the_ops_agent_is_only_installed_when_it_is_the_destination():
    """No point installing and restarting a collector that receives nothing —
    and on a Datadog VM its unstoppable OTel sub-agent would be pure noise."""
    install = TPL.index("--- OPS AGENT")
    guard = TPL.rindex("%{ if cloud_logging_logs_active ~}", 0, install)
    assert "%{ endif ~}" not in TPL[guard:install]


def test_enable_gcp_logging_is_documented_as_a_permit_not_a_selector():
    block = _var_block("enable_gcp_logging")
    assert "container_logs_destination" in block, (
        "an operator reading the permit switch must be told what actually selects the destination"
    )
    assert "gcplogs" not in block, (
        "the pipeline has been fluentd + Ops Agent since #679; the gcplogs "
        "wording described a driver this module no longer uses"
    )


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------


def _processing_rules() -> dict[str, dict]:
    rendered = render_template(
        (MODULE / "files/datadog/datadog.yaml.tpl").read_text(),
        {"site": "datadoghq.com", "env": "p", "tags": ["app:agnes"], "enable_logs": True},
    )
    doc = yaml.safe_load(rendered.replace("@@DD_API_KEY@@", "x"))
    return {r["name"]: r for r in doc["logs_config"]["processing_rules"]}


# One line that must lose its secret, per rule. The label around the value is
# asserted to SURVIVE where the rule captures it — a mask that eats the
# context makes the log line useless for the incident it was kept for.
LEAKS = {
    "mask_url_credentials": (
        "connect failed: postgresql+psycopg://agnes:hunter2swordfish@postgres:5432/agnes",
        "hunter2swordfish",
        "postgres:5432/agnes",
    ),
    "mask_authorization_values": (
        'upstream 401 {"Authorization": "Bearer abcdEFGH1234ijklMNOP"}',
        "abcdEFGH1234ijklMNOP",
        "Authorization",
    ),
    "mask_known_key_shapes": (
        "anthropic call failed key=sk-ant-api03-AAAABBBBCCCCDDDDEEEE",
        "sk-ant-api03-AAAABBBBCCCCDDDDEEEE",
        "anthropic call failed",
    ),
    "mask_jwt": (
        "session token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NX0.dBjftJeZ4CVPmB92K27u rejected",
        "eyJhbGciOiJIUzI1NiJ9",
        "rejected",
    ),
}

# Ordinary production lines. None of them may be touched by any rule.
BENIGN = (
    "GET /api/data/orders 200 in 14ms",
    "rebuild finished: 12 views over 4 sources",
    "llm_generation provider=anthropic model=claude-opus-5 input_tokens=812",
    "user=analyst@example.com opened session s_42",
    "https://agnes.example.com/api/health returned 200",
    "egress ALLOW api.example.com:443 — host allowlist",
)


def test_every_rule_in_the_template_is_covered_by_a_leak_fixture():
    """A rule added without a fixture would ship unverified."""
    assert set(_processing_rules()) == set(LEAKS)


@pytest.mark.parametrize("name", sorted(LEAKS))
def test_each_rule_is_re2_safe(name: str):
    """The agent compiles these with Go's RE2: no lookarounds, no
    backreferences. Python's `re` accepts them, so an unusable pattern would
    otherwise reach a VM and be dropped there in silence."""
    pattern = _processing_rules()[name]["pattern"]
    for unsupported in ("(?=", "(?!", "(?<=", "(?<!"):
        assert unsupported not in pattern, f"{name}: RE2 has no {unsupported}"
    assert not re.search(r"\\[1-9]", pattern), f"{name}: RE2 has no backreferences"
    re.compile(pattern)


@pytest.mark.parametrize("name", sorted(LEAKS))
def test_each_rule_masks_its_secret_and_keeps_the_context(name: str):
    rule = _processing_rules()[name]
    line, secret, context = LEAKS[name]
    masked = re.sub(rule["pattern"], rule["replace_placeholder"].replace("$1", r"\1"), line)
    assert secret not in masked, f"{name} did not mask {secret!r}"
    assert context in masked, f"{name} ate the context that makes the line useful"


@pytest.mark.parametrize("name", sorted(LEAKS))
@pytest.mark.parametrize("line", BENIGN)
def test_no_rule_touches_an_ordinary_log_line(name: str, line: str):
    rule = _processing_rules()[name]
    masked = re.sub(rule["pattern"], rule["replace_placeholder"].replace("$1", r"\1"), line)
    assert masked == line, f"{name} over-matched: {line!r} -> {masked!r}"


def test_no_compose_file_sets_an_autodiscovery_log_label():
    """An integration-level `com.datadoghq.ad.logs` config COMPLETELY overrides
    the global processing_rules for that container — i.e. it would un-redact
    it. If one is ever needed, the masking rules have to move onto it."""
    offenders = [p.name for p in sorted(REPO.glob("docker-compose*.yml")) if "com.datadoghq.ad.logs" in p.read_text()]
    assert not offenders, (
        f"{offenders} set com.datadoghq.ad.logs, which drops the global redaction rules for those containers"
    )
