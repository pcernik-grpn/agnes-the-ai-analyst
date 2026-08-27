"""Static contract for maintenance.html delivery to running VMs.

Caddy's `handle_errors 502 503` block (Caddyfile) serves
`/caddy-static/maintenance.html` during an app-container recreate — but
`static/maintenance.html` was never shipped to `/opt/agnes/static/` on any
real VM: not baked into the Dockerfile's `/opt/agnes-host/` artifact set
(so a fresh boot never gets it), and not in `agnes-auto-upgrade.sh`'s
`CONFIG_FILES` (so an already-running VM never picks it up either). Users
saw a raw connection error instead of the friendly page during every
redeploy. Pins both delivery paths so this can't regress silently again.
"""

import re
from pathlib import Path

DOCKERFILE = Path("Dockerfile")
AUTO_UPGRADE = Path("scripts/ops/agnes-auto-upgrade.sh")


def test_dockerfile_bakes_maintenance_html_into_agnes_host():
    body = DOCKERFILE.read_text()
    assert "mkdir -p /opt/agnes-host/static" in body, (
        "Dockerfile must create /opt/agnes-host/static/ so the recursive "
        "docker cp on VM boot preserves the static/ subdirectory Caddy expects"
    )
    assert "cp /app/static/maintenance.html /opt/agnes-host/static/" in body, (
        "Dockerfile must COPY static/maintenance.html into "
        "/opt/agnes-host/static/ — otherwise a fresh VM boot never gets "
        "the maintenance page"
    )


def test_auto_upgrade_config_files_includes_maintenance_html():
    body = AUTO_UPGRADE.read_text()
    m = re.search(r"CONFIG_FILES=\((.*?)\)", body, re.DOTALL)
    assert m, "agnes-auto-upgrade.sh must declare CONFIG_FILES"
    assert "static/maintenance.html" in m.group(1), (
        "CONFIG_FILES must include static/maintenance.html so a page-content "
        "edit propagates to already-running VMs (same rationale as Caddyfile)"
    )


def test_auto_upgrade_creates_parent_dir_before_extract():
    body = AUTO_UPGRADE.read_text()
    # The refresh loop must mkdir -p the destination's parent before the
    # docker-cp extraction writes its `.new` file there, since
    # static/maintenance.html introduces the first nested CONFIG_FILES path.
    m = re.search(r'for f in "\$\{CONFIG_FILES\[@\]\}"; do\n(.*?)\n\s*done', body, re.DOTALL)
    assert m, "could not find the CONFIG_FILES refresh loop"
    loop_body = m.group(1)
    assert 'mkdir -p "/opt/agnes/$(dirname "$f")"' in loop_body, (
        "refresh loop must mkdir -p the parent dir before extracting, or a "
        "nested CONFIG_FILES path (e.g. static/maintenance.html) fails on a "
        "VM where that subdirectory doesn't already exist"
    )
    assert loop_body.index("mkdir -p") < loop_body.index("extract_host_artifact"), (
        "mkdir -p must run BEFORE the extraction call in the loop"
    )
