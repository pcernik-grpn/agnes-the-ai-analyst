"""CLI artifact download + install script endpoints (#9)."""

import os
import re
import shlex
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse

# Strict allowlists for values interpolated into the generated install.sh.
# The endpoint is unauthenticated and users `curl | bash` it, so any shell
# metacharacter leaking through the Host header or AGNES_VERSION env var
# would become RCE. `shlex.quote` is applied on top for defense in depth.
#
# Host charset allows underscores (Docker Compose hostnames) and `[` `]` `:`
# so IPv6 literals like http://[::1]:8000 pass. Optional trailing path lets
# reverse-proxy deployments (request.base_url = "https://host/agnes/") work.
#
# `\Z` (not `$`) anchors strictly to end-of-string. Python's `$` also matches
# immediately before a trailing `\n`, which would let a crafted Host header
# like "good.example.com\n$(rm -rf /)" slip past the allowlist. `\Z` closes
# that bypass — shlex.quote downstream is still defense-in-depth.
_SAFE_URL_RE = re.compile(r"^https?://[A-Za-z0-9._\-\[\]:]+(:\d+)?(/[A-Za-z0-9._\-/]*)?\Z")
_SAFE_VERSION_RE = re.compile(r"^[A-Za-z0-9._\-]+\Z")

router = APIRouter(tags=["cli"])


def _dist_dir() -> Path:
    return Path(os.environ.get("AGNES_CLI_DIST_DIR", "/app/dist"))


def _find_wheel() -> Path | None:
    d = _dist_dir()
    if not d.exists():
        return None
    wheels = sorted(d.glob("*.whl"))
    return wheels[-1] if wheels else None


@router.get("/cli/latest")
async def cli_latest():
    """Metadata for the currently-shipped CLI wheel.

    Consumed by `agnes` CLI's auto-update check so it can warn when a newer
    version is on the server. Public + cacheable — no secrets here.
    Returns `version=None` when the server has no wheel yet (dev image that
    didn't run `uv build`).
    """
    wheel = _find_wheel()
    if not wheel:
        return {"version": None, "wheel_filename": None, "download_url_path": None}
    # PEP 427 wheel filename: {name}-{version}(-{build})?-{py}-{abi}-{plat}.whl
    # The version is the second `-`-separated token.
    parts = wheel.stem.split("-")
    version = parts[1] if len(parts) >= 2 else None
    return {
        "version": version,
        "wheel_filename": wheel.name,
        "download_url_path": f"/cli/wheel/{wheel.name}",
    }


@router.get("/cli/download")
async def cli_download():
    wheel = _find_wheel()
    if not wheel:
        raise HTTPException(
            status_code=404,
            detail=(
                "CLI wheel not found in dist dir. Build it with `uv build --wheel` "
                "or run the official docker image (which builds on image-build)."
            ),
        )
    return FileResponse(
        path=str(wheel),
        filename=wheel.name,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{wheel.name}"'},
    )


@router.get("/cli/wheel/{wheel_name}")
async def cli_wheel_versioned(wheel_name: str):
    """Serve the currently-present wheel at a PEP 427-compliant URL.

    Only the exact filename of the current wheel is honoured; any other
    `wheel_name` returns 404. No filesystem lookup is done from user input —
    the path param is only compared against `_find_wheel().name`.
    """
    wheel = _find_wheel()
    if not wheel or wheel.name != wheel_name:
        raise HTTPException(status_code=404, detail="Wheel not found")
    return FileResponse(
        path=str(wheel),
        filename=wheel.name,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{wheel.name}"'},
    )


@router.get("/cli/install.sh", response_class=PlainTextResponse)
async def cli_install_script(request: Request):
    """Shell installer — bakes this server's URL into the generated config."""
    base_url = str(request.base_url).rstrip("/")
    if not _SAFE_URL_RE.match(base_url):
        raise HTTPException(status_code=400, detail="Unexpected server URL format")
    version = os.environ.get("AGNES_VERSION", "dev")
    if not _SAFE_VERSION_RE.match(version):
        version = "dev"
    # shlex.quote hardens against anything that slipped past the regex
    server_q = shlex.quote(base_url)
    version_q = shlex.quote(version)
    script = f"""#!/usr/bin/env bash
# Agnes CLI installer — server: {base_url}
set -euo pipefail

SERVER={server_q}
echo "Installing Agnes CLI from $SERVER (version: {version_q})"

# 1. Download the wheel
# Portable mktemp: X's must be at the end of the template on both GNU and BSD/macOS.
TMPDIR_WHEEL=$(mktemp -d -t agnes_cli.XXXXXX)
trap 'rm -rf "$TMPDIR_WHEEL"' EXIT
# Use -OJ so curl honours Content-Disposition and saves the wheel with its real
# PEP-427 filename (pip / uv tool install reject filenames without a version).
# --max-redirs 0 because -OJ takes the saved filename from the response and -L
# follows redirects across hosts: a hostname alias answering 308 would have the
# redirect target's wheel installed with nothing on screen to say the download
# moved. Any redirect now aborts with
# `curl: (47) Maximum (0) redirects followed`.
(cd "$TMPDIR_WHEEL" && curl -fsSL --max-redirs 0 -OJ "$SERVER/cli/download")
WHEEL=$(ls "$TMPDIR_WHEEL"/*.whl 2>/dev/null | head -n1)
if [ -z "$WHEEL" ]; then
    echo "error: wheel download failed (no .whl found in $TMPDIR_WHEEL)" >&2
    exit 1
fi

# 2. Install via pip (prefer uv tool install if available)
if command -v uv >/dev/null 2>&1; then
    uv tool install --force "$WHEEL"
else
    python3 -m pip install --user --force-reinstall "$WHEEL"
fi

# 3. Seed/merge the server URL in CLI config. MERGE (do not truncate) so a
#    re-run of this installer on an already-initialized machine preserves
#    workspace_root and any other keys — a naive `cat > config.yaml` would wipe
#    them. Done in shell so it never depends on `agnes` being on PATH yet.
CFG_DIR="${{AGNES_CONFIG_DIR:-$HOME/.config/agnes}}"
mkdir -p "$CFG_DIR"
CFG="$CFG_DIR/config.yaml"
if [ -f "$CFG" ]; then
    CFG_TMP=$(mktemp "$CFG_DIR/config.yaml.XXXXXX")
    # grep -v exits 0 (kept non-server lines) or 1 (config was server-only —
    # an empty tmp is correct). ANY exit > 1 is a real read error: abort rather
    # than clobber config.yaml down to a single server: line (which would wipe
    # workspace_root and every other key). `rc=$?` in an || list is exempt from
    # `set -e`, so grep's benign rc=1 does not kill the script.
    grep -v '^server:' "$CFG" > "$CFG_TMP" 2>/dev/null && rc=0 || rc=$?
    if [ "$rc" -gt 1 ]; then
        echo "error: could not read existing config $CFG (grep exit $rc); leaving it untouched" >&2
        rm -f "$CFG_TMP"
        exit 1
    fi
    printf 'server: %s\\n' "$SERVER" >> "$CFG_TMP"
    mv "$CFG_TMP" "$CFG"
else
    printf 'server: %s\\n' "$SERVER" > "$CFG"
fi

echo "Installed."
echo "Next steps:"
echo "  1. Sign in to $SERVER and create a personal access token at $SERVER/me/profile"
echo "  2. Export it:   export AGNES_TOKEN=<your-token>"
echo "  3. Verify:      agnes auth whoami"
"""
    return script
