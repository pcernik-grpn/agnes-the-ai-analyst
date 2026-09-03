"""Contract: the module's startup script leaves instance.yaml owner-only.

`/data/state/instance.yaml` holds the Postgres URL with its password inline,
plus whatever credentials an operator put in a connector overlay, and it sits
on the data volume that several non-root containers mount. Created by root
under the default umask it lands world-readable (0644), while the equivalent
`/opt/agnes/.env` is 0600 — the asymmetry this guard exists to prevent.

The chmod must sit OUTSIDE the `if [ ! -f "$INSTANCE_YAML" ]` create branch:
an existing file is exactly the case that needs repairing, since deployments
provisioned before this line — or written by an app-side surface that predates
its own chmod — already carry the loose mode.
"""

import os
import re
from pathlib import Path

import pytest

TPL = Path("infra/modules/customer-instance/startup-script.sh.tpl")


def _create_branch(body: str) -> str:
    """Return the `if [ ! -f "$INSTANCE_YAML" ]; then ... fi` block."""
    m = re.search(r'if \[ ! -f "\$INSTANCE_YAML" \]; then(.*?)\nfi\n', body, re.DOTALL)
    assert m, "expected the guarded instance.yaml create branch in the template"
    return m.group(1)


def test_tpl_chmods_instance_yaml():
    body = TPL.read_text()
    # Indented now — it sits inside the uid gate, see
    # `test_the_chmod_is_conditional_on_the_pin_having_taken`.
    assert re.search(r'^\s*chmod 600 "\$INSTANCE_YAML"', body, re.MULTILINE), (
        "startup-script.sh.tpl must chmod 600 $INSTANCE_YAML — root's umask "
        "otherwise leaves the DB password world-readable on the data volume"
    )


def test_chmod_is_outside_the_create_branch():
    body = TPL.read_text()
    assert 'chmod 600 "$INSTANCE_YAML"' not in _create_branch(body), (
        "the chmod must run on every boot, not only when the file is created "
        "— an already-existing 0644 file is the case that needs repairing"
    )


def test_create_branch_chowns_to_the_app_uid():
    """The create path hands a fresh instance.yaml to the app's uid.

    This is not on its own what decides the final owner — see
    ``test_recursive_state_chown_is_the_line_that_decides_the_owner`` — but a
    freshly created file must not be left owned by root, or the app cannot
    read it at 0600 even on the first boot.
    """
    body = TPL.read_text()
    assert 'chown 999:999 "$INSTANCE_YAML"' in body, (
        "the instance.yaml create branch must chown the file to uid 999 — "
        "root-owned at 0600 is unreadable by the app container"
    )


def test_recursive_state_chown_is_the_line_that_decides_the_owner():
    """0600 makes ownership load-bearing, and this is the line that sets it.

    ``chown -R agnes-applier:agnes-applier /data/state`` runs *after* the
    create branch's ``chown 999:999``, so it — not that line — determines who
    owns instance.yaml, and the applier re-creates the file under its own uid
    on every rewrite anyway. At 0600 the app container (uid 999) can therefore
    read the file only while ``agnes-applier`` resolves to uid 999, which
    ``useradd --system`` produces by allocation rather than by pin.

    The guard exists so a provisioning change to this line has to confront
    that dependency instead of silently locking the app out of its own
    config. Pinning the applier's uid is tracked separately; it is a fleet
    provisioning change, not a test fixture.
    """
    body = TPL.read_text()
    assert "chown -R agnes-applier:agnes-applier /data/state" in body, (
        "the recursive /data/state chown is what finally owns instance.yaml; "
        "if it moved or changed target, the uid-999 read assumption that 0600 "
        "depends on has to be re-established explicitly"
    )
    create_at = body.index('chown 999:999 "$INSTANCE_YAML"')
    recursive_at = body.index("chown -R agnes-applier:agnes-applier /data/state")
    assert recursive_at > create_at, (
        "the recursive chown is expected to run after the create branch — if "
        "that order flipped, the create branch would be the deciding line and "
        "this guard is pointed at the wrong one"
    )


APPLIER = Path("scripts/ops/agnes-state-applier.sh")


def test_applier_does_not_swallow_an_unreadable_instance_yaml():
    """A read failure must abort, not rebuild the file from an empty base.

    ``write_instance_yaml`` merges the new ``database:`` block into whatever
    the file already holds precisely so a backend switch keeps the operator's
    other sections. At 0644 the read always succeeded. At 0600 it can fail on
    a uid mismatch, and a blanket ``except Exception: existing = {}`` would
    turn that into a rewrite containing only ``database:`` — silently
    destroying every operator-set section, which is the failure the merge was
    introduced to prevent.
    """
    body = APPLIER.read_text()
    assert "except OSError" in body, (
        "write_instance_yaml must catch OSError separately and abort — a file "
        "it cannot read must not be rewritten from an empty base"
    )
    assert "except Exception:\n        existing = {}" not in body, (
        "the bare except around the instance.yaml read is what turns a PermissionError into a silent config wipe"
    )


def test_applier_propagates_a_refused_rewrite_to_its_caller():
    """Aborting only helps if the caller can tell the write did not happen."""
    body = APPLIER.read_text()
    assert 'return "$rc"' in body, (
        "write_instance_yaml must return the writer's exit status; a bare "
        "`return` discards it and the state machine logs a backend flip that "
        "never reached disk"
    )
    assert "if write_instance_yaml " in body, (
        "the post-migration success path must branch on the rewrite actually succeeding before it logs the flip"
    )


def test_every_write_instance_yaml_call_site_is_guarded():
    """A refused rewrite must never abort the applier mid-run.

    The applier runs under ``set -euo pipefail`` with ``trap '__rollback'
    ERR``. Once ``write_instance_yaml`` can exit non-zero, a *bare* call
    terminates the script at that point — and the two places it is called
    during a migration are followed by the lifecycle-flag handling and by
    step 4, which brings app+scheduler back up. Aborting there takes the
    instance offline and leaves it there: ``_recover_stuck_jobs`` only
    repairs jobs still in status ``running``, and a failed migration's job
    is already ``failed``.

    So every call site must either consume the status (``if …``) or discard
    it explicitly (``|| true``). A bare call is the bug.
    """
    lines = APPLIER.read_text().splitlines()
    bare = [
        (n, line.strip())
        for n, line in enumerate(lines, 1)
        # the call, not the definition, a comment, or a log line mentioning it
        if "write_instance_yaml " in line
        and not line.strip().startswith("#")
        and "write_instance_yaml() {" not in line
        and "logger" not in line
        and "echo" not in line
        and not line.strip().startswith("if ")
        and not line.strip().startswith("elif ")
        and "|| true" not in line
    ]
    assert not bare, (
        "unguarded write_instance_yaml call(s) — under `set -e` + the ERR trap "
        f"a refused rewrite aborts the applier before app+scheduler restart: {bare}"
    )


# ---------------------------------------------------------------------------
# Every writer of the overlay, not just the ones a review happened to find
# ---------------------------------------------------------------------------

_OVERLAY_WRITERS = [
    ("app/api/admin.py", "server-config editor + the narrow overlay writer"),
    ("app/api/initial_workspace.py", "_write_section / _drop_section"),
    ("src/db_state_machine.py", "write_backend_state"),
    ("scripts/ops/agnes-state-applier.sh", "the applier's embedded PyYAML writer"),
]


def _replace_calls_without_a_preceding_chmod(body: str) -> list[tuple[int, str]]:
    """Lines doing an ``os.replace`` onto the overlay with no ``os.chmod`` of
    the temp in the four lines above it."""
    lines = body.splitlines()
    offenders = []
    for n, line in enumerate(lines):
        if "os.replace(" not in line:
            continue
        window = "\n".join(lines[max(0, n - 6) : n])
        if "os.chmod(" not in window:
            offenders.append((n + 1, line.strip()))
    return offenders


def test_every_overlay_writer_chmods_the_temp_before_the_rename():
    """0600 is only worth anything if EVERY writer applies it.

    The overlay is rewritten from four places. A writer that misses the chmod
    does not merely leave one save world-readable — ``os.replace`` carries the
    temp file's mode onto the destination, so it relaxes the file
    permanently, including on instances a previous save or the startup script
    had already repaired. Two of the four were missed on the first pass here
    (`initial_workspace`), which is exactly why this guard enumerates writers
    instead of naming the ones a review happened to catch.

    The chmod must precede the rename. Chmodding the destination afterwards
    leaves the real path observable at the umask default for the window
    between the two calls, on a file holding the database url with its
    password inline.
    """
    from pathlib import Path as _P

    failures = {}
    for rel, what in _OVERLAY_WRITERS:
        offenders = _replace_calls_without_a_preceding_chmod(_P(rel).read_text())
        if offenders:
            failures[f"{rel} ({what})"] = offenders
    assert not failures, f"overlay writers renaming without a preceding chmod: {failures}"


def test_success_path_does_not_overwrite_the_migrator_terminal_status():
    """A completed migration must not be reported as a failure.

    The applier's post-migration ``write_instance_yaml`` is NOT the backend
    flip — ``scripts/db_state_migrator.py`` calls ``write_backend_state``
    immediately before ``mark_success``, so by the time the applier sees
    ``FINAL_STATUS=success`` the overlay already names the target. What runs
    here normalizes ``database.url`` from the migrator's pinned-IP form to the
    canonical hostname. Losing that leaves a working instance on the right
    backend, so stamping the job ``failed`` over the migrator's own terminal
    status would report a migration that moved every row as a failure.
    """
    body = APPLIER.read_text()
    start = body.index('if [ "$FINAL_STATUS" = "success" ]')
    end = body.index("\nelse\n", start)
    success_branch = body[start:end]
    assert 'update_job "$PENDING_JOB" "failed"' not in success_branch, (
        "the success branch must not mark the job failed — the migrator already "
        "wrote the terminal status and already flipped the backend"
    )


def test_an_unreadable_overlay_raises_rather_than_falling_back(tmp_path, monkeypatch):
    """Behavioural, and it fails on the old code for the RIGHT reason.

    ``app/instance_config.py`` wrapped the overlay read in a bare
    ``except Exception`` that logs the file as "corrupt" and falls back to the
    static base config. ``database.backend`` lives in that overlay, so a
    PermissionError there boots an instance whose data is on Postgres onto the
    DuckDB default and starts writing to the wrong store. At 0644 the read
    could not fail this way; at 0600 a uid mismatch is enough.

    The primary assertion is "it did not return", not "it raised type X" — an
    assertion keyed only on the new exception type would fail on the old code
    because the name does not exist yet, which proves nothing about behaviour.
    The type is checked second, on the exception actually caught.
    """
    if os.geteuid() == 0:
        pytest.skip("running as root — mode bits do not deny reads")

    import app.instance_config as ic

    state = tmp_path / "state"
    state.mkdir()
    overlay = state / "instance.yaml"
    overlay.write_text("database:\n  backend: postgres\n", encoding="utf-8")
    overlay.chmod(0o000)

    monkeypatch.setattr("app.secrets._state_dir", lambda: state)
    ic.reset_cache()
    try:
        raised = None
        try:
            cfg = ic.load_instance_config(strict=True)
        except Exception as exc:  # noqa: BLE001 — the type is asserted below
            raised = exc
        assert raised is not None, (
            "load_instance_config() returned instead of refusing: an unreadable overlay "
            f"silently fell back to the base config, whose database.backend is "
            f"{(cfg.get('database') or {}).get('backend')!r} rather than the overlay's"
        )
        assert type(raised).__name__ == "InstanceConfigUnreadable", (
            "the refusal must carry a distinct type so the boot path can tell it apart "
            f"from a soft config problem; got {type(raised).__name__}"
        )
    finally:
        overlay.chmod(0o600)
        ic.reset_cache()


def test_get_static_config_error_does_not_raise_when_never_loaded(tmp_path, monkeypatch):
    """`get_static_config_error()` is a diagnostic accessor — it must never
    itself raise, even on the exact process state it exists to explain.

    It calls `load_instance_config()` (default `strict=False`) before
    reading `_static_config_error`. `load_instance_config()`'s lenient
    fallback for an unreadable overlay only fires when `_last_good_config`
    is already set (`not strict and _last_good_config is not None`); on a
    process where the overlay was unreadable from the very first call —
    nothing good has EVER loaded — that condition is false regardless of
    `strict`, so it raises `InstanceConfigUnreadable` instead of falling
    back. A future admin-UI caller of this accessor would 500 on precisely
    the misconfigured instance the page exists to explain.
    """
    if os.geteuid() == 0:
        pytest.skip("running as root — mode bits do not deny reads")

    import app.instance_config as ic

    state = tmp_path / "state"
    state.mkdir()
    overlay = state / "instance.yaml"
    overlay.write_text("database:\n  backend: postgres\n", encoding="utf-8")
    overlay.chmod(0o000)

    monkeypatch.setattr("app.secrets._state_dir", lambda: state)
    # Simulate a fresh process that has never loaded a good config —
    # `ic.reset_cache()` alone does not clear `_last_good_config` /
    # `_loaded_once`, and other tests in this worker process may have
    # already populated them.
    ic._instance_config = None
    ic._last_good_config = None
    ic._loaded_once = False
    try:
        error = ic.get_static_config_error()
        assert isinstance(error, str) and error, (
            "get_static_config_error() must return a non-empty diagnostic string "
            "in this state, not raise or return None"
        )
    finally:
        overlay.chmod(0o600)
        ic.reset_cache()


def test_a_malformed_overlay_still_falls_back(tmp_path, monkeypatch):
    """The other half of the split — this one must NOT refuse to start.

    A malformed file is visible to the operator and repairable through the
    editor; refusing to boot on it is a worse trade than continuing on the
    base config. Only an unreadable one is fail-closed.
    """
    import app.instance_config as ic

    state = tmp_path / "state"
    state.mkdir()
    (state / "instance.yaml").write_text("database: [unclosed\n", encoding="utf-8")

    monkeypatch.setattr("app.secrets._state_dir", lambda: state)
    ic.reset_cache()
    try:
        cfg = ic.load_instance_config()
        assert isinstance(cfg, dict)
    finally:
        ic.reset_cache()


def test_the_boot_path_reraises_it_instead_of_logging_it():
    """`app/main.py` wraps the startup load in `except Exception` on purpose —
    a soft config problem must not stop an instance serving. That arm would
    also have swallowed this one, leaving the process up and 500ing every
    `get_value()` consumer while looking healthy. The refusal only exists if
    the boot path lets it through."""
    body = Path("app/main.py").read_text()
    assert "except InstanceConfigUnreadable:" in body, (
        "app/main.py must re-raise InstanceConfigUnreadable — otherwise the refusal "
        "to start is a comment, not a behaviour"
    )
    specific = body.index("except InstanceConfigUnreadable:")
    broad = body.index('logger.warning(f"Could not load instance config')
    assert specific < broad, "the specific arm must come before the broad one to be reachable"


def test_undecodable_overlay_bytes_fall_back_rather_than_propagating(tmp_path, monkeypatch):
    """The read split must not leak a THIRD failure mode.

    ``Path.read_text()`` raises ``UnicodeDecodeError`` — a ``ValueError``, not
    an ``OSError`` — for bytes that are not valid UTF-8, which is exactly the
    partial-write shape the lenient path was written for. Caught by neither
    arm it propagates, ``_instance_config`` is never assigned, the boot path's
    broad ``except`` logs it, and every later ``get_value()`` re-raises: an
    instance that looks healthy and 500s on everything, which is the failure
    the split exists to prevent.
    """
    import app.instance_config as ic

    state = tmp_path / "state"
    state.mkdir()
    (state / "instance.yaml").write_bytes(b"database:\n  backend: \xff\xfe not utf-8\n")

    monkeypatch.setattr("app.secrets._state_dir", lambda: state)
    ic.reset_cache()
    try:
        cfg = ic.load_instance_config()
        assert isinstance(cfg, dict), "undecodable bytes must degrade to the base config"
    finally:
        ic.reset_cache()


def test_a_refused_rollback_does_not_restart_into_the_transient_backend():
    """`use_pg()` treats `*_in_progress` as Postgres.

    So an overlay left naming the transient does not mean "the backend it was
    already using" — with a duckdb source it points the app at a Postgres the
    failed migration never filled. The restart is withheld on that branch;
    everything else the guard-the-bare-call fix was for still runs.
    """
    body = APPLIER.read_text()
    assert "SKIP_APP_RESTART=1" in body, (
        "the rollback-refused branch must withhold the app+scheduler restart — the "
        "overlay still names a transient that resolves to a different engine than the data"
    )
    gate = body.index('if [ "${SKIP_APP_RESTART:-0}" = "1" ]')
    restart = body.index("RESTART_LOG=$(dc up -d --no-deps --force-recreate app scheduler")
    assert gate < restart, "the gate must precede the restart to have any effect"


def test_an_unreadable_overlay_after_boot_degrades_instead_of_500ing(tmp_path, monkeypatch):
    """The other half of the new contract, and the reason it is gated.

    ``load_instance_config`` is reached from ``get_value()``, i.e. from
    essentially every request path, and ``reset_cache()`` re-runs it on a live
    instance after an admin save. A file that becomes unreadable AFTER a good
    boot — an operator chown, a half-finished manual repair — must not turn
    every request into a 500 where it used to degrade. The danger being
    guarded against is *starting* on the wrong ``database.backend``, and a
    process that already holds a good config is not about to do that.
    """
    if os.geteuid() == 0:
        pytest.skip("running as root — mode bits do not deny reads")

    import app.instance_config as ic

    state = tmp_path / "state"
    state.mkdir()
    overlay = state / "instance.yaml"
    overlay.write_text("database:\n  backend: postgres\n", encoding="utf-8")

    monkeypatch.setattr("app.secrets._state_dir", lambda: state)
    ic.reset_cache()
    try:
        first = ic.load_instance_config(strict=True)
        assert (first.get("database") or {}).get("backend") == "postgres"

        # Now it goes unreadable and an admin save drops the cache. This is
        # the real sequence: `reset_cache()` sets `_instance_config = None`,
        # so the fallback branch cannot lean on it — it has to hold the last
        # good config separately or it hands back the static base with every
        # operator-set section gone, while logging that it kept them.
        overlay.chmod(0o000)
        ic.reset_cache()
        again = ic.load_instance_config()
        assert isinstance(again, dict), "a live instance must keep serving, not raise per request"
        assert (again.get("database") or {}).get("backend") == "postgres", (
            "the overlay's settings must survive — returning the static base here is the "
            "silent-wrong-config outcome the boot refusal exists to prevent, just after boot"
        )

        # And it must be cached, or every request re-reads and re-parses the
        # static YAML and emits another ERROR line.
        assert ic._instance_config is not None, (
            "the fallback must populate the parse-once cache; without it `get_value()` "
            "redoes the whole load per request and floods the log"
        )
    finally:
        overlay.chmod(0o600)
        ic.reset_cache()


def test_neither_restart_path_starts_the_app_on_an_in_progress_backend():
    """Two restart sites, one invariant.

    `use_pg()` counts SIDE_CAR_IN_PROGRESS and CLOUD_IN_PROGRESS as Postgres,
    so an overlay left naming a transient points a duckdb-source instance at a
    database the failed or crashed migration never finished filling. Both the
    post-migration path and the stuck-job recovery bring app+scheduler back up,
    and both had to learn to withhold that when the rollback write was refused
    — fixing one and leaving the other is the shape this review keeps finding.
    """
    body = APPLIER.read_text()
    assert "RECOVERY_SKIP_RESTART=1" in body and "SKIP_APP_RESTART=1" in body, (
        "both the stuck-job recovery and the post-migration path must be able to withhold their restart"
    )
    assert '[ "$recovered_any" -eq 1 ] && [ "${RECOVERY_SKIP_RESTART:-0}" != "1" ]' in body, (
        "the stuck-job recovery's own restart must honour its own flag — it is a separate "
        "`dc up` from step 4's and inherits nothing"
    )


def test_the_two_restart_gates_do_not_share_a_flag():
    """Shell variables have no function scope, so one flag is one decision.

    The recovery's refusal is about ITS rollback write. A pending job later in
    the same tick can still migrate successfully — the migrator writes the
    target backend from its own container before `mark_success` — and step 4
    must be free to restart. Sharing the flag turned a completed migration
    into an instance left offline on a stale marker.
    """
    body = APPLIER.read_text()
    recovery_at = body.index("RECOVERY_SKIP_RESTART=1")
    recovery_fn_end = body.index("_recover_stuck_jobs\n", recovery_at)
    assert "SKIP_APP_RESTART=1" not in body[recovery_at:recovery_fn_end], (
        "the recovery path must not set step 4's flag — it leaks into the rest of the "
        "tick and blocks the restart after a migration that succeeded"
    )


def test_the_applier_uid_is_pinned_not_allocated():
    """0600 only works while the applier and the app are the same uid.

    `chown -R agnes-applier /data/state` decides who owns instance.yaml, and
    the applier re-creates the file under its own uid on every rewrite — so at
    0600 the app container (uid 999) can read its own config only while those
    two numbers match. `useradd --system` without `--uid` produces that match
    by allocating the top free id in the system range, which is not the same
    thing as intending it.

    #1217: the target uid is a single named variable (`AGNES_APPLIER_UID`),
    not a literal repeated at every call site — declared once near the top,
    with a comment tying it to the app container's uid, so the two useradd
    attempts and the readback check below can't drift apart from each other.
    """
    body = TPL.read_text()
    assert re.search(r"^AGNES_APPLIER_UID=999\s*$", body, re.MULTILINE), (
        "the applier's target uid must be a single declared variable "
        "(AGNES_APPLIER_UID=999), not a literal scattered across the file"
    )
    assert '--uid "$AGNES_APPLIER_UID"' in body, (
        "the state-applier user must pin its uid from $AGNES_APPLIER_UID — allocation happens "
        "to land on the app's uid on today's image, and 0600 turns that coincidence load-bearing"
    )


def test_the_chmod_is_conditional_on_the_pin_having_taken():
    """The mode must not outrun its own precondition.

    If the pinned uid was already taken at provisioning time the pin falls
    through to an allocated id, and a 0600 file the app does not own is one it
    cannot read — which, with the fail-closed read this change also
    introduces, is a refusal to start. A full outage in place of the silent
    degradation 0644 gave. Where the pin did not take, the mode stays as it
    was and the reason goes to the console.
    """
    body = TPL.read_text()
    assert "APPLIER_UID=$(id -u agnes-applier" in body, "provisioning must read back the uid it pinned"
    gate = body.index('if [ "$APPLIER_UID" = "$AGNES_APPLIER_UID" ]')
    chmod_at = body.index('chmod 600 "$INSTANCE_YAML"')
    assert gate < chmod_at, "the chmod must sit inside the uid gate, not before it"


def test_every_applier_useradd_attempt_shares_the_pinned_uid_variable():
    """#1217 was exactly this bug: the pin was added to ONE of two useradd
    call sites in this file and the second — the belt-and-braces block right
    before the `.env` chown — kept allocating an uid by chance. In the normal
    boot order the second block's `if` is always false (the first already
    created the user), so nothing observable caught the drift; it only bites
    on a reordering or a partial run. Every `useradd … agnes-applier` line in
    the template must pin the same variable so they cannot disagree.
    """
    body = TPL.read_text()
    # useradd invocations wrap across lines via `\` continuation — join
    # continued lines, then split into one logical string per invocation
    # (non-greedy up to the first `agnes-applier`, so a pinned attempt
    # immediately followed by `|| useradd …` is still two separate matches).
    logical_body = re.sub(r"\\\n\s*", " ", body)
    useradd_attempts = re.findall(r"useradd --system.*?agnes-applier(?: 2>/dev/null)?", logical_body)
    assert len(useradd_attempts) >= 4, (
        f"expected 2 pinned + 2 unpinned-fallback useradd attempts (primary block + B3-NEW "
        f"belt-and-braces block), got {len(useradd_attempts)}: {useradd_attempts}"
    )
    pinned = [a for a in useradd_attempts if '--uid "$AGNES_APPLIER_UID"' in a]
    unpinned_fallbacks = [a for a in useradd_attempts if "--uid" not in a]
    # Every pinned attempt must be followed by exactly one unpinned fallback
    # form (the `|| useradd …` without `--uid`, taken only when the pin fails)
    # — so counting them 1:1 catches a pinned attempt with no fallback
    # (bricks on any collision) as well as a fallback with no pinned attempt
    # first (silently never pins).
    assert len(pinned) == len(unpinned_fallbacks) and len(pinned) >= 2, (
        f"expected each pinned useradd attempt to have exactly one unpinned fallback; "
        f"pinned={pinned}, unpinned_fallbacks={unpinned_fallbacks}"
    )


def test_the_chmod_still_runs_outside_the_create_branch():
    """Moving it below the applier user must not move it INTO the create branch —
    an already-existing 0644 file is the case that needs repairing."""
    body = TPL.read_text()
    assert 'chmod 600 "$INSTANCE_YAML"' not in _create_branch(body)


def test_the_boot_refusal_is_not_defused_by_an_earlier_import(monkeypatch, tmp_path):
    """The guard must not depend on being the first read in the process.

    It was gated on `_loaded_once`, a module flag meant to mean "we are past
    startup". Importing `app.main` loads the config, so that flag was already
    True by the time the startup block ran and the refusal was permanently
    defused — a guard armed only when nothing else imported first is not a
    guard. Strictness is the caller's declaration now, so a prior successful
    load cannot disarm it.
    """
    if os.geteuid() == 0:
        pytest.skip("running as root — mode bits do not deny reads")

    import app.instance_config as ic

    state = tmp_path / "state"
    state.mkdir()
    overlay = state / "instance.yaml"
    overlay.write_text("database:\n  backend: postgres\n", encoding="utf-8")
    monkeypatch.setattr("app.secrets._state_dir", lambda: state)

    ic.reset_cache()
    try:
        ic.load_instance_config()  # a prior successful load, as any import does
        overlay.chmod(0o000)
        ic.reset_cache()
        with pytest.raises(ic.InstanceConfigUnreadable):
            ic.load_instance_config(strict=True)
    finally:
        overlay.chmod(0o600)
        ic.reset_cache()


def test_the_boot_path_drops_the_cache_before_its_strict_read():
    """A strict read that returns the cache inspects nothing.

    Importing `app.main` populates the parse-once cache, so the startup call
    would short-circuit on it and never touch the file — the check would pass
    on an instance whose overlay is unreadable.
    """
    body = Path("app/main.py").read_text()
    i = body.index("load_instance_config(strict=True)")
    window = body[max(0, i - 600) : i]
    assert "reset_cache()" in window, (
        "the boot check must drop the cache first; otherwise it validates a config that "
        "was loaded at import time and reads nothing"
    )


def test_a_failed_flip_verification_does_not_revert_the_backend():
    """The revert is right for a failure BEFORE the rows move, wrong after.

    `_flip_and_verify` can only fail once the data is already on the target, so
    routing it through the generic handler — which writes the source backend
    back before marking the job failed — would produce a config that disagrees
    with where the rows are. That is the source-vs-target split the whole H1
    machinery exists to prevent, arriving through the guard meant to catch it.
    """
    body = Path("scripts/db_state_migrator.py").read_text()
    assert "class BackendFlipNotVerified(" in body, (
        "the verification failure needs its own type — a bare RuntimeError falls to the generic handler, which reverts"
    )
    specific = body.index("except BackendFlipNotVerified as e:")
    generic = body.index("except Exception as e:\n        # Revert state to the source backend")
    assert specific < generic, "the specific handler must precede the reverting one to be reachable"
    arm = body[specific:generic]
    assert "write_backend_state(" not in arm, (
        "the BackendFlipNotVerified arm must not write the backend — the data is on the target"
    )
    assert "mark_failed(" in arm, "it must still mark the job failed"


def test_every_logging_call_resolves_to_a_real_module_logger():
    """A logger name that does not exist turns an error path into a NameError.

    `scripts/db_state_migrator.py` binds its logger as `log`. A handler written
    against `logger` raises inside its own `except` clause, so everything after
    the log line — `mark_failed`, the return code — never runs, and the caller
    sees a traceback plus a job stuck in `running`. For the
    `BackendFlipNotVerified` arm specifically that inverted the whole point:
    the applier then reads a non-terminal status, takes its failure branch and
    performs exactly the revert the exception exists to prevent.

    Ruff reports this as F821, but the repo's lint gate does not surface F821
    on this file — a pre-existing `F821 Undefined name 'BackendState'` sits
    there with CI green — so nothing catches it before runtime. Hence a check
    that costs nothing: every `X.info/debug/warning/error/exception(...)` must
    resolve to a name bound at module level.
    """
    import ast

    for rel in ("scripts/db_state_migrator.py", "scripts/ops/agnes-state-applier.sh"):
        if not rel.endswith(".py"):
            continue
        tree = ast.parse(Path(rel).read_text())
        bound = {
            t.id for node in tree.body if isinstance(node, ast.Assign) for t in node.targets if isinstance(t, ast.Name)
        }
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                bound.update(a.asname or a.name.split(".")[0] for a in node.names)

        methods = {"info", "debug", "warning", "error", "exception", "critical"}
        offenders = [
            (n.lineno, f"{n.func.value.id}.{n.func.attr}")
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in methods
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id not in bound
        ]
        assert not offenders, (
            f"{rel}: logging call(s) on a name never bound at module level {offenders} — "
            "this raises NameError inside whatever branch reaches it"
        )


def test_useradd_does_not_combine_mutually_exclusive_options():
    """`--gid` and `--user-group` cannot be used together.

    A form passing both always fails, so it is dead code that also makes the
    comment beside it claim a gid pin that never happens. Only the uid needs
    pinning here: instance.yaml is 0600, so the group bits grant nothing and
    the owner's uid alone decides who can read it.
    """
    body = TPL.read_text()
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue  # the fix's own comment names both flags
        assert not ("--gid" in stripped and "--user-group" in stripped), (
            f"mutually exclusive useradd options on one line: {stripped}"
        )


def test_the_applier_honours_the_migrator_no_revert_decision():
    """The rollback must not undo a decision made one layer down.

    `BackendFlipNotVerified` is raised only after the rows are on the target,
    and the migrator deliberately leaves instance.yaml alone for that reason.
    The applier's failure branch fires for every non-success status, so without
    reading the recorded class it would write the source backend back and point
    the app at the old store while every row lives in the new one.
    """
    body = APPLIER.read_text()
    assert "FAIL_CLASS" in body and "BackendFlipNotVerified" in body, (
        "the applier must read the failure class the migrator recorded"
    )
    guard = body.index('[ "$FAIL_CLASS" = "BackendFlipNotVerified" ]')
    rollback = body.index('write_instance_yaml "$SOURCE_BACKEND"', guard - 2000)
    assert guard < body.index('elif write_instance_yaml "$SOURCE_BACKEND"'), (
        "the class check must precede the rollback write, not follow it"
    )
    assert rollback is not None


# ---------------------------------------------------------------------------
# The applier's own writer must honour the same precondition the provisioning
# script gates its `chmod 600` on. Behavioural, not static: the functions are
# extracted from the shell script and driven in a sandbox with stubbed `id`
# and `chown`, so the assertions are about the mode instance.yaml actually
# ends up with and the group grant it was handed.
# ---------------------------------------------------------------------------

APP_UID = 999  # Dockerfile: `useradd --system --uid 999 … agnes`, `USER agnes`


def _extract_shell_functions(body: str, *names: str) -> str:
    """Return the source of the named shell functions, in file order.

    Relies on this script's house style: `name() {` on its own line, closing
    `}` at column 0.
    """
    out = []
    for name in names:
        m = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}$", body, re.DOTALL | re.MULTILINE)
        assert m, f"could not find shell function {name}() in {APPLIER}"
        out.append(m.group(0))
    return "\n\n".join(out)


def _write_instance_yaml_sandbox(
    tmp_path,
    *,
    process_uid: int,
    applier_uid: int | None,
    seed_mode: int | None,
    force_bash_fallback: bool = False,
    chown_works: bool = True,
) -> int:
    """Run the applier's `write_instance_yaml` against a sandboxed overlay.

    Returns the mode `/data/state/instance.yaml` is left at. `id` is stubbed
    so the test can put the process and the agnes-applier account on any uid;
    `chown` is stubbed because a test process cannot give a file away —
    `chown_works` drives whether the gid-999 grant probe (and the post-rename
    re-assert) reports success, and every invocation is appended to
    ``chown.log`` so tests can assert WHAT was granted. ``seed_mode=None``
    starts with no overlay at all (the fresh-provision case).
    """
    import shutil
    import stat as stat_mod
    import subprocess
    import sys

    tmp_path.mkdir(parents=True, exist_ok=True)
    overlay = tmp_path / "instance.yaml"
    if seed_mode is not None:
        overlay.write_text("logging:\n  level: debug\ndatabase:\n  backend: duckdb\n")
        os.chmod(overlay, seed_mode)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    applier_line = f"echo {applier_uid}; exit 0" if applier_uid is not None else "exit 1"
    (fake_bin / "id").write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "-u" ] && [ -z "${2:-}" ]; then echo ' + str(process_uid) + "; exit 0; fi\n"
        'if [ "$1" = "-u" ] && [ "$2" = "agnes-applier" ]; then ' + applier_line + "; fi\n"
        "exit 1\n"
    )
    # journald is not reachable from a test; swallow the warning.
    (fake_bin / "logger").write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$LOGGER_LOG"\nexit 0\n')
    # A non-root process cannot really give a file away, so `chown` is a stub
    # either way; its exit code decides whether the host can hand the overlay
    # to the app's gid (the probe + the post-rename re-assert both ride it).
    (fake_bin / "chown").write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$CHOWN_LOG"\nexit ' + ("0" if chown_works else "1") + "\n"
    )
    if force_bash_fallback:
        # Drives the PyYAML-less branch: `python3 -c 'import yaml'` must fail.
        (fake_bin / "python3").write_text("#!/usr/bin/env bash\nexit 1\n")
    else:
        # The interpreter running the tests definitely has PyYAML; the host's
        # bare `python3` may not.
        (fake_bin / "python3").write_text(f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n')
    for f in fake_bin.iterdir():
        os.chmod(f, 0o755)

    body = APPLIER.read_text().replace("/data/state/instance.yaml", str(overlay))
    funcs = _extract_shell_functions(
        body, "_instance_yaml_target_mode", "_instance_yaml_reassert_owner", "write_instance_yaml"
    )
    script = (
        "set -euo pipefail\n"
        + funcs
        + '\nwrite_instance_yaml side_car "postgresql+psycopg://agnes:pw@postgres:5432/agnes"\n'
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "LOGGER_LOG": str(tmp_path / "logger.log"),
        "CHOWN_LOG": str(tmp_path / "chown.log"),
    }
    proc = subprocess.run(
        [shutil.which("bash") or "/bin/bash", "-c", script],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, f"write_instance_yaml failed: {proc.stderr}"
    assert "backend: side_car" in overlay.read_text(), "the sandbox did not actually rewrite the overlay"
    return stat_mod.S_IMODE(os.stat(overlay).st_mode)


@pytest.mark.parametrize("force_bash_fallback", [False, True], ids=["pyyaml", "bash-fallback"])
def test_a_mismatched_host_writes_the_overlay_group_readable_not_0600(tmp_path, force_bash_fallback):
    """The documented degraded state must stay survivable — via the app's gid.

    When uid 999 is already taken, provisioning falls back to an allocated id
    and deliberately skips `chmod 600` (`test_the_chmod_is_conditional_on_the_
    pin_having_taken`) — the runbook and the bootstrap unit both promise the
    overlay stays readable by the app and everything keeps working.

    The applier's writer originally ignored that: it chmodded the temp 0600
    unconditionally and renamed it over the overlay, which re-owns the file to
    whoever wrote it. On a host where agnes-applier is not uid 999 the app
    container then owns neither the file nor a readable bit of it, and
    `app/main.py`'s fail-closed strict read refuses to start the instance —
    turning an availability-safe degradation into an outage, triggered by
    ordinary admin activity (every backend flip, cancel and stuck-job recovery
    rewrites this file). Found by Devin Review on #1298. The policy on such a
    host is 0640 with the app's gid (999) as the group — read granted through
    the group bit, never through an owner the app is not — with the
    post-rename ownership re-assert landing on `agnes-applier:999`.
    """
    mode = _write_instance_yaml_sandbox(
        tmp_path,
        process_uid=997,
        applier_uid=997,
        seed_mode=0o644,
        force_bash_fallback=force_bash_fallback,
    )
    assert mode == 0o640, (
        f"expected 0640 (the app reads via group {APP_UID}) on a host where the applier does not "
        f"resolve to uid {APP_UID}, got {mode:04o} — 0600 there locks the app out of its own "
        f"config and the next boot refuses to start"
    )
    chown_calls = (tmp_path / "chown.log").read_text()
    assert f"agnes-applier:{APP_UID}" in chown_calls, (
        "at 0640 the read grant IS the group — the post-rename ownership re-assert must land on "
        f"the app's gid, but chown was invoked with: {chown_calls!r}"
    )
    warnings = (tmp_path / "logger.log").read_text()
    assert "0640" in warnings, "the degraded rewrite must say which mode it chose and why"


@pytest.mark.parametrize("force_bash_fallback", [False, True], ids=["pyyaml", "bash-fallback"])
def test_an_app_written_0600_overlay_is_not_preserved_across_the_reown(tmp_path, force_bash_fallback):
    """Preserving an existing 0600 re-creates the exact outage, one tick later.

    On a mismatched host the overlay is routinely 0600-owned-by-999 *before*
    the applier touches it: every admin-triggered migration first calls
    `write_backend_state()` from inside the app container (uid 999), and that
    writer — like the admin config editors — chmods its temp 0600
    unconditionally, which is correct where it runs. The applier's next
    rewrite then re-owns the file to agnes-applier; carrying the 0600 across
    that rename leaves it owner-only under a uid the app is not, and the next
    boot fails closed (`load_instance_config(strict=True)`). So the first
    fix's "preserve, never widen" was exactly wrong here: the mode must be
    recomputed for the account the file will come to REST on. Found by Devin
    Review on #1298 (second round).
    """
    mode = _write_instance_yaml_sandbox(
        tmp_path,
        process_uid=997,
        applier_uid=997,
        seed_mode=0o600,
        force_bash_fallback=force_bash_fallback,
    )
    assert mode == 0o640, (
        f"a 0600 overlay rewritten by a mismatched-uid applier came out {mode:04o}; 0600 under a "
        f"non-{APP_UID} owner is unreadable by the app, and preserving the existing mode is how it happens"
    )


def test_the_group_grant_fallback_is_0644_and_says_so(tmp_path):
    """Where the gid-999 grant is impossible, availability still wins.

    The common mismatched host runs the applier as a non-root agnes-applier
    on an allocated uid with no gid-999 membership — `chown :999` fails
    there, and a 0640 whose group grant never landed is the same lockout
    0600 was. The fallback is 0644: the same degradation provisioning
    documents for this host state, but as a conscious decision with a logged
    warning naming the exposure (the database url with its password inline),
    not something falling out of a stat default. Never 0600, never silent.
    """
    mode = _write_instance_yaml_sandbox(
        tmp_path,
        process_uid=997,
        applier_uid=997,
        seed_mode=0o600,
        chown_works=False,
    )
    assert mode == 0o644, f"expected the documented 0644 fallback, got {mode:04o}"
    warnings = (tmp_path / "logger.log").read_text()
    assert "0644" in warnings and "world-readable" in warnings, (
        "the 0644 fallback must be announced with its exposure — it is a deliberate "
        f"availability-over-hardening trade, not a default; got: {warnings!r}"
    )


def test_a_fresh_overlay_is_created_by_policy_not_by_stat_default(tmp_path):
    """No file yet is a decision point, not a stat failure.

    The first policy read the existing mode with a `|| existing=644` fallback,
    so on a mismatched host the applier CREATED the overlay world-readable —
    database url and password inline — because there was nothing to preserve.
    Found by Devin Review on #1298 (second round). A fresh overlay follows the
    same policy as a rewrite: 0640 + group 999 where the grant lands, the
    logged 0644 degradation where it cannot.
    """
    grantable = _write_instance_yaml_sandbox(tmp_path / "a", process_uid=997, applier_uid=997, seed_mode=None)
    assert grantable == 0o640, (
        f"a fresh overlay on a mismatched host came out {grantable:04o}, not 0640 — "
        "world-readable-by-default is the finding this test pins"
    )

    degraded = _write_instance_yaml_sandbox(
        tmp_path / "b", process_uid=997, applier_uid=997, seed_mode=None, chown_works=False
    )
    assert degraded == 0o644
    assert "0644" in (tmp_path / "b" / "logger.log").read_text(), (
        "even the 0644 fallback on a fresh overlay must be logged, not silent"
    )


@pytest.mark.parametrize("force_bash_fallback", [False, True], ids=["pyyaml", "bash-fallback"])
def test_the_applier_still_tightens_the_overlay_it_will_own(tmp_path, force_bash_fallback):
    """…and the hardening must survive the fix.

    Where the pin DID take, the applier is uid 999, the app owns the file it
    writes, and 0600 is exactly right — the overlay carries the database url
    with its password inline on a volume several non-root containers mount.
    """
    mode = _write_instance_yaml_sandbox(
        tmp_path,
        process_uid=APP_UID,
        applier_uid=APP_UID,
        seed_mode=0o644,
        force_bash_fallback=force_bash_fallback,
    )
    assert mode == 0o600, (
        f"expected the 0600 tightening to still apply when the applier is uid {APP_UID}, got {mode:04o}"
    )


def test_the_applier_reads_the_owner_the_rename_will_leave_not_its_own_uid(tmp_path):
    """Running as root does not make 0600 safe — or unsafe — by itself.

    The pre-Phase-8.1 shape (and the in-flight tick described in the runbook)
    runs the applier as root. Root's own uid is never 999, but the `chown
    agnes-applier` after the rename still moves the file onto whatever that
    account resolves to, so the mode has to follow the account, not the
    process.
    """
    tight = _write_instance_yaml_sandbox(tmp_path / "a", process_uid=0, applier_uid=APP_UID, seed_mode=0o644)
    assert tight == 0o600, (
        f"root + agnes-applier on uid {APP_UID} ends up owned by the app; 0600 is safe (got {tight:04o})"
    )

    loose = _write_instance_yaml_sandbox(tmp_path / "b", process_uid=0, applier_uid=997, seed_mode=0o644)
    assert loose == 0o640, (
        f"root + agnes-applier on uid 997 hands the file to a uid the app is not — and root can "
        f"always grant group {APP_UID}, so the policy mode there is 0640 (got {loose:04o})"
    )
