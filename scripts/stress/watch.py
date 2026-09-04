"""Watch a containerized service during a load run, and abort the run.

Two things a load run needs that no dashboard provides.

**The app log.** A connection-pool exhaustion (`QueuePool limit of size N
overflow M, connection timed out`) is invisible to infrastructure metrics:
the pool is a ceiling *inside* one process, so the database never looks
busy and the host never looks loaded. The only place it is ever stated is
the application's own log, so the run has to be reading it while it
happens.

**CPU throttling, fresh.** A container against its `cpus:` cap is throttled
by the kernel long before any host metric moves. The authoritative counter
is the cgroup's own `cpu.stat`, read here once a second over SSH rather
than through a metrics pipeline — a monitoring backend's minute-granular,
ingestion-delayed copy of this number is a fine record afterwards and a
useless trigger during.

The abort is a file. When throttling stays over the threshold, this writes
it; ``runner.py --abort-file`` sees it between steps and stops starting new
work. A file rather than a signal because the two processes are started
independently, often in different terminals, and a file is also the record
of *why* the run stopped.

Nothing here knows which host it is talking to: ``--ssh`` is the command
prefix (any ``ssh``/``gcloud compute ssh``/… invocation that accepts a
remote command as its final argument), so the deployment's own coordinates
stay in the operator's shell.

Usage::

    python scripts/stress/watch.py \\
        --ssh "gcloud compute ssh HOST --zone Z --project P --tunnel-through-iap --command" \\
        --service app --out-dir /path/run-dir \\
        --abort-throttle 0.35 --abort-file /path/ABORT
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Log lines worth pulling out of the stream by default. The first is H1's
# only possible witness; the rest are the shapes a saturated app produces
# around it.
DEFAULT_PATTERNS = [
    r"QueuePool",
    r"OperationalError",
    r"TimeoutError",
    r"Traceback",
    r"500 Internal",
    # Learned from the 10-user wave on 2026-09-04: the decisive evidence was
    # not in the app at all but in the agent engine, and it had to be
    # reconstructed afterwards from `docker logs --since`. These shapes make
    # the same evidence arrive live and timestamped.
    r"EADDRINUSE",
    r"liveness timeout",
    r"severed mid-turn",
    r"SandboxNotFound",
    r"engine_error",
    r"process with pid \d+ not found",
    r"Sandbox process exited",
    r"Failed to reconnect",
]

SAMPLE_INTERVAL_S = 1.0

# Consecutive failures to REOPEN a stream before we declare that evidence
# stream lost. A container being recreated takes a few seconds; a stream that
# cannot be reopened after this many tries is not coming back on its own, and
# a run continuing blind is worse than one that says so.
_SETUP_FAILURE_LIMIT = 5


def _remote_locate(service: str) -> str:
    """Shell that prints the cgroup cpu.stat path for a compose service."""
    return (
        f'CID=$(sudo docker ps --filter "label=com.docker.compose.service={shlex.quote(service)}" '
        '--no-trunc --format "{{.ID}}" | head -1); '
        '[ -n "$CID" ] || { echo "NOCONTAINER"; exit 1; }; '
        'echo "CID=$CID"; '
        'echo "STAT=/sys/fs/cgroup/system.slice/docker-$CID.scope/cpu.stat"'
    )


def _resolve_container(ssh: Ssh, service: str) -> str:
    """Current container id for a compose service. Re-resolved per attempt."""
    out = ssh.run(_remote_locate(service))
    ids = dict(line.split("=", 1) for line in out.strip().splitlines() if "=" in line and not line.startswith("WARN"))
    cid = ids.get("CID")
    if not cid:
        raise SystemExit(f"could not locate the {service!r} container: {out.strip()[:300]}")
    return cid


def _resolve_stat_path(ssh: Ssh, service: str) -> str:
    out = ssh.run(_remote_locate(service))
    ids = dict(line.split("=", 1) for line in out.strip().splitlines() if "=" in line and not line.startswith("WARN"))
    path = ids.get("STAT")
    if not path:
        raise SystemExit(f"could not locate cpu.stat for {service!r}: {out.strip()[:300]}")
    return path


class Ssh:
    """Runs remote commands through an operator-supplied command prefix."""

    def __init__(self, prefix: str) -> None:
        self.argv = shlex.split(prefix)
        if not self.argv:
            raise SystemExit("--ssh must be a non-empty command prefix")

    def run(self, remote: str, *, timeout: float = 120.0) -> str:
        proc = subprocess.run([*self.argv, remote], capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            raise SystemExit(f"ssh failed ({proc.returncode}): {proc.stderr.strip()[:400]}")
        return proc.stdout

    def popen(self, remote: str) -> subprocess.Popen:
        return subprocess.Popen(
            [*self.argv, remote],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )


def _stream_with_retry(
    ssh: Ssh,
    remote_factory,
    on_line,
    stop: threading.Event,
    label: str,
    on_reconnect=None,
    on_dead=lambda reason: None,
) -> None:
    """Consume a remote stream, reopening it if the transport dies.

    A load run's capture must outlive its own SSH tunnel. On 2026-09-04 a
    tunnel carrying both the app log and its cgroup sampler dropped
    mid-wave (`gcloud compute ssh … return code [255]`) and took 47 % of
    that wave's server-side record with it — the half containing the
    interval where load and the scheduler overlapped. A dropped transport
    is a gap in the evidence, not a reason to stop watching.

    ``remote_factory`` is re-invoked on every attempt so the container id
    is re-resolved: a container recreated mid-run gets a new id, and
    reattaching to the old one would silently capture nothing.
    ``on_reconnect`` lets a stateful consumer (the delta sampler) discard
    state that does not survive a new container.
    """
    attempt = 0
    consecutive_setup_failures = 0
    while not stop.is_set():
        # Opening the stream is inside the loop's error handling, not before
        # it: resolving a container that is mid-recreate raises, and an
        # unhandled raise here only kills this daemon thread — the watcher
        # would keep running and exit successfully with the evidence stream
        # silently gone.
        try:
            proc = ssh.popen(remote_factory())
        except BaseException as exc:  # SystemExit included: it must not kill the thread
            consecutive_setup_failures += 1
            print(
                f"[watch:{label}] could not open the stream "
                f"({consecutive_setup_failures}/{_SETUP_FAILURE_LIMIT}): {exc!r:.200}",
                file=sys.stderr,
                flush=True,
            )
            if consecutive_setup_failures >= _SETUP_FAILURE_LIMIT:
                on_dead(
                    f"{label}: could not reopen the stream after "
                    f"{_SETUP_FAILURE_LIMIT} attempts — this evidence stream is gone"
                )
                return
            time.sleep(min(2.0 * consecutive_setup_failures, 15.0))
            continue
        if attempt:
            print(f"[watch:{label}] stream reconnected (attempt {attempt})", file=sys.stderr, flush=True)
        produced = False
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                produced = True
                # Opening the process proves nothing: a missing cgroup path or
                # a vanished container yields a stream that exits at once. Only
                # a stream that actually delivered a line has proved healthy,
                # so the failure counter is cleared here and not at popen —
                # otherwise an open/exit loop retries forever, never reaches
                # the limit, and leaves an empty capture file behind a
                # apparently clean run.
                consecutive_setup_failures = 0
                if stop.is_set():
                    break
                on_line(line)
        finally:
            proc.terminate()
        if stop.is_set():
            return
        if not produced:
            consecutive_setup_failures += 1
            if consecutive_setup_failures >= _SETUP_FAILURE_LIMIT:
                on_dead(
                    f"{label}: the stream opened but produced nothing "
                    f"{_SETUP_FAILURE_LIMIT} times running — this evidence stream is gone"
                )
                return
        attempt += 1
        print(
            f"[watch:{label}] stream ended unexpectedly (rc={proc.returncode}, produced={produced}) — reconnecting",
            file=sys.stderr,
            flush=True,
        )
        if on_reconnect is not None:
            on_reconnect()
        time.sleep(min(2.0 * attempt, 15.0))


class Aborter:
    """Owns the single decision to stop the run, and the reason for it."""

    def __init__(self, path: Optional[Path]) -> None:
        self.path = path
        self.tripped = False
        #: Streams that gave up reopening. Not an abort — losing the capture
        #: is a reason to distrust the record, not to stop the load — but the
        #: watcher must exit non-zero so it cannot be mistaken for a clean run.
        self.dead_streams: list[str] = []

    def stream_dead(self, reason: str) -> None:
        self.dead_streams.append(reason)
        print(f"\n*** CAPTURE LOST *** {reason}\n", file=sys.stderr, flush=True)

    def trip(self, reason: str) -> None:
        if self.tripped:
            return
        self.tripped = True
        stamp = datetime.now(timezone.utc).isoformat()
        line = f"{stamp} {reason}"
        print(f"\n*** ABORT *** {line}\n", file=sys.stderr, flush=True)
        if self.path:
            self.path.write_text(line + "\n")


def _tail_log(
    ssh: Ssh,
    service: str,
    out_path: Path,
    patterns: list[str],
    stop: threading.Event,
    aborter: Aborter,
    abort_on_pool: bool,
    label: str = "app",
) -> None:
    """Stream the container log, keeping everything, echoing what matters.

    Everything is written to disk because the interesting line is often the
    one *before* the one you went looking for; only matches are echoed to
    the terminal so a live run stays readable.
    """
    rx = re.compile("|".join(patterns), re.IGNORECASE)
    fh = out_path.open("a")
    state: dict = {"matches": 0, "since": None, "seen": set()}

    def remote() -> str:
        cid = _resolve_container(ssh, service)
        # Resume from the last line we actually received, not from the live
        # tail. `--tail 0` would silently drop everything produced during the
        # disconnect — which is the window the reconnect exists to preserve,
        # and exactly where a saturation event hides. `--timestamps` is what
        # makes the resume point knowable; the overlap it re-delivers is
        # de-duplicated below.
        since = f" --since {shlex.quote(state['since'])}" if state["since"] else " --tail 0"
        return f"sudo docker logs -f --timestamps{since} {shlex.quote(cid)} 2>&1"

    def on_line(line: str) -> None:
        # `--since` is inclusive to the second, so a reconnect re-delivers the
        # tail of that second. Drop only lines already written, keyed on the
        # whole line: identical text in the same second is indistinguishable
        # and duplicating it would double-count a match and re-trip an abort
        # already acted on.
        stamp = line.split(" ", 1)[0] if " " in line else ""
        if stamp and stamp == state["since"]:
            if line in state["seen"]:
                return
            state["seen"].add(line)
        elif stamp:
            state["since"], state["seen"] = stamp, {line}
        fh.write(line)
        fh.flush()
        if rx.search(line):
            state["matches"] += 1
            print(f"[log:{label}] {line.rstrip()[:300]}", file=sys.stderr, flush=True)
            if abort_on_pool and "queuepool" in line.lower():
                aborter.trip(f"QueuePool exhaustion in the {label} log — H1 confirmed")

    try:
        _stream_with_retry(ssh, remote, on_line, stop, label, on_dead=aborter.stream_dead)
    finally:
        fh.close()
    print(f"[log:{label}] stream ended after {state['matches']} matched lines", file=sys.stderr)


def _sample_throttle(
    ssh: Ssh,
    service: str,
    out_path: Path,
    stop: threading.Event,
    aborter: Aborter,
    threshold: Optional[float],
    consecutive: int,
    warn: Optional[float],
    label: str = "app",
) -> None:
    """Sample ``cpu.stat`` once a second and write a JSONL row per sample.

    The reported ratio is ``Δnr_throttled / Δnr_periods`` — the share of CFS
    scheduling periods in which the kernel had to stop the container. It is
    bounded 0..1 and is the honest saturation signal: unlike CPU usage it
    does not need to be compared against a quota to be read, because being
    throttled at all already means the quota was the limit. It is also the
    only source that resolves a burst: the quota is enforced per 100 ms
    period, so a container can average a third of its cap for a minute and
    still be throttled solid for a second inside it.
    """
    fh = out_path.open("a")
    state: dict = {"prev": None, "over": 0}

    def remote() -> str:
        stat_path = _resolve_stat_path(ssh, service)
        # `cat || break`, and the read is its own statement rather than a
        # substitution inside `echo`: in a pipeline the exit status is `tr`'s,
        # so a container recreated underneath us would leave `cat` failing
        # while the loop happily emitted unparsable lines forever — the
        # reconnect (which re-resolves the new cgroup path) would never fire.
        return (
            f"while :; do S=$(cat {shlex.quote(stat_path)}) || break; "
            f"echo \"T $(date +%s.%N) $(echo \"$S\" | tr '\\n' ' ')\"; "
            f"sleep {SAMPLE_INTERVAL_S}; done"
        )

    def on_reconnect() -> None:
        # These are monotonic per-container counters. After a reconnect the
        # container may be a different one, so a delta against the old
        # baseline would be meaningless (or hugely negative).
        state["prev"] = None
        state["over"] = 0

    def on_line(line: str) -> None:
        if not line.startswith("T "):
            return
        parts = line.split()
        try:
            ts = float(parts[1])
        except (IndexError, ValueError):
            return
        fields: dict = {}
        for key, value in zip(parts[2::2], parts[3::2]):
            try:
                fields[key] = int(value)
            except ValueError:
                continue
        if not {"nr_periods", "nr_throttled", "throttled_usec"} <= fields.keys():
            return
        cur = {"ts": ts, **fields}
        prev = state["prev"]
        if prev is not None:
            d_periods = cur["nr_periods"] - prev["nr_periods"]
            d_throttled = cur["nr_throttled"] - prev["nr_throttled"]
            d_usec = cur["throttled_usec"] - prev["throttled_usec"]
            ratio = (d_throttled / d_periods) if d_periods > 0 else 0.0
            fh.write(
                json.dumps(
                    {
                        "ts": datetime.fromtimestamp(cur["ts"], timezone.utc).isoformat(),
                        "throttled_period_ratio": round(ratio, 4),
                        "throttled_ms": round(d_usec / 1000.0, 1),
                        "periods": d_periods,
                        "wall_s": round(cur["ts"] - prev["ts"], 2),
                    }
                )
                + "\n"
            )
            fh.flush()
            if warn is not None and ratio >= warn and (threshold is None or ratio < threshold):
                print(f"[throttle:{label}] {ratio:.0%} of periods", file=sys.stderr, flush=True)
            if threshold is not None and ratio >= threshold:
                state["over"] += 1
                print(
                    f"[throttle:{label}] {ratio:.0%} of periods ({state['over']}/{consecutive} over {threshold:.0%})",
                    file=sys.stderr,
                    flush=True,
                )
                if state["over"] >= consecutive:
                    aborter.trip(
                        f"{label}: CPU throttling >= {threshold:.0%} of scheduling "
                        f"periods for {consecutive} consecutive samples"
                    )
            else:
                state["over"] = 0
        state["prev"] = cur

    try:
        _stream_with_retry(ssh, remote, on_line, stop, label, on_reconnect=on_reconnect, on_dead=aborter.stream_dead)
    finally:
        fh.close()


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ssh", required=True, help="command prefix taking a remote command as its last arg")
    p.add_argument(
        "--service",
        action="append",
        default=None,
        help="compose service label; repeatable (default: app). Watch every service that "
        "can hold the evidence — the agent engine's log, not the app's, is where a "
        "sandbox failure is stated.",
    )
    p.add_argument("--out-dir", required=True)
    p.add_argument(
        "--abort-throttle",
        type=float,
        default=None,
        help="trip the abort at this share of throttled scheduling periods (0..1)",
    )
    p.add_argument("--abort-consecutive", type=int, default=3, help="samples over the line before tripping")
    p.add_argument(
        "--warn-throttle",
        type=float,
        default=None,
        help="print (never abort) at this share of throttled periods — the ramp exists to FIND "
        "saturation, so the onset should be visible without ending the run",
    )
    p.add_argument(
        "--no-abort-on-pool",
        action="store_true",
        help="do NOT abort on a QueuePool line (it is the finding the run is looking for, "
        "so aborting is the default: once seen, more load only repeats it)",
    )
    p.add_argument("--abort-file", default=None)
    p.add_argument("--pattern", action="append", default=None, help="extra log regex (repeatable)")
    p.add_argument("--duration", type=float, default=None, help="stop after N seconds")
    args = p.parse_args(argv)
    if not args.service:
        args.service = ["app"]

    # Pure input validation first, before anything reaches for the network:
    # an operator's bad regex should be a startup error, not an exception
    # inside a daemon thread that kills only that thread and lets the watcher
    # exit cleanly with no log capture at all.
    patterns = DEFAULT_PATTERNS + list(args.pattern or [])
    try:
        re.compile("|".join(patterns))
    except re.error as exc:
        raise SystemExit(f"--pattern does not compile: {exc}")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ssh = Ssh(args.ssh)

    located_services: list[tuple[str, str, str]] = []
    for service in args.service:
        located = ssh.run(_remote_locate(service))
        ids = dict(
            line.split("=", 1) for line in located.strip().splitlines() if "=" in line and not line.startswith("WARN")
        )
        container_id, stat_path = ids.get("CID"), ids.get("STAT")
        if not container_id or not stat_path:
            raise SystemExit(f"could not locate the {service!r} container: {located.strip()[:300]}")
        located_services.append((service, container_id, stat_path))
        print(f"[watch] {service} -> {container_id[:12]}", file=sys.stderr)

    abort_path = Path(args.abort_file) if args.abort_file else None
    if abort_path and abort_path.exists():
        raise SystemExit(f"{abort_path} already exists — delete it before starting a run")
    aborter = Aborter(abort_path)

    stop = threading.Event()
    threads: list[threading.Thread] = []
    for service, container_id, stat_path in located_services:
        threads.append(
            threading.Thread(
                target=_tail_log,
                args=(
                    ssh,
                    service,
                    out_dir / f"{service}.log",
                    patterns,
                    stop,
                    aborter,
                    not args.no_abort_on_pool,
                    service,
                ),
                daemon=True,
            )
        )
        threads.append(
            threading.Thread(
                target=_sample_throttle,
                args=(
                    ssh,
                    service,
                    out_dir / f"{service}-throttle.jsonl",
                    stop,
                    aborter,
                    args.abort_throttle,
                    args.abort_consecutive,
                    args.warn_throttle,
                    service,
                ),
                daemon=True,
            )
        )
    for t in threads:
        t.start()

    signal.signal(signal.SIGINT, lambda *_: stop.set())
    print(
        f"[watch] writing to {out_dir}; abort at "
        + (f"{args.abort_throttle:.0%} throttled periods" if args.abort_throttle else "QueuePool only")
        + " (Ctrl-C to stop)",
        file=sys.stderr,
    )
    deadline = time.monotonic() + args.duration if args.duration else None
    try:
        while not stop.is_set():
            if deadline and time.monotonic() > deadline:
                break
            if aborter.dead_streams:
                # Continuing would run the rest of the wave blind and then
                # report a record that is missing exactly the part nobody can
                # reconstruct afterwards.
                print(
                    "[watch] a capture stream is gone — stopping rather than watching blind",
                    file=sys.stderr,
                )
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    stop.set()
    if aborter.dead_streams:
        print(
            f"[watch] stopped — {len(aborter.dead_streams)} capture stream(s) were lost; "
            "the server-side record for this run is incomplete",
            file=sys.stderr,
        )
    else:
        print("[watch] stopped", file=sys.stderr)
    return 1 if (aborter.tripped or aborter.dead_streams) else 0


if __name__ == "__main__":
    raise SystemExit(main())
