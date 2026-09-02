"""Static checks for the customer-instance watchdog + DB-backup artifacts.

Like ``test_state_applier_unit_file.py``, these do NOT exercise systemd or a
VM — they assert the committed module files stay wired together: every file
under ``infra/modules/customer-instance/files/`` must be installed by the
startup-script template, the watchdog must keep grepping for the incident
signatures it was built around (the 2026-06 DuckDB index-corruption incident:
crash-loop via ``terminate called``, the invalidated-database "zombie" state,
WAL salvage data-loss events), and the Terraform plumbing for the alert
webhook must remain declared.
"""

import re
import shutil
import subprocess
from pathlib import Path

MODULE = Path("infra/modules/customer-instance")
FILES = MODULE / "files"


def test_module_files_exist():
    expected = {
        "agnes-watchdog.sh",
        "agnes-db-backup.sh",
        "agnes-db-verify.py",
        "agnes-watchdog.service",
        "agnes-watchdog.timer",
        "agnes-db-backup.service",
        "agnes-db-backup.timer",
    }
    actual = {p.name for p in FILES.iterdir()}
    assert expected <= actual, f"missing module files: {expected - actual}"


def test_watchdog_checks_incident_signatures():
    """The watchdog exists because of specific production failure modes —
    each grep below maps to one. Removing any of them silently re-opens the
    corresponding blind spot."""
    sh = (FILES / "agnes-watchdog.sh").read_text()
    for signature in [
        "terminate called",  # DuckDB FatalException crash loop
        "database has been invalidated",  # zombie: app healthy, writes 500
        "WAL replay failed",  # salvage = data-loss window
        "Failed to delete all rows from index",  # ART index desync (write path)
        "Failed to append to PRIMARY_",  # ART index desync (append path)
        "wal.discarded",  # salvage artifact on disk
        "RestartCount",  # container restart delta
        "oom_kill",  # cgroup OOM counter delta
        "/api/health",  # liveness probe
    ]:
        assert signature in sh, f"watchdog no longer checks for: {signature}"


def test_watchdog_label_precedence_and_webhook_optional():
    sh = (FILES / "agnes-watchdog.sh").read_text()
    # Environment label: explicit override > module-written stage >
    # AGNES_DEPLOYMENT_ENV (when the deployment sets it) > hostname.
    assert "ENV_LABEL" in sh
    assert "ENV_STAGE" in sh
    assert "AGNES_DEPLOYMENT_ENV" in sh
    # Empty webhook must mean log-only, not a crash.
    assert 'WEBHOOK_URL="${WEBHOOK_URL:-}"' in sh
    # Anti-spam hash must cover only the alert-type prefixes, one per line:
    # bodies embed per-run counts/timestamps (hash never repeats -> no
    # suppression), and a single-line join truncates to the first prefix
    # (over-suppression). Devin review on PR #623 caught the former.
    assert "printf '%s\\n' \"${ALERTS[@]}\" | sed 's/:.*//'" in sh, (
        "anti-spam hash must be computed from per-line alert-type prefixes"
    )


def test_backup_script_verifies_restore():
    sh = (FILES / "agnes-db-backup.sh").read_text()
    assert "agnes-db-verify.py" in sh, "backup must run the canary restore-verify"
    assert "system.duckdb" in sh
    # Retention must be bounded.
    assert "-mtime +7" in sh


def test_backup_script_pg_dump_restore_canary():
    """Postgres side-car coverage (WS I task 2): pg_dump the control-plane DB
    alongside the DuckDB backup, gated on the same persisted backend field
    startup-script.sh.tpl reads, plus a container-presence check, and prove
    the dump restores via a throwaway-DB canary before ever declaring
    success."""
    sh = (FILES / "agnes-db-backup.sh").read_text()
    # Same detection field + sed pattern as startup-script.sh.tpl's
    # PERSISTED_BACKEND check — must stay in lockstep so the two scripts
    # never disagree about which backend is active.
    assert "database.backend" in sh or "backend:" in sh
    assert "side_car" in sh
    assert "docker ps --format '{{.Names}}'" in sh, "must confirm the postgres container is actually running"
    # The dump itself: custom format (pg_restore-able), no password (local
    # trust auth — see scripts/db_state_migrator.py's backup_sidecar_pg).
    assert "pg_dump -U agnes -F c agnes" in sh
    # Restore-canary: throwaway DB, trivial sanity query, cleanup.
    assert "createdb -U agnes" in sh
    assert "pg_restore -U agnes -d" in sh
    assert "SELECT count(*) FROM users" in sh
    assert "dropdb -U agnes --if-exists" in sh
    # Forward reference to the DuckLake catalog living in the same PG.
    assert "DuckLake" in sh
    # Retention: the pg artifacts land in the same dated $DEST as
    # system.duckdb, so the existing -mtime +7 sweep covers them for free —
    # no separate retention block should exist.
    assert sh.count("-mtime +7") == 1


def test_webhook_payloads_are_json_escaped():
    """Both scripts embed $MSG (which includes the operator-configurable
    ENV_LABEL) into a JSON payload — an unescaped quote/backslash would
    malform the JSON and the alert would silently fail (Devin review on
    PR #623 caught the backup script missing this)."""
    escape = "sed 's/\\\\/\\\\\\\\/g; s/\"/\\\\\"/g'"
    for name in ["agnes-watchdog.sh", "agnes-db-backup.sh"]:
        sh = (FILES / name).read_text()
        assert escape in sh, f"{name} must JSON-escape the webhook payload"
        assert '\\"text\\": \\"$esc\\"' in sh, f"{name} must POST the escaped variable, not raw $MSG"


def test_verify_script_compiles_and_exercises_incident_statements():
    src = (FILES / "agnes-db-verify.py").read_text()
    compile(src, "agnes-db-verify.py", "exec")  # SyntaxError -> test failure
    # The canary must replay the two statement classes that failed in the
    # 2026-06 incident, inside a rolled-back transaction.
    assert "INSERT OR REPLACE INTO usage_session_summary" in src
    assert "usage_tool_daily" in src
    assert "ROLLBACK" in src


def test_shell_scripts_parse():
    bash = shutil.which("bash")
    assert bash, "bash required for syntax check"
    for name in ["agnes-watchdog.sh", "agnes-db-backup.sh"]:
        proc = subprocess.run([bash, "-n", str(FILES / name)], capture_output=True, text=True)
        assert proc.returncode == 0, f"{name} has syntax errors: {proc.stderr}"


def test_units_are_paired_and_persistent():
    for stem in ["agnes-watchdog", "agnes-db-backup"]:
        service = (FILES / f"{stem}.service").read_text()
        timer = (FILES / f"{stem}.timer").read_text()
        assert "Type=oneshot" in service
        assert f"ExecStart=/usr/local/bin/{stem}.sh" in service
        assert "OnCalendar=" in timer
        # Persistent=true: a missed tick (VM was off) runs on next boot.
        assert "Persistent=true" in timer


def test_startup_script_installs_every_module_file():
    """The tpl writes the files via a fileset loop, so a new file under
    files/ lands automatically — but the install/enable lines are explicit.
    Assert each artifact is referenced so a rename can't orphan one."""
    tpl = (MODULE / "startup-script.sh.tpl").read_text()
    assert "watchdog_files_b64" in tpl
    for name in [
        "agnes-watchdog.sh",
        "agnes-db-backup.sh",
        "agnes-db-verify.py",
        "agnes-watchdog.timer",
        "agnes-db-backup.timer",
    ]:
        assert name in tpl, f"startup-script does not install {name}"
    assert "enable_watchdog" in tpl
    # Operator-edited webhook must survive reboots when the TF var is empty
    # (same preserve pattern as AGNES_TAG).
    assert "EXISTING_WEBHOOK" in tpl


def test_terraform_plumbing_declared():
    variables = (MODULE / "variables.tf").read_text()
    assert 'variable "enable_watchdog"' in variables
    assert 'variable "alert_webhook_url"' in variables
    assert re.search(r'variable "alert_webhook_url"[\s\S]*?sensitive\s*=\s*true', variables), (
        "alert_webhook_url must be marked sensitive"
    )
    main = (MODULE / "main.tf").read_text()
    for ref in ["enable_watchdog", "alert_webhook_url", "watchdog_files_b64"]:
        assert ref in main, f"main.tf does not pass {ref} into the template"


def test_watchdog_reports_image_and_schema_changes():
    """Deployment-timeline info events (operator request after the
    2026-06-12 incidents, where RESTARTS/HEALTH alerts arrived with no
    context that an auto-upgrade had just recreated the container): the
    watchdog reports an app image change and a DB schema-version change as
    informational lines, tracked as run-to-run deltas in the state dir the
    same way RestartCount/oom_kill already are. Info lines must bypass the
    hourly alert-type anti-spam (they are one-shot by construction) and the
    first run must seed state silently (no spam on fresh installs)."""
    sh = (FILES / "agnes-watchdog.sh").read_text()
    # Separate info channel, distinct from incident ALERTS.
    assert "INFOS" in sh
    # Image-change line, fed from a persisted previous image id.
    assert "UPGRADE:" in sh
    assert '"$STATE/image"' in sh
    # Schema-change line, read from the /api/health body the liveness probe
    # already fetches (no extra DB access).
    assert "DB: schema" in sh
    assert '"current":' in sh
    assert '"$STATE/schema"' in sh
    # Info-only runs must still notify: the early-exit guard has to consider
    # both arrays, not just ALERTS.
    assert '[ "${#ALERTS[@]}" -eq 0 ] && [ "${#INFOS[@]}" -eq 0 ] && exit 0' in sh
