"""Every externally served Caddy site block records its requests.

The app logs no HTTP access lines of its own, so a request that reaches a
site block without a `log` directive leaves no record anywhere: a status a
user reports cannot be confirmed, dated, or counted afterwards.

`log` is SITE-SCOPED, not global — which is the whole reason this guard
exists. Adding it to the root `Caddyfile` covers exactly one of the four
shipped site blocks; the legacy-domain block beside it, the multi-tier proxy
(deploy/caddy/Caddyfile.mtier) and the hosted-apps vhost
(deploy/caddy/Caddyfile.apps-subdomain, appended to the Caddyfile at boot as
an INDEPENDENT site) each need their own, and nothing about editing one of
them points an author at the other three.

Same style as tests/test_caddyfile_metrics_deny.py and
tests/test_caddyfile_mtier.py: text-structure assertions, no Caddy binary and
no Docker.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple

_ROOT = Path(__file__).resolve().parent.parent

# Every Caddy config this repository ships and serves the outside world with.
# A new one belongs here the day it is added.
_CONFIGS: List[Path] = [
    _ROOT / "Caddyfile",
    _ROOT / "deploy" / "caddy" / "Caddyfile.mtier",
    _ROOT / "deploy" / "caddy" / "Caddyfile.apps-subdomain",
]


def _site_blocks(text: str) -> List[Tuple[str, str]]:
    """``(address, body)`` for each top-level site block.

    A site block opens on a line at column 0 that ends in ``{``. Caddy's
    global options block — a bare ``{`` — is not a site and is skipped; so
    are comment lines, which can end in a brace inside prose.
    """
    blocks: List[Tuple[str, str]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        opens = line.rstrip().endswith("{") and line[:1] not in ("", " ", "\t", "#")
        if not opens:
            i += 1
            continue
        address = line.rstrip()[:-1].strip()
        depth = 0
        body: List[str] = []
        while i < len(lines):
            depth += lines[i].count("{") - lines[i].count("}")
            body.append(lines[i])
            i += 1
            if depth == 0:
                break
        if address:  # a bare "{" is the global options block, not a site
            blocks.append((address, "\n".join(body)))
    return blocks


def test_every_shipped_config_declares_at_least_one_site() -> None:
    # Guards the parser itself: a silently empty block list would make every
    # assertion below vacuously true.
    for path in _CONFIGS:
        assert path.exists(), f"{path} is missing"
        assert _site_blocks(path.read_text()), f"no site block parsed out of {path}"


def test_every_site_block_logs_its_requests() -> None:
    missing: Dict[str, List[str]] = {}
    for path in _CONFIGS:
        for address, body in _site_blocks(path.read_text()):
            if "\n\tlog {" not in body:
                missing.setdefault(path.name, []).append(address)
    assert not missing, (
        "these Caddy site blocks serve requests without recording them: "
        f"{missing}. `log` is site-scoped — add a `log {{ output stdout / "
        "format json }}` block to each one."
    )


def test_logs_go_to_stdout_as_json() -> None:
    # The container log is what the host already ships; JSON is what a
    # collector can query. A `log` writing to a file inside the container
    # would satisfy the test above and still be invisible in practice.
    for path in _CONFIGS:
        for address, body in _site_blocks(path.read_text()):
            assert "output stdout" in body, f"{path.name} [{address}] does not log to stdout"
            assert "format json" in body, f"{path.name} [{address}] does not log JSON"
