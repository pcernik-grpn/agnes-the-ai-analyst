r"""Plain `runtime_secret_env` — hardening of the Secret Manager → /opt/agnes/.env
path for SINGLE-LINE secret values.

`runtime_secret_env_multiline` (tests/test_infra_runtime_secret_env_multiline.py)
carries multiline values as base64. This file covers the plain map's two
residual hazards:

- A multiline value mapped here by mistake used to be written raw into the
  .env heredoc, corrupting every following line — and the startup script's
  own ``set -a; . .env`` (run right before ``docker compose up``) then
  executed the value's lines as commands (a PEM turns into
  ``-----BEGIN: command not found`` at best). The fetch loop now REFUSES
  such a value: blanked to "" plus a boot-log warning pointing at
  `runtime_secret_env_multiline`.
- Values used to be written unquoted, so a value containing a space aborted
  that same boot-time source (the CADDY_TLS trap documented in the template),
  and ``$``/backticks were expanded or executed. Values are now written
  double-quoted with ``\\`` ``"`` ``$`` and backtick backslash-escaped — the
  one escape set that BOTH consumers of the line unescape identically: bash
  (``\X`` inside double quotes → ``X`` for exactly these four) and docker
  compose's dotenv parser (``\"``/``\\``/backtick via its unescape pass,
  ``\$`` via its escaped-dollar rule in variable expansion).

The functional tests do not re-implement the logic: they extract the real
block between the template's ``runtime-secret-env-plain`` markers, mini-render
the Terraform loop for a one-entry map (drop ``%{ for }``/``%{ endfor }``
directive lines, substitute the two loop variables, unescape ``$${`` → ``${``),
stub ``gcloud``, and run it under the script's own ``set -euo pipefail`` —
the same extract-don't-copy idiom as tests/test_caddyfile_apps_subdomain_docker.py.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MODULE = REPO / "infra/modules/customer-instance"
TPL = MODULE / "startup-script.sh.tpl"
VARIABLES_TF = MODULE / "variables.tf"

SECRET_NAME = "my-secret"
ENV_NAME = "MY_SECRET"

# The exact heredoc loop the template must use to write plain-map values —
# double-quoted, from the pre-escaped *_QUOTED variable the fetch block set.
HEREDOC_LOOP = (
    '%{ for secret_name, env_name in runtime_secret_env ~}\n${env_name}="$${${env_name}_QUOTED}"\n%{ endfor ~}'
)


def _extract_fetch_block() -> str:
    body = TPL.read_text()
    m = re.search(
        r"^# --- runtime-secret-env-plain begin.*?$\n(.*?)^# --- runtime-secret-env-plain end ---$",
        body,
        re.S | re.M,
    )
    assert m, "runtime-secret-env-plain markers missing from the startup script"
    return m.group(1)


def _render(block: str) -> str:
    """Mini-templatefile for a one-entry runtime_secret_env map."""
    lines = [line for line in block.splitlines() if not line.lstrip().startswith("%{")]
    out = "\n".join(lines)
    out = out.replace("${secret_name}", SECRET_NAME).replace("${env_name}", ENV_NAME)
    out = out.replace("$${", "${")
    # Anything still Terraform-shaped would mean the test executes something
    # the real boot does not.
    assert "%{" not in out
    assert "${secret_name}" not in out and "${env_name}" not in out
    return out


def _boot_script(tmp_path: Path, value: str) -> Path:
    """The fetch block + the .env heredoc write, with gcloud stubbed."""
    secret_file = tmp_path / "secret_value"
    secret_file.write_text(value)
    script = tmp_path / "boot.sh"
    script.write_text(
        "set -euo pipefail\n"
        f'gcloud() {{ cat "{secret_file}"; }}\n'
        f"{_render(_extract_fetch_block())}\n"
        f'cat > "{tmp_path}/.env" <<ENVEOF\n'
        f"{_render(HEREDOC_LOOP)}\n"
        "ENVEOF\n"
    )
    return script


def _source_env(tmp_path: Path) -> str:
    """Read the value back the way the startup script's boot-time source does."""
    reader = tmp_path / "read.sh"
    reader.write_text(
        "set -euo pipefail\n"
        f'cd "{tmp_path}"\n'
        "set -a; . ./.env; set +a\n"
        f'printf \'%s\' "${{{ENV_NAME}}}" > "{tmp_path}/roundtrip"\n'
    )
    proc = subprocess.run(["bash", str(reader)], capture_output=True, text=True)
    assert proc.returncode == 0, f"sourcing .env failed:\n{proc.stderr}\n{(tmp_path / '.env').read_text()}"
    return (tmp_path / "roundtrip").read_text()


def test_fetch_block_guards_multiline_and_pre_escapes():
    block = _extract_fetch_block()
    # Refusal: the warning must name the right fix, and the value must be
    # blanked — never written raw.
    assert "WARNING" in block
    assert "runtime_secret_env_multiline" in block
    assert '${env_name}=""' in block
    # Pre-escape for the double-quoted .env line: exactly \ " $ ` .
    assert "sed -e 's/[\\\\\"$`]/\\\\&/g'" in block


def test_env_heredoc_writes_plain_values_double_quoted():
    assert HEREDOC_LOOP in TPL.read_text()
    # The multiline map's loop stays raw/unquoted (its value is base64 —
    # already .env-safe; see test_infra_runtime_secret_env_multiline.py).
    assert (
        "%{ for secret_name, env_name in runtime_secret_env_multiline ~}\n${env_name}=$${${env_name}}\n%{ endfor ~}"
    ) in TPL.read_text()


def test_variable_description_documents_the_refusal():
    variables = VARIABLES_TF.read_text()
    block = variables.split('variable "runtime_secret_env" {', 1)[1].split("\nvariable ", 1)[0]
    assert "refus" in block  # refused/refuses — the guard, not silent corruption
    assert "runtime_secret_env_multiline" in block


def test_multiline_value_is_refused_with_warning(tmp_path):
    pem = "-----BEGIN PRIVATE KEY-----\nMIIfake\n-----END PRIVATE KEY-----"
    proc = subprocess.run(["bash", str(_boot_script(tmp_path, pem))], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "runtime_secret_env_multiline" in proc.stdout + proc.stderr
    content = (tmp_path / ".env").read_text()
    assert "BEGIN PRIVATE" not in content
    assert f'{ENV_NAME}=""' in content
    assert _source_env(tmp_path) == ""


def test_special_char_value_round_trips_and_never_executes(tmp_path):
    nasty = 'spaces "double" $HOME `touch PWNED` back\\slash tail'
    proc = subprocess.run(["bash", str(_boot_script(tmp_path, nasty))], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    # The boot-time source must reproduce the exact raw value — spaces kept,
    # $HOME unexpanded, backtick command NOT executed, backslash intact.
    assert _source_env(tmp_path) == nasty
    assert not (tmp_path / "PWNED").exists()


def test_plain_single_line_token_stays_intact(tmp_path):
    token = "sk-ant-api03-abc_DEF-123"
    subprocess.run(["bash", str(_boot_script(tmp_path, token))], check=True, capture_output=True, text=True)
    assert _source_env(tmp_path) == token
