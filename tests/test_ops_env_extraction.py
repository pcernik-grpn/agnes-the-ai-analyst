"""Regression guard for the host-side `.env` key extraction.

The three host-side ops scripts used to bash-source the whole
`/opt/agnes/.env` (`set -a; . .env`). A free-text app var in that file
(e.g. an operator-set `AGNES_INSTANCE_CUSTOM_PREAMBLE` containing a
backtick / `>` / `$` / quote) aborted the source under `set -e`, silently
blocking auto-upgrade (deploys stopped) and latent on the cutover
state-applier + daily TLS rotation.

This locks in the fix two ways, for all three scripts:
  1. static — no script may bash-source the whole `.env` again;
  2. behavioural — the *real* committed `_env_get` helper extracts the
     keys each script needs from a hostile `.env`, never shell-evaluating
     a value (quoted/spaced values like TLS_CSR_SUBJECT survive intact).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

_OPS = Path("scripts/ops")

# script -> the infra-controlled keys it legitimately reads from .env
_SCRIPTS: dict[str, set[str]] = {
    "agnes-auto-upgrade.sh": {
        "AGNES_TAG",
        "AGNES_IMAGE_REPO",
        "STATE_DIR",
        "COMPOSE_FILE",
        "SCHEDULER_API_TOKEN",
        "COMPOSE_PROFILES",
    },
    "agnes-state-applier.sh": {"AGNES_TAG", "AGNES_IMAGE_REPO"},
    "agnes-tls-rotate.sh": {
        "TLS_FULLCHAIN_URL",
        "TLS_PRIVKEY_URL",
        "TLS_CSR_SUBJECT",
        "DOMAIN",
        "STATE_DIR",
    },
}

# A .env whose free-text app config is packed with shell metacharacters that
# would break `. .env` (backtick, >, $, &, <, pipe, single quotes).
_HOSTILE_ENV = textwrap.dedent(
    """\
    AGNES_TAG=dev-hostile-tag
    AGNES_IMAGE_REPO=europe-docker.pkg.dev/example-project/agnes/agnes
    STATE_DIR=/data/state
    COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml
    DOMAIN=agnes.example.com
    TLS_FULLCHAIN_URL=sm://certs/fullchain
    TLS_PRIVKEY_URL=
    TLS_CSR_SUBJECT="/C=US/ST=Illinois/L=Chicago/O=Some Org, Inc./CN=agnes.example.com"
    SCHEDULER_API_TOKEN=hostile-scheduler-token-`whoami`
    COMPOSE_PROFILES=mtier
    AGNES_INSTANCE_CUSTOM_PREAMBLE=NOTE `agnes` secure > all $HOME & <svg> 'quoted' | pipe
    """
)

_EXPECTED = {
    "AGNES_TAG": "dev-hostile-tag",
    "AGNES_IMAGE_REPO": "europe-docker.pkg.dev/example-project/agnes/agnes",
    "STATE_DIR": "/data/state",
    "COMPOSE_FILE": "docker-compose.yml:docker-compose.prod.yml",
    "DOMAIN": "agnes.example.com",
    "TLS_FULLCHAIN_URL": "sm://certs/fullchain",
    "TLS_PRIVKEY_URL": "",
    "TLS_CSR_SUBJECT": "/C=US/ST=Illinois/L=Chicago/O=Some Org, Inc./CN=agnes.example.com",
    "SCHEDULER_API_TOKEN": "hostile-scheduler-token-`whoami`",
    "COMPOSE_PROFILES": "mtier",
}

_BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(_BASH is None, reason="bash not available")

# A line that dot-sources / `source`s a *.env file.
_SOURCE_ENV = re.compile(r"^\s*(?:\.|source)\s+\S*\.env", re.MULTILINE)


def _extract_env_get(script_text: str) -> str:
    """Return the real `_env_get` function definition from a script."""
    lines = script_text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("_env_get()"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i].rstrip() == "}")
    return "\n".join(lines[start : end + 1])


@pytest.mark.parametrize("script_name", list(_SCRIPTS))
def test_no_whole_file_env_sourcing(script_name):
    """No host script may bash-source the whole `.env` (the outage class)."""
    text = (_OPS / script_name).read_text()
    assert not _SOURCE_ENV.search(text), f"{script_name} still dot-sources a .env file"
    assert "set -a" not in text, f"{script_name} still uses `set -a` env export"
    assert "_env_get" in text, f"{script_name} must read keys via the _env_get helper"


@pytest.mark.parametrize("script_name,keys", list(_SCRIPTS.items()))
def test_env_get_extracts_keys_from_hostile_env(script_name, keys, tmp_path):
    """The committed `_env_get` extracts the needed keys from a hostile `.env`
    and never shell-evaluates a value."""
    env_file = tmp_path / ".env"
    env_file.write_text(_HOSTILE_ENV)

    env_get = _extract_env_get((_OPS / script_name).read_text())
    # Point the helper's hardcoded path at the temp file (both path forms).
    env_get = env_get.replace('"$COMPOSE_DIR/.env"', f'"{env_file}"').replace("/opt/agnes/.env", str(env_file))

    queries = "\n".join(f'printf "%s=[%s]\\n" {k} "$(_env_get {k})"' for k in sorted(keys))
    prog = f"set -euo pipefail\n{env_get}\n{queries}\n"
    res = subprocess.run([_BASH, "-c", prog], capture_output=True, text=True)

    assert res.returncode == 0, f"{script_name}: _env_get crashed: {res.stderr}"
    for k in keys:
        assert f"{k}=[{_EXPECTED[k]}]" in res.stdout, (
            f"{script_name}: {k} not extracted correctly from a hostile .env\nstdout={res.stdout!r}"
        )
    # The free-text preamble (backtick `agnes`) must never have been executed.
    assert "command not found" not in res.stderr
