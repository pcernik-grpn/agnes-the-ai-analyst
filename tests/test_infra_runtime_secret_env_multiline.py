"""`runtime_secret_env_multiline` — Secret Manager → /opt/agnes/.env for
MULTILINE secret values (the SharePoint combined cert+key PEM being the
motivating case).

The existing `runtime_secret_env` writes `<ENV>=<fetched>` as one .env line,
which a multiline value breaks three ways at once: the .env format itself,
`agnes-auto-upgrade.sh`'s `set -a; . .env` bash source, and docker compose's
env_file parsing (multiline support varies by compose version). The multiline
variant therefore base64-encodes the fetched value into a single line, and
the app-side consumer (`connectors/sharepoint/settings._env_secret`) decodes
it — PEM material always contains `-----BEGIN`, base64 text never does (no
hyphen in its alphabet), so the decode cannot misfire on a raw value.

Same text-pinning idiom as tests/test_infra_secrets_template.py.
"""

from pathlib import Path

MODULE = Path("infra/modules/customer-instance")
TPL = MODULE / "startup-script.sh.tpl"
MAIN_TF = MODULE / "main.tf"
VARIABLES_TF = MODULE / "variables.tf"


def test_variable_is_declared_with_empty_default():
    variables = VARIABLES_TF.read_text()
    assert 'variable "runtime_secret_env_multiline"' in variables
    block = variables.split('variable "runtime_secret_env_multiline"', 1)[1].split("\nvariable ", 1)[0]
    assert "map(string)" in block
    assert "default     = {}" in block or "default = {}" in block
    # The description must carry the operational contract: base64 transport +
    # the app-side decode.
    assert "base64" in block


def test_startup_script_fetches_base64_encoded_single_line():
    tpl = TPL.read_text()
    assert "%{ for secret_name, env_name in runtime_secret_env_multiline ~}" in tpl
    fetch = tpl.split("%{ for secret_name, env_name in runtime_secret_env_multiline ~}", 1)[1].split("%{ endfor ~}", 1)[
        0
    ]
    assert "gcloud secrets versions access latest --secret=${secret_name}" in fetch
    assert "base64" in fetch


def test_env_heredoc_writes_the_multiline_map_next_to_the_plain_one():
    tpl = TPL.read_text()
    # Both maps write `<ENV>=$<ENV>` lines into the same heredoc.
    heredoc_loop = (
        "%{ for secret_name, env_name in runtime_secret_env_multiline ~}\n${env_name}=$${${env_name}}\n%{ endfor ~}"
    )
    assert heredoc_loop in tpl


def test_terraform_grants_accessor_and_forwards_the_template_var():
    main = MAIN_TF.read_text()
    assert 'resource "google_secret_manager_secret_iam_member" "vm_runtime_env_multiline"' in main
    grant = main.split('"vm_runtime_env_multiline"', 1)[1].split("\nresource ", 1)[0]
    # A secret mapped in BOTH runtime_secret_env and the multiline variant
    # must not produce the same IAM binding twice (apply errors with
    # "already exists" — the documented duplicate-binding trap).
    assert "runtime_secret_env" in grant and "contains(" in grant
    import re

    assert re.search(r"runtime_secret_env_multiline\s+=\s+var\.runtime_secret_env_multiline", main)


def test_kai_agent_dedup_also_subtracts_the_multiline_map():
    """The kai-agent secret grants subtract already-granted secrets so an
    apply never declares the same binding twice — the new map joins that
    subtraction."""
    main = MAIN_TF.read_text()
    kai_block = main.split("kai_agent_secrets =", 1)[1].split("\n\n", 1)[0]
    assert "runtime_secret_env_multiline" in kai_block
