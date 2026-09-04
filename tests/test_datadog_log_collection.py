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
import shutil
import subprocess
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


def _resolver_locals() -> str:
    """The three resolver locals, verbatim from main.tf."""
    start = MAIN_TF.index("  container_logs_destination = (")
    end = MAIN_TF.index("\n\n", MAIN_TF.index("datadog_logs_active", start))
    return MAIN_TF[start:end]


def test_the_resolver_expression_is_pinned_exactly():
    """Pinned as a whole, not probed for substrings.

    Substring assertions cannot see ORDER, and order is the entire semantics
    of a ternary chain: a resolver with the `enable_gcp_logging` branch moved
    in front of the `enable_datadog` one contains every fragment a membership
    check would look for and resolves the headline case backwards. Pinning the
    normalised expression means any reordering has to be a deliberate edit to
    this test as well.
    """
    normalised = re.sub(r"[ \t]+", " ", _resolver_locals()).strip()
    assert normalised == (
        "container_logs_destination = (\n"
        ' contains(["cloud_logging", "datadog", "none"], var.container_logs_destination)\n'
        " ? var.container_logs_destination\n"
        ' : var.enable_datadog ? "datadog" : var.enable_gcp_logging ? "cloud_logging" : "none"\n'
        " )\n"
        ' cloud_logging_logs_active = local.container_logs_destination == "cloud_logging"\n'
        ' datadog_logs_active = local.container_logs_destination == "datadog"'
    )


_TERRAFORM = shutil.which("terraform") or shutil.which("tofu")

# destination, enable_datadog, enable_gcp_logging -> resolved
_TRUTH_TABLE = [
    # The auto branch. "auto" must behave exactly like "" — it is only an
    # explicit spelling, and it is deliberately absent from the contains()
    # list, which is subtle enough to be worth proving rather than reading.
    *[
        (dest, dd, gl, expected)
        for dest in ("", "auto")
        for dd, gl, expected in [
            (True, True, "datadog"),
            (True, False, "datadog"),
            (False, True, "cloud_logging"),
            (False, False, "none"),
        ]
    ],
    # An explicit value wins over the derivation in every combination.
    *[
        (dest, dd, gl, dest)
        for dest in ("cloud_logging", "datadog", "none")
        for dd in (True, False)
        for gl in (True, False)
    ],
]


@pytest.fixture(scope="module")
def evaluate_resolver(tmp_path_factory):
    """Evaluate the REAL locals with the REAL Terraform.

    Every other assertion in this file about the resolver reads `.tf` source
    text, which cannot distinguish a correct expression from a wrong one that
    happens to contain the same fragments. This lifts the locals verbatim into
    a standalone provider-less module — so `init` is instant and offline — and
    asks Terraform itself what they evaluate to.
    """
    if _TERRAFORM is None:
        pytest.skip("neither terraform nor tofu is on PATH")
    d = tmp_path_factory.mktemp("resolver")
    (d / "main.tf").write_text(
        'variable "container_logs_destination" { type = string }\n'
        'variable "enable_datadog" { type = bool }\n'
        'variable "enable_gcp_logging" { type = bool }\n\n'
        "locals {\n" + _resolver_locals() + "\n}\n"
    )
    subprocess.run(
        [_TERRAFORM, "init", "-input=false", "-no-color"],
        cwd=d,
        check=True,
        capture_output=True,
        text=True,
    )

    def _evaluate(destination: str, enable_datadog: bool, enable_gcp_logging: bool) -> tuple[str, bool, bool]:
        # One expression, not three lines: with a piped (non-TTY) stdin
        # `terraform console` evaluates only the first thing it is given.
        proc = subprocess.run(
            [
                _TERRAFORM,
                "console",
                "-no-color",
                f"-var=container_logs_destination={destination}",
                f"-var=enable_datadog={str(enable_datadog).lower()}",
                f"-var=enable_gcp_logging={str(enable_gcp_logging).lower()}",
            ],
            cwd=d,
            input=("[local.container_logs_destination, local.cloud_logging_logs_active, local.datadog_logs_active]\n"),
            check=True,
            capture_output=True,
            text=True,
        )
        values = [
            line.strip().rstrip(",").strip('"')
            for line in proc.stdout.splitlines()
            if line.strip() not in ("", "[", "]")
        ]
        assert len(values) == 3, f"unexpected console output: {proc.stdout!r}"
        return values[0], values[1] == "true", values[2] == "true"

    return _evaluate


@pytest.mark.parametrize(
    "destination, enable_datadog, enable_gcp_logging, expected",
    _TRUTH_TABLE,
    ids=lambda v: str(v),
)
def test_the_resolver_truth_table(
    evaluate_resolver, destination: str, enable_datadog: bool, enable_gcp_logging: bool, expected: str
):
    resolved, cloud_logging_active, datadog_active = evaluate_resolver(destination, enable_datadog, enable_gcp_logging)
    assert resolved == expected
    # Exactly one destination is ever active — the invariant the whole design
    # rests on, since Docker allows one log driver per container.
    assert cloud_logging_active == (expected == "cloud_logging")
    assert datadog_active == (expected == "datadog")
    assert not (cloud_logging_active and datadog_active)


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
        "the pipeline has been fluentd + Ops Agent since c99803936; the "
        "gcplogs wording described the original overlay (#679) and a driver "
        "this module no longer uses"
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


# Shapes a single per-rule fixture does not reach. Each entry found a real gap
# in review, so they are pinned separately rather than folded into LEAKS.
EXTRA_LEAKS = [
    pytest.param(
        "mask_authorization_values",
        "upstream 401 authorization=Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ",
        "QWxhZGRpbjpvcGVuIHNlc2FtZQ",
        id="basic-auth",
    ),
    pytest.param(
        "mask_authorization_values",
        "jira 403 Authorization: Token abcdEFGH1234ijklMNOP",
        "abcdEFGH1234ijklMNOP",
        id="token-scheme",
    ),
    pytest.param(
        "mask_authorization_values",
        'X-StorageApi-Token: "abcdEFGH1234ijklMNOP"',
        "abcdEFGH1234ijklMNOP",
        id="bare-value-no-scheme",
    ),
    pytest.param(
        "mask_url_credentials",
        "redis connect failed: redis://:hunter2swordfish@cache:6379/0",
        "hunter2swordfish",
        id="empty-username-dsn",
    ),
    pytest.param(
        "mask_authorization_values",
        "upstream 401 headers={'Authorization': 'Bearer abcdEFGH1234ijklMNOP'}",
        "abcdEFGH1234ijklMNOP",
        id="python-dict-repr-single-quotes",
    ),
    pytest.param(
        "mask_known_key_shapes",
        "openai call failed key=sk-proj-AbCdEfGh1234IjKlMnOp5678QrSt",
        "sk-proj-AbCdEfGh1234IjKlMnOp5678QrSt",
        id="project-scoped-openai-key",
    ),
    pytest.param(
        "mask_known_key_shapes",
        "anthropic call failed key=sk-ant-api03-AAAABBBBCCCCDDDDEEEE",
        "sk-ant-api03-AAAABBBBCCCCDDDDEEEE",
        id="anthropic-key-after-branch-collapse",
    ),
]


@pytest.mark.parametrize("rule_name, line, secret", EXTRA_LEAKS)
def test_the_rules_cover_the_credential_shapes_this_stack_actually_emits(rule_name: str, line: str, secret: str):
    """`Authorization: Basic <base64>` is the one that mattered: this stack
    speaks Basic in the Jira connector, the MCP client, the marketplace git
    router's PAT header and the PAT resolver. With `bearer` as the only
    recognised scheme the whole header passed through unmasked, because once
    that branch failed the value class had to match from `Basic` and the space
    after it is not in the class.

    The single-quote case is the same class of near-miss: Python's dict repr
    writes {'Authorization': '...'}, so a rule that only accepted double quotes
    missed every header logged through a repr — which is most of them. And a
    key class of `[A-Za-z0-9]` stops at the second hyphen of a project-scoped
    `sk-proj-` key, leaving it in the clear."""
    rule = _processing_rules()[rule_name]
    masked = re.sub(rule["pattern"], rule["replace_placeholder"].replace("$1", r"\1"), line)
    assert secret not in masked, f"{rule_name} left {secret!r} in the clear"


@pytest.mark.parametrize(
    "claims",
    [
        pytest.param({}, id="empty-payload"),
        pytest.param({"a": 1}, id="one-claim"),
        pytest.param({"sub": "1234567890", "name": "analyst", "iat": 1516239022}, id="typical"),
    ],
)
def test_the_jwt_rule_masks_small_payloads_too(claims: dict):
    """A JWT's middle segment is only as long as its claims: `{}` base64s to
    `e30`, three characters. A bound tuned to a typical payload would let
    exactly the smallest tokens through — the failure mode a spot-check with
    one realistic token never surfaces."""
    import base64
    import json

    def seg(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode().rstrip("=")

    token = f"{seg({'alg': 'HS256', 'typ': 'JWT'})}.{seg(claims)}.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    rule = _processing_rules()["mask_jwt"]
    masked = re.sub(
        rule["pattern"], rule["replace_placeholder"].replace("$1", r"\1"), f"auth rejected token {token} from peer"
    )
    assert token not in masked, f"unmasked JWT with {len(seg(claims))}-char payload"
    assert "auth rejected token" in masked


def test_no_compose_file_sets_an_autodiscovery_log_label():
    """An integration-level `com.datadoghq.ad.logs` config COMPLETELY overrides
    the global processing_rules for that container — i.e. it would un-redact
    it. If one is ever needed, the masking rules have to move onto it."""
    offenders = [p.name for p in sorted(REPO.glob("docker-compose*.yml")) if "com.datadoghq.ad.logs" in p.read_text()]
    assert not offenders, (
        f"{offenders} set com.datadoghq.ad.logs, which drops the global redaction rules for those containers"
    )
