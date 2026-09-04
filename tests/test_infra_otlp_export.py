"""customer-instance module: per-VM opt-in OTLP export + the deployment label.

The module renders four `.env` lines from the per-instance `otlp_endpoint`,
`otlp_headers_secret`, `otlp_capture_content` and `deployment_env` fields
(`docs/observability.md` → "OpenTelemetry export"). The headers value is a
credential: it must be fetched at boot like `runtime_secret_env` values —
never interpolated into the metadata script — and written with the same
escape set the plain-map path uses, so a token containing `$`, quotes or
spaces survives both consumers of `.env` (the auto-upgrade script's bash
`source` and docker compose's dotenv parser).

The fetch block is mini-rendered and executed with `gcloud` stubbed, the
same way `tests/test_infra_runtime_secret_env_hardening.py` drives the
plain-map path.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "infra" / "modules" / "customer-instance"
TPL = MODULE / "startup-script.sh.tpl"
VARIABLES_TF = MODULE / "variables.tf"
MAIN_TF = MODULE / "main.tf"

SECRET_NAME = "agnes-x-otlp-headers"

HEREDOC_LINES = (
    "AGNES_DEPLOYMENT_ENV=${deployment_env}\n"
    '%{ if otlp_endpoint != "" ~}\n'
    "OTEL_EXPORTER_OTLP_ENDPOINT=${otlp_endpoint}\n"
    '%{ if otlp_headers_secret != "" ~}\n'
    'OTEL_EXPORTER_OTLP_HEADERS="$OTLP_HEADERS_QUOTED"\n'
    "%{ endif ~}\n"
    "AGNES_OTEL_CAPTURE_CONTENT=${otlp_capture_content}\n"
    "%{ endif ~}"
)


def _extract_fetch_block() -> str:
    body = TPL.read_text()
    m = re.search(r"^# --- otlp-headers begin.*?$\n(.*?)^# --- otlp-headers end ---$", body, re.S | re.M)
    assert m, "otlp-headers markers missing from the startup script"
    return m.group(1)


def _render(block: str, *, endpoint: str = "https://collector.example/base") -> str:
    lines = [line for line in block.splitlines() if not line.lstrip().startswith("%{")]
    out = "\n".join(lines)
    out = out.replace("${otlp_headers_secret}", SECRET_NAME)
    out = out.replace("${otlp_endpoint}", endpoint)
    out = out.replace("${deployment_env}", "agnes-x")
    out = out.replace("${otlp_capture_content}", "1")
    out = out.replace("$${", "${")
    assert "%{" not in out and "${otlp" not in out and "${deployment" not in out
    return out


def _boot_script(tmp_path: Path, value: str) -> Path:
    secret_file = tmp_path / "secret_value"
    secret_file.write_text(value)
    script = tmp_path / "boot.sh"
    script.write_text(
        "set -euo pipefail\n"
        f'gcloud() {{ cat "{secret_file}"; }}\n'
        f"{_render(_extract_fetch_block())}\n"
        f'cat > "{tmp_path}/.env" <<ENVEOF\n'
        f"{_render(HEREDOC_LINES)}\n"
        "ENVEOF\n"
    )
    return script


def _source_env(tmp_path: Path, name: str) -> str:
    reader = tmp_path / "read.sh"
    reader.write_text(
        "set -euo pipefail\n"
        f'cd "{tmp_path}"\n'
        "set -a; . ./.env; set +a\n"
        f'printf \'%s\' "${{{name}}}" > "{tmp_path}/roundtrip"\n'
    )
    proc = subprocess.run(["bash", str(reader)], capture_output=True, text=True)
    assert proc.returncode == 0, f"sourcing .env failed:\n{proc.stderr}\n{(tmp_path / '.env').read_text()}"
    return (tmp_path / "roundtrip").read_text()


def test_heredoc_writes_the_four_lines_exactly():
    body = TPL.read_text()
    assert HEREDOC_LINES in body, "the .env heredoc must carry the OTLP block verbatim (guards included)"
    # The label line is unconditional — every VM gets the same field the logs
    # and the spans key on; the OTLP lines only when an endpoint is set.
    head, _, _ = body.partition('%{ if otlp_endpoint != "" ~}')
    assert "AGNES_DEPLOYMENT_ENV=${deployment_env}" in head


def test_headers_value_is_fetched_at_boot_never_interpolated():
    block = _extract_fetch_block()
    assert "gcloud secrets versions access latest --secret=${otlp_headers_secret}" in block
    assert "OTLP_HEADERS_QUOTED=$(printf '%s' \"$OTLP_HEADERS\" | sed -e 's/[\\\\\"$`]/\\\\&/g'" in block
    # The template must not carry the value itself anywhere.
    main = MAIN_TF.read_text()
    assert re.search(r"otlp_headers_secret\s+= each\.value\.otlp_headers_secret", main)
    assert "otlp_headers_value" not in main and "OTEL_EXPORTER_OTLP_HEADERS" not in main


def test_hostile_token_survives_both_env_consumers(tmp_path: Path):
    value = 'Authorization=Bearer%20abc$def "quoted" `tick` \\ back'
    proc = subprocess.run(["bash", str(_boot_script(tmp_path, value))], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    env = (tmp_path / ".env").read_text()
    assert "AGNES_DEPLOYMENT_ENV=agnes-x\n" in env
    assert "OTEL_EXPORTER_OTLP_ENDPOINT=https://collector.example/base\n" in env
    assert "AGNES_OTEL_CAPTURE_CONTENT=1\n" in env
    assert _source_env(tmp_path, "OTEL_EXPORTER_OTLP_HEADERS") == value
    # The written line carries exactly the escape set bash sourcing and docker
    # compose's dotenv parser unescape identically (`\` `"` `$` and backtick)
    # — the same contract tests/test_infra_runtime_secret_env_hardening.py
    # pins for the plain map, and the only reason a token with `$` survives.
    expected = 'OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer%20abc\\$def \\"quoted\\" \\`tick\\` \\\\ back"\n'
    assert expected in env, env


def test_multiline_token_is_refused_not_written_raw(tmp_path: Path):
    proc = subprocess.run(["bash", str(_boot_script(tmp_path, "line1\nline2"))], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "multiline" in proc.stderr
    env = (tmp_path / ".env").read_text()
    assert 'OTEL_EXPORTER_OTLP_HEADERS=""' in env
    assert "line2" not in env


def test_fields_declared_on_both_instance_types_and_validated():
    tf = VARIABLES_TF.read_text()
    for field in ("otlp_endpoint", "otlp_headers_secret", "otlp_capture_content", "deployment_env"):
        # Terraform silently drops attributes absent from an object type, so
        # a field declared on prod_instance only would never reach a dev VM.
        assert tf.count(f"{field} ") >= 2 or tf.count(f"{field}=") >= 2, field
    assert 'otlp_headers_secret == "" || ' in tf, "a headers secret without an endpoint must fail the plan"


def test_module_grants_the_headers_secret_per_instance():
    main = MAIN_TF.read_text()
    assert 'resource "google_secret_manager_secret_iam_member" "vm_otlp"' in main
    assert "for_each  = local.otlp_secrets" in main
    assert "inst.otlp_headers_secret" in main
