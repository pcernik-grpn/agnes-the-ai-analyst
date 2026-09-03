"""VM-derived sizing in the customer-instance startup script (TCRD-296).

``app_mem_limit`` / ``scheduler_mem_limit`` / ``extraction_worker_mem_limit``
default to ``"auto"`` (see ``infra/modules/customer-instance/variables.tf``):
instead of a fixed literal baked in at `terraform plan` time, the startup
script derives container memory ceilings and Postgres tuning from the VM's
own ``/proc/meminfo`` + ``nproc`` on every boot. Live finding on a
64-vCPU/251GB VM: a fixed 4g app cap OOM-killed uvicorn four times serving
DuckDB queries, and Postgres' stock settings (128-160MB shared_buffers, 4MB
work_mem, 5GB effective_cache_size) plus Docker's default 64MB ``/dev/shm``
made every parallel worker fail with "could not resize shared memory
segment" (~2850 times in 30 minutes), killing a facts extraction job.

Same marker-extraction idiom as ``tests/test_startup_vault_key.py``: the pure
sizing functions are pulled out of the template between the
``vm-sizing begin``/``vm-sizing end`` markers and executed under real bash,
so these tests exercise the exact code a VM boots, not a re-implementation.
The functions take RAM (MiB) / vCPU count as plain arguments rather than
reading ``/proc/meminfo`` themselves, which is what lets every test below
drive the arithmetic directly instead of depending on the runner's own host.
"""

import re
import shutil
import subprocess
from pathlib import Path

TPL = Path("infra/modules/customer-instance/startup-script.sh.tpl")

BEGIN = "# --- vm-sizing begin"
END = "# --- vm-sizing end"


def _sizing_block() -> str:
    tpl = TPL.read_text()
    m = re.search(re.escape(BEGIN) + r".*?\n(.*?)" + re.escape(END), tpl, re.DOTALL)
    assert m, (
        "startup-script.sh.tpl must contain the marker-delimited vm-sizing "
        f"block ({BEGIN!r} ... {END!r}) — the functional tests below execute it"
    )
    block = m.group(1)
    assert "${" not in block and "%{" not in block, (
        "vm-sizing block must not use Terraform interpolation ('${' / '%{'); "
        "keep it plain bash so tests execute exactly what ships in the template"
    )
    return block


def _call(function: str, *args: str) -> str:
    bash = shutil.which("bash")
    assert bash, "bash required"
    script = "set -euo pipefail\n" + _sizing_block() + f"\n{function} {' '.join(args)}\n"
    proc = subprocess.run([bash, "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, f"{function}({args}) failed: {proc.stderr}"
    return proc.stdout.strip()


# --- Textual plumbing: "auto" is resolved, not passed through verbatim ----


def test_env_heredoc_uses_resolved_bash_vars_not_raw_terraform_values():
    tpl = TPL.read_text()
    assert "AGNES_APP_MEM_LIMIT=$RESOLVED_APP_MEM_LIMIT" in tpl
    assert "AGNES_SCHEDULER_MEM_LIMIT=$RESOLVED_SCHEDULER_MEM_LIMIT" in tpl
    assert "AGNES_EXTRACTION_WORKER_MEM_LIMIT=$RESOLVED_EXTRACTION_WORKER_MEM_LIMIT" in tpl
    # The raw Terraform value is only read once each, to decide auto vs explicit.
    assert 'RESOLVED_APP_MEM_LIMIT="${app_mem_limit}"' in tpl
    assert 'RESOLVED_SCHEDULER_MEM_LIMIT="${scheduler_mem_limit}"' in tpl
    assert 'RESOLVED_EXTRACTION_WORKER_MEM_LIMIT="${extraction_worker_mem_limit}"' in tpl


def test_env_heredoc_carries_postgres_tuning():
    tpl = TPL.read_text()
    for line in (
        "AGNES_PG_SHARED_BUFFERS=$AGNES_PG_SHARED_BUFFERS",
        "AGNES_PG_EFFECTIVE_CACHE_SIZE=$AGNES_PG_EFFECTIVE_CACHE_SIZE",
        "AGNES_PG_WORK_MEM=$AGNES_PG_WORK_MEM",
        "AGNES_PG_MAINTENANCE_WORK_MEM=$AGNES_PG_MAINTENANCE_WORK_MEM",
        "AGNES_PG_MAX_PARALLEL_WORKERS_PER_GATHER=$AGNES_PG_MAX_PARALLEL_WORKERS_PER_GATHER",
        "AGNES_PG_SHM_SIZE=$AGNES_PG_SHM_SIZE",
    ):
        assert line in tpl, f"missing .env line: {line}"


def test_sizing_runs_after_the_uid_reservation_and_before_docker():
    """The uid reservation's guarantee is "before ANY package activity"
    (#2137 follow-up) — this section runs commands (awk, nproc), so it must
    NOT precede section 0's `if ! id -u agnes-applier` block (see
    tests/test_startup_datadog_toggle.py::test_nothing_executable_precedes_
    the_uid_reservation). It must still run before section 1 (Docker
    install) and well before .env is written, so nothing downstream depends
    on an unset variable."""
    tpl = TPL.read_text()
    uid_reservation_idx = tpl.index("if ! id -u agnes-applier")
    sizing_idx = tpl.index("AGNES_TOTAL_MEM_MB=$(awk")
    docker_idx = tpl.index("# --- 1. Docker (install if missing) ---")
    assert uid_reservation_idx < sizing_idx < docker_idx


# --- Functional: the pure functions, executed under real bash ------------


def test_clamp():
    assert _call("agnes_clamp", "5", "1", "10") == "5"
    assert _call("agnes_clamp", "0", "1", "10") == "1"
    assert _call("agnes_clamp", "99", "1", "10") == "10"


def test_app_mem_limit_gb_clamped_4_to_32():
    # Tiny VM floors at 4 GiB.
    assert _call("agnes_auto_app_mem_limit_gb", "2048") == "4"
    # 32 GiB RAM / 8 = 4 GiB.
    assert _call("agnes_auto_app_mem_limit_gb", "32768") == "4"
    # 64-vCPU/251GB live VM: 257024 MiB / 8 / 1024 = 31.
    assert _call("agnes_auto_app_mem_limit_gb", "257024") == "31"
    # Huge VM caps at 32 GiB.
    assert _call("agnes_auto_app_mem_limit_gb", str(1024 * 1024)) == "32"


def test_scheduler_mem_limit_is_fixed_2gb_regardless_of_ram():
    assert _call("agnes_auto_scheduler_mem_limit_gb") == "2"


def test_worker_mem_limit_gb_ratio_and_headroom():
    # Small VM: ratio (2*0.6=1) and headroom (2-4-8, negative) both bite —
    # floors at 4, then clamps to the VM's own total RAM (2 GiB).
    assert _call("agnes_auto_worker_mem_limit_gb", "2048") == "2"
    # Mid VM (32 GiB): ratio 32*0.6=19 GiB, headroom 32-4-8=20 GiB — ratio wins.
    assert _call("agnes_auto_worker_mem_limit_gb", "32768") == "19"
    # Live 64-vCPU/251GB VM: ratio 251*0.6=150 GiB, headroom 251-31-8=212 GiB
    # — ratio wins, well under the box's actual RAM.
    assert _call("agnes_auto_worker_mem_limit_gb", "257024") == "150"


def test_postgres_shared_buffers_25pct_capped_32gb():
    # 25% of 4 GiB.
    assert _call("agnes_pg_shared_buffers_mb", "4096") == "1024"
    # 251 GiB * 25% = ~64 GiB, capped to 32768 MiB (32 GiB).
    assert _call("agnes_pg_shared_buffers_mb", "257024") == "32768"


def test_postgres_effective_cache_size_60pct_uncapped():
    assert _call("agnes_pg_effective_cache_size_mb", "4096") == "2457"


def test_postgres_work_mem_clamped_16_to_128mb():
    assert _call("agnes_pg_work_mem_mb", "2048") == "16"  # floor
    assert _call("agnes_pg_work_mem_mb", "257024") == "128"  # cap


def test_postgres_maintenance_work_mem_capped_4gb():
    assert _call("agnes_pg_maintenance_work_mem_mb", "4096") == "256"
    assert _call("agnes_pg_maintenance_work_mem_mb", "257024") == "4096"  # cap


def test_postgres_max_parallel_workers_per_gather_min_4_or_nproc_div_8():
    assert _call("agnes_pg_max_parallel_workers_per_gather", "1") == "0"
    assert _call("agnes_pg_max_parallel_workers_per_gather", "8") == "1"
    assert _call("agnes_pg_max_parallel_workers_per_gather", "64") == "4"  # capped at 4
    assert _call("agnes_pg_max_parallel_workers_per_gather", "128") == "4"


def test_postgres_shm_size_2pct_floored_256mb():
    assert _call("agnes_pg_shm_size_mb", "2048") == "256"  # floor
    assert _call("agnes_pg_shm_size_mb", "257024") == "5140"


def test_full_resolution_end_to_end():
    """Execute the resolution/derivation lines that immediately follow the
    marker block in the template — auto-detection off "auto", pure
    pass-through for an explicit override — against fixed RAM/vCPU inputs.

    Only the ``AGNES_TOTAL_MEM_MB=$(awk ... /proc/meminfo)`` / ``AGNES_NPROC=
    $(nproc)`` lines are excluded (both variables are pre-set below instead):
    ``/proc/meminfo`` does not exist on a non-Linux dev machine, and this test
    must be able to drive the resolution logic with a fixed RAM/vCPU shape
    regardless of what the runner itself reports.
    """
    bash = shutil.which("bash")
    assert bash, "bash required"
    tpl_text = TPL.read_text()
    start = tpl_text.index("AGNES_NPROC=$(nproc)") + len("AGNES_NPROC=$(nproc)")
    end = tpl_text.index("\n\n# --- 1. Docker (install if missing) ---")
    section = tpl_text[start:end]
    # Unlike the marker block itself (asserted interpolation-free above), the
    # RESOLVED_*_MEM_LIMIT lines below use Terraform's `${app_mem_limit}` etc.
    # deliberately — bash's own `${VAR}` parameter expansion reads that same
    # syntax identically, so setting plain shell variables of the same names
    # below (before sourcing this section) exercises the exact code a real
    # `terraform apply` render produces, with no rewriting needed.

    script = (
        "set -euo pipefail\n" + _sizing_block() + "\n"
        "AGNES_TOTAL_MEM_MB=257024\n"  # the live 64-vCPU/251GB TCRD-296 VM
        "AGNES_NPROC=64\n"
        'app_mem_limit="auto"\n'
        'scheduler_mem_limit="auto"\n'
        'extraction_worker_mem_limit="16g"\n'  # explicit override must win untouched
         + section + "\n"
        'printf "%s\\n" "$RESOLVED_APP_MEM_LIMIT" "$RESOLVED_SCHEDULER_MEM_LIMIT" '
        '"$RESOLVED_EXTRACTION_WORKER_MEM_LIMIT" "$AGNES_PG_SHARED_BUFFERS" "$AGNES_PG_SHM_SIZE"\n'
    )
    proc = subprocess.run([bash, "-c", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.strip().splitlines()
    app, scheduler, worker, shared_buffers, shm_size = lines
    assert app == "31g"
    assert scheduler == "2g"
    assert worker == "16g", "an explicit override must pass through untouched"
    assert shared_buffers == "32768MB"
    assert shm_size == "5140m"
