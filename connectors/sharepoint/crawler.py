"""Built-in SharePoint document crawler — THE extraction pipeline.

Owner decision 2026-08-31 overrides the "the producer is never vendored"
rule this connector was originally written under: the crawl -> convert ->
(anonymize) -> ingest pipeline is a FIRST-CLASS Agnes pipeline, and it is
the ONLY one. The external-producer subprocess seam that used to sit in
``app/worker/kinds.py::_run_corpus_extraction`` — with its
``extraction.producer.*`` config, its curated child env and its Admin-grade
callback credential — was removed with that decision; that handler is now a
thin delegate to :func:`run_builtin_crawl` below.

What it does, per confirmed scope of one ``source_connections`` row:

1. Resolve the scope to one or more DRIVES (a site scope fans out to every
   document library; a drive scope is itself; a folder scope needs the
   scope row's ``drive_id`` and delta-enumerates that folder's subtree).
2. Enumerate each drive with Graph's ``delta`` feed, resuming from the
   persisted ``@odata.deltaLink`` so a re-run only sees what changed.
3. Per file: stream it to a TEMP file, convert to markdown, anonymize when
   the scope is marked for it, ingest the markdown into the scope's
   collection through the SAME internal path a wizard upload takes, and
   delete the temp file. The original bytes never persist on this host.

Hardening (all of it load-bearing on a ~1.6 TB / 100k-file estate — a run
outlives its access token, meets 429/503, and gets killed mid-pass):

* **Token refresh.** :class:`GraphAuth` re-acquires the app-only token a few
  minutes before expiry, through ``graph_client.get_app_token`` — this
  module never reimplements the certificate-credential flow.
* **410 Gone.** A dead ``deltaLink`` is dropped WITHOUT being persisted and
  that drive is re-enumerated from scratch, once. Persisting a dead link is
  how change detection silently stops forever.
* **Bounded 429.** ``Retry-After`` is honored but capped, per attempt AND in
  total per request; the budget being exhausted raises rather than sleeping
  forever, and every second waited lands in the run report.
* **5xx / transport retry.** 500/502/503/504 and connect/read timeouts retry
  with exponential backoff + jitter; 401 forces one token refresh.
* **Resume without skips or duplicates.** Crawl state (per-drive deltaLink,
  per-item cTag) is persisted at every delta-page boundary, and a drive's
  deltaLink is written only AFTER the rows that page produced were ingested
  — a crash costs re-work, never coverage. Re-ingesting an already-ingested
  item is a no-op: the ingest path matches on ``(collection, stable_id)``
  and an unchanged sha256 against an indexed row skips re-chunking.
* **A per-item failure never advances past itself.** A page that finishes
  cleanly still writes its deltaLink even when one of ITS rows failed to
  download/convert/anonymize/ingest — that is correct, the alternative pins
  the whole drive on one bad file. What is NOT correct is forgetting the
  failure: Graph's delta feed only re-offers an item when it CHANGES, so an
  item nobody touches again would otherwise never come back around. Every
  such failure is recorded in the state file's ``failed_items`` and replayed
  from there on every future run, independent of what delta reports, until
  it either succeeds (the entry is cleared) or exhausts
  :data:`_MAX_ITEM_RETRY_ATTEMPTS` (recorded as given-up — still visible in
  the state file and the run report, never silently dropped).
* **Oversize accounting.** A file over the size cap is skipped, counted, and
  its bytes reported — "we indexed the corpus" and "we indexed the small
  half of it" must never look the same in the report. Every other refusal
  (a drive this app registration cannot read, a file that would not convert,
  a document that could not be anonymized) gets its own counter for the same
  reason: nothing goes un-indexed invisibly.

Security posture:

* Credentials come from :func:`connectors.sharepoint.settings.
  resolve_sharepoint_settings` (vault slot, else the deployment's env var)
  and the anonymization key from ``app.worker.kinds._resolve_anonymization_key``
  — the existing, allowlist-gated resolvers. Nothing here reads a secret
  from anywhere else, and no token, key, or certificate is ever logged.
* Every URL this module sends the bearer token to is checked against
  :data:`_GRAPH_HOST` first (:func:`_require_graph_url`). ``@odata.nextLink``
  / ``@odata.deltaLink`` are values taken from a response body and replayed
  on the NEXT request with the ``Authorization`` header attached, so they are
  credential-egress destinations in the sense of the security playbook §8 —
  gated, never trusted because of where they came from.
* Broken-inheritance subtrees found by ``sharepoint-subtree-sweep`` are
  HONORED here (the external producer only ever received the list and was
  trusted to obey it): each excluded root is resolved once to its
  drive-relative path and every file under that prefix is skipped.
  Fail-closed — a scope whose exclusion roots cannot be resolved is not
  crawled at all.
* ``GraphTransport.download_to_temp`` follows a file download's 302 to its
  pre-authenticated URL BY HAND, on a separate request that carries no
  ``Authorization`` header — the bearer token must never reach the
  non-Graph host that redirect points at. See its own docstring.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import logging
import multiprocessing
import os
import random
import re
import signal
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

import httpx

from connectors.sharepoint import graph_client
from connectors.sharepoint.acl_sync import active_zone_rows, scope_rel_root
from connectors.sharepoint.graph_client import GRAPH_BASE, SharePointGraphError
from connectors.sharepoint.settings import SharePointSettingsError, resolve_sharepoint_settings
from connectors.sharepoint.shard_plan import SIGNAL_NONE, PlanningBudgetExhausted, compute_shard_plan

logger = logging.getLogger(__name__)

#: The ONLY host this module will attach an Authorization header to. Graph
#: hands back absolute ``@odata.nextLink``/``@odata.deltaLink`` URLs that we
#: replay verbatim; a redirect or a poisoned stored state file must not be
#: able to turn one into a credential-exfiltration destination.
_GRAPH_HOST = "graph.microsoft.com"

#: Names never worth crawling (OS/Office droppings), verbatim from the
#: reference crawler.
_SKIP_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
_SKIP_PREFIXES = ("~$",)  # Office lock files

#: A 403/404 on one drive or one item is a routine permissions fact, not a
#: broken connection — app-only access across a real tenant is never uniform.
#: Same set, and the same reasoning, as
#: ``graph_client._PERMISSION_SKIP_STATUS_CODES``.
_PERMISSION_SKIP_STATUS_CODES = frozenset({403, 404})

#: Transport policy. A 100k-file crawl meets every one of these.
_RETRY_STATUS = frozenset({500, 502, 503, 504})
_MAX_ATTEMPTS = 8  # per request, including the first try
_MAX_THROTTLE_WAIT_S = 900.0  # 429 sleep allowed per request, in total
_MAX_SINGLE_WAIT_S = 300.0  # cap on any ONE sleep, Retry-After included
_BACKOFF_BASE_S = 2.0  # doubled per attempt, plus jitter
_REQUEST_TIMEOUT_S = 120.0

#: Default per-file size cap (``extraction.crawler.max_file_mb``; 0 disables).
_DEFAULT_MAX_FILE_MB = 50
#: Default in-page item concurrency (``extraction.crawler.concurrency``).
#: Six is deliberately modest: the bound that matters on a real tenant is
#: Graph's own throttling, and the point of this knob is to stop OUR loop
#: from being the narrower one — not to race the tenant into a 429 storm.
#: ``1`` is the pre-parallel behaviour, exactly (see :func:`_process_page`).
_DEFAULT_CONCURRENCY = 6
#: Hard ceiling on the configured value. Past this the extra parallelism buys
#: 429s, temp-file pressure and RAM, never throughput. Sized for a large
#: conversion box (one core per in-flight file; the crawl parent holds
#: roughly 2 GB per in-flight file) — a small VM should stay well under it.
_MAX_CONCURRENCY = 64
#: Ceiling on a PER-RUN ``payload["concurrency"]`` override. Lower than the
#: configured ceiling on purpose: an ad-hoc run (an admin pressing "run now")
#: is the wrong place to go looking for a tenant's throttling limit.
_MAX_PAYLOAD_CONCURRENCY = 16

# --------------------------------------------------------------------------
# Auto-parallel-crawl (2026-09-03 design) — the ONE config knob
# (``extraction.crawler.shard_target_docs``, resolved by
# :func:`_shard_target_docs`) plus the named constants everything else uses.
# --------------------------------------------------------------------------

#: The knob's default — see :func:`_shard_target_docs`. 0 (an admin-set
#: override, never the default) disables sharding entirely.
_DEFAULT_SHARD_TARGET_DOCS = 5000
#: Hard ceiling on how many shards one site's plan may produce — mirrors
#: ``connectors.sharepoint.shard_plan.plan_shards``'s own ``max_shards``
#: default; passed through explicitly so the two never drift apart.
_MAX_SHARDS = 32
#: The child job kind a sharded site's planner enqueues — registered in
#: ``app/worker/kinds.py``, same EXTRACTION lane as ``corpus-extraction``.
_SHARD_JOB_KIND = "corpus-extraction-shard"
#: Below a default-priority job (``sharepoint-facts-extraction``, priority
#: 0) in ``claim_next``'s ``priority DESC`` order (design §4.6/Task 7): a
#: queued facts pass always claims before a queued shard, so a run's tail
#: (facts) is never starved behind a fresh site's initial shard fan-out.
_SHARD_JOB_PRIORITY = -1
#: Adaptive downshift trigger. A delta page that met MORE than this many
#: throttled responses — or spent more than :data:`_THROTTLE_BURST_WAIT_S`
#: waiting on them — is a tenant pushing back, not one stray 429.
_THROTTLE_BURST_429S = 2
_THROTTLE_BURST_WAIT_S = 30.0
#: Largest skipped files kept in the report.
_OVERSIZE_SAMPLE = 20
#: Per-file errors kept ON THE STATS OBJECT while the crawl runs. Matches
#: ``extraction_runs_pg._SKIPS_CAP`` (200) — the value the persisted row
#: itself is capped to at ``finish()`` — so the in-memory sample and the
#: stored one never disagree about how many are honestly kept.
_ERROR_SAMPLE = 200
#: Cap on the per-run ``failed_items``/``skipped_items`` lists an admin can
#: retry or just read (owner decision 2026-09-02, live whole-site crawl
#: finding: a conversion failure was invisible outside the worker log and
#: the delta cursor moved past it for good). Far larger than
#: :data:`_ERROR_SAMPLE` on purpose — that sample is a diagnostic preview,
#: this list is what ``retry_failed`` and an admin reading the run actually
#: work from, so it needs to name every failure a realistic bad run
#: produces, not just the first couple hundred. A pathological run beyond
#: this many still gets an honest ``*_truncated: true`` rather than an
#: unbounded row.
_FAILED_ITEMS_CAP = 5000
#: Completed items kept in the live checkpoint's `activity.recent` list.
_RECENT_ACTIVITY_SAMPLE = 5
#: A conversion worker is recycled after converting this many documents,
#: whichever slot it is. Insurance against a REAL, observed failure: crash
#: isolation (`_ConvertProcessPool`) fixed "one bad file kills the worker",
#: but a worker that never dies just keeps running — and markitdown/
#: pypdfium2 hold onto memory per document, so its RSS climbs without
#: bound over a large crawl. On a live deployment this reached ~8.4 GiB
#: across 6 slots (~22 documents each) before the container's memory
#: cgroup started SIGKILLing whichever child allocated next, INDISCRIMINATELY
#: — including a `.url`, a `.json` and a one-page `.docx`, files nowhere
#: near a gigabyte on their own. At that point isolation had only turned
#: "one bad file kills the worker" into "the worker survives but converts
#: nothing else", which is better but still fatal to a large crawl.
#: Configurable (``extraction.crawler.convert_recycle_after_docs``) because
#: the right number depends on the container's own memory ceiling, not on
#: this code; 0 disables the document-count trigger (the RSS trigger below
#: still applies).
_DEFAULT_CONVERT_RECYCLE_AFTER_DOCS = 40
#: A worker is ALSO recycled the moment its own peak RSS crosses this many
#: MiB, whichever trigger fires first — some documents are simply heavier
#: than others, so a fixed document count alone under-reacts to a run that
#: draws a cluster of large files early. Configurable
#: (``extraction.crawler.convert_recycle_rss_mb``); 0 disables it (the
#: document-count trigger above still applies).
_DEFAULT_CONVERT_RECYCLE_RSS_MB = 512
#: Each conversion child's OWN virtual-address-space ceiling (``RLIMIT_AS``,
#: installed once, right after fork — see ``_install_memory_limit``).
#: Insurance against a SEPARATE live-deployment finding from the one above:
#: recycling holds the STEADY STATE (per-child RSS observed at 530-760 MB
#: across 11 children), but a single pathological document can still spike
#: ONE child past the container's own ceiling in isolation — an .xlsx that
#: openpyxl loads whole into memory, in one observed case — and the kernel's
#: OOM killer then SIGKILLs whichever child happens to be allocating at that
#: moment, which is NOT necessarily the file that caused the spike (see
#: ``_ConvertCrashed``'s external-pressure framing). Capping the CHILD
#: rather than the container makes the common case attributable: a runaway
#: document now raises a plain ``MemoryError`` inside the process that read
#: it, reported as an ordinary ``convert_failed`` for THAT file, before it
#: can pressure any sibling. Raising the container's own memory limit is
#: NOT a fix for this — it only moves the ceiling a single heavy document
#: can still reach (observed at 4 GiB, then 12 GiB, then 20 GiB on the live
#: instance). Configurable (``extraction.crawler.convert_child_memory_limit_mb``);
#: 0 disables the cap. Interpreted as HEADROOM above whatever this worker
#: process's OWN VmSize happens to be at fork time, not an absolute
#: ceiling — see ``_install_memory_limit``'s docstring for the live
#: finding (a 64-vCPU worker's own ~2.2 GB VmSize) that made the absolute
#: reading unusable. Not enforceable on every platform (notably macOS,
#: where this repo's tests run) — see ``_install_memory_limit``.
_DEFAULT_CONVERT_CHILD_MEMORY_LIMIT_MB = 1536
#: A per-child RSS ceiling the PARENT itself enforces by polling (see
#: :meth:`_ConvertProcessPool._await_reply`), independent of — and a
#: backstop for — ``convert_child_memory_limit_mb`` above. A FOURTH
#: live-deployment finding, distinct from that one: ``RLIMIT_AS`` is
#: interpreted as HEADROOM above the worker's OWN VmSize *at fork time*
#: (see ``_effective_memory_limit_bytes``), so on a long-running crawl
#: parent whose own VmSize has grown to 6-15 GB after hours of crawling, a
#: single child converting one huge spreadsheet or PDF reached 10-17 GB
#: RSS before ``RLIMIT_AS`` — pinned to that already-large baseline — ever
#: fired; with ~80 concurrent children a replica climbed to 115 GB and had
#: to be SIGKILLed by a host-level watchdog script outside this pool's own
#: accounting, which then attributed the loss to whatever file happened to
#: be in flight AND left the slot dead until the next page boundary (see
#: ``convert_spares_per_slot`` below for the second half of that gap).
#: This watchdog polls the CHILD's own ``/proc/<pid>/status`` — not the
#: parent's — every :data:`_RSS_WATCHDOG_POLL_INTERVAL_S` seconds while a
#: conversion is in flight, so a child that grows past this absolute
#: ceiling (however large the parent has grown, unlike the headroom-based
#: RLIMIT_AS) is SIGKILLed and the file it was converting is counted an
#: attributable ``convert_failed`` — never the vaguer "killed by memory
#: pressure outside its control" wording a bare external SIGKILL gets
#: (see :func:`_convert_crash_detail`), because THIS SIGKILL was issued by
#: this pool itself, for a reason it can name. Configurable
#: (``extraction.crawler.convert_child_max_rss_mb``); 0 disables it.
#: Linux only (reads ``/proc/<pid>/status``) — a no-op on any platform
#: where that path does not exist, notably macOS, where this repo's own
#: tests run.
_DEFAULT_CONVERT_CHILD_MAX_RSS_MB = 4096
#: How often :meth:`_ConvertProcessPool._await_reply` polls a child's RSS
#: (and, when the per-item timeout is also enabled, whether it has
#: answered yet) while a conversion is in flight. Small enough that a
#: runaway allocation is caught promptly, large enough that polling
#: ``/proc/<pid>/status`` in a tight loop is not itself the parent's own
#: CPU cost on a large crawl.
_RSS_WATCHDOG_POLL_INTERVAL_S = 0.5
#: How many pre-forked, idle standby children :meth:`_ConvertProcessPool.
#: start`/:meth:`repair` keep ready PER SLOT — see the class docstring's
#: "RECYCLING" section for why spares exist at all. A FIFTH live-
#: deployment finding: the ORIGINAL design kept exactly one spare per
#: slot, forked only at a delta-page boundary — so a slot that recycled
#: or crashed a SECOND time inside the same (up to 200-item) page, before
#: the next :meth:`repair` had a chance to refill it, had nothing left to
#: swap in and simply kept running unbounded (a recycle) or stayed dead
#: for the rest of the page (a crash), exactly the pre-spares behaviour in
#: both cases. Keeping ``N`` spares per slot instead pushes that gap out to
#: the ``N``-plus-first recycle/crash in one page — never eliminated
#: entirely (see :meth:`_ConvertProcessPool.repair`'s own docstring for why
#: a MORE frequent safe point does not exist in the concurrent branch), but
#: far less likely to matter in practice. Configurable
#: (``extraction.crawler.convert_spares_per_slot``); 0 disables spares
#: outright (the pre-spares behaviour, exactly).
_DEFAULT_CONVERT_SPARES_PER_SLOT = 2
#: Sanity ceiling on the configured value above — each spare is one idle,
#: import-only process per slot, so this is a cost cap, not a tuned number.
_MAX_CONVERT_SPARES_PER_SLOT = 8
#: How long a single item's CONVERSION may run before its worker is killed
#: and the file counted an ordinary, attributable ``convert_failed`` — the
#: per-item TIME bound. Nothing previously bounded how long one document
#: could occupy a worker slot: the run-level deadline (``extraction.
#: timeout_s``) is only checked BETWEEN items, and a cooperative stop
#: request is polled at those same quiescent points, so a single item stuck
#: inside native conversion code made both unreachable. Observed on a live
#: deployment: one file occupied a slot for over nine minutes — 9m16s of
#: CPU and 5.8 GB RSS — with no bound at all; the run's deadline never
#: fired, an operator's stop request went unanswered for 20+ minutes, and
#: the kernel's OOM killer eventually ended the run, taking ~140 unrelated
#: in-flight files down with it.
#:
#: This bound lives at the CONVERSION-SUBPROCESS boundary
#: (:meth:`_ConvertProcessPool.convert`), not around the worker THREAD that
#: calls :func:`_prepare_document` (hash -> convert -> anonymize): a Python
#: thread cannot be forcibly cancelled, so a naive ``asyncio`` timeout
#: around that thread would abandon the runaway call while it keeps running
#: and keeps occupying one of this run's fixed ``ThreadPoolExecutor``
#: slots — over a large crawl, repeated timeouts would eventually exhaust
#: every slot and recreate the exact stall this bound exists to prevent,
#: just delayed. The conversion child is the one span of an item's pipeline
#: that already crosses an OS process boundary, so it is the one span that
#: can be forcibly reclaimed (SIGKILL) without leaking anything — the same
#: reasoning that motivated isolating conversion in its own process to
#: begin with, extended from crash isolation to hang isolation.
#: Configurable (``extraction.crawler.item_timeout_s``); 0 disables it (the
#: pre-bound behaviour exactly).
_DEFAULT_ITEM_TIMEOUT_S = 300
#: Ceiling on the CONVERTED MARKDOWN a child is allowed to send back across
#: the pipe, in MiB (0 disables). A THIRD, separate live-deployment finding
#: from the two above: recycling and `RLIMIT_AS` both hold the CHILD's own
#: memory down, but neither one bounds how big the converted TEXT itself is
#: allowed to get before it crosses back into the parent — where
#: `_prepare_document` (anonymize), `_Ingestor.ingest` (encode, store) and
#: `ingest_file` (re-read, chunk) each hold their own copy, on a PARENT
#: thread, entirely unisolated. On a live deployment a full run OOM-killed
#: the PARENT (uvicorn) at 12.3 GiB while every one of ten conversion
#: children sat idle at 0.0% CPU and ~240 MB — a spreadsheet's converted
#: markdown table can stay comfortably under the child's own `RLIMIT_AS`
#: ceiling the whole time (nothing there ever fires) while still being
#: large enough, multiplied across the documents concurrency lets run at
#: once, to exhaust the parent. This cap refuses the oversized result
#: (counted `convert_failed`, exactly like any other unconvertible
#: document — see `_convert_worker_main`) INSIDE the child, before
#: `_ConvertReply` is ever built, so the giant string never crosses the
#: pipe at all. Configurable (``extraction.crawler.max_converted_mb``).
#:
#: The number has to sit under what the converter can actually emit, or the
#: guard is decorative. ``convert.DEFAULT_MAX_CHARS`` caps a conversion at
#: 5,000,000 CHARACTERS, so the largest reply that can exist is that many
#: characters encoded as UTF-8: ~4.8 MiB of ASCII, ~14 MiB of CJK, ~19 MiB
#: at the 4-bytes-per-character worst case. A 200 MiB threshold was therefore
#: unreachable by construction and left the parent OOM it was written for
#: completely unaddressed (Devin Review on #2078). 8 MiB is chosen against
#: those numbers: an ordinary document — even a 5M-character one in a
#: single-byte script — passes untouched, while the multi-byte documents that
#: can actually reach double-digit megabytes are refused, which is exactly
#: the set that multiplies across concurrent slots into the parent's memory.
#: ``tests/test_sharepoint_convert_child.py`` pins the two ceilings together
#: so a future change to either cannot silently make this one decorative
#: again.
_DEFAULT_MAX_CONVERTED_MB = 8
#: Suffixes (no leading dot, lower-case) a real crawl has observed failing
#: `markitdown`'s conversion attempt on EVERY item of that type, identically,
#: forever — live deployment finding 2026-09: 868 persisted retry-queue
#: entries were mp4 (78), m4a (21), ipynb (17), vtt (7), pbix (4), vsdx (6)
#: and more, each re-downloaded and re-attempted on every crawl until
#: :data:`_MAX_ITEM_RETRY_ATTEMPTS` gave up, then sitting in the state file
#: forever. Checked in :func:`_process_item` BEFORE any download or convert
#: attempt (see the check just after the size cap) — distinct from
#: `convert_unsupported` (`src.ingest.convert.
#: UnsupportedConversionFormat`), which is markitdown itself discovering
#: mid-conversion that no backend `accepts()` a format it was not known in
#: advance to reject; both land in the SAME `skipped_unsupported` counter and
#: `skipped_items` list (never `errors`/`convert_failed`, never the retry
#: queue), because both mean "nothing was attempted". ``.zip`` is
#: DELIBERATELY absent: markitdown converts a zip's members and a live
#: corpus indexed 1,577 of them cleanly — an extension failing here is a
#: property of the FORMAT, not of "unusual to see in a document library".
#: Additive-only via ``extraction.crawler.unsupported_extensions`` — see
#: :func:`_unsupported_extensions`.
_DEFAULT_UNSUPPORTED_EXTENSIONS = frozenset(
    {
        # audio / video containers — no text content, no codec markitdown reads
        "mp4",
        "m4a",
        "mp3",
        "mov",
        "wav",
        "avi",
        "mkv",
        "wmv",
        "flv",
        "webm",
        # notebooks / source code — markitdown has no backend for these
        "ipynb",
        "sql",
        "py",
        # subtitles
        "vtt",
        "srt",
        # BI / diagramming formats with no text extraction path
        "pbix",
        "vsdx",
        # vector graphics — markup, not prose
        "svg",
        # fonts
        "ttf",
        "otf",
        "woff",
        "woff2",
        "eot",
        # executables / native binaries
        "exe",
        "dll",
        "so",
        "dylib",
        "bin",
        "msi",
    }
)
#: Delta page size asked of Graph — also the RESUME-STATE checkpoint
#: granularity (deltaLink + cTags, `_crawl_drive`): that one stays exactly
#: here, load-bearing for the resume contract. The run recorder's PROGRESS
#: checkpoint (`files_done`/`checkpoint_at`, `_RunRecorder.maybe_checkpoint`)
#: is a separate, more frequent cadence — see the constants below.
_DELTA_PAGE_SIZE = 200
#: How many passes a single item is retried through the failure queue (see
#: the module docstring's "a per-item failure never advances past itself")
#: before it is given up on. Bounds the cost of a permanently-broken file
#: (a corrupt document that will never convert) at a handful of retries per
#: run rather than forever; crossing it is recorded, never silent — see
#: :func:`_note_retry`.
_MAX_ITEM_RETRY_ATTEMPTS = 5
#: Live finding 2026-09-04 (#66 item 2): inside one folder with hundreds of
#: spreadsheets that had failed conversion in every previous run, each file
#: walked the FULL rescue chain again on every replay — page throughput fell
#: from ~70k items/h to ~100 items per 10 minutes. A document whose most
#: recent failure is DETERMINISTIC (`src.ingest.convert.
#: DETERMINISTIC_ERROR_CLASSES` — the same backend will reject the same
#: bytes again, every time) is skipped WITHOUT a download once it has failed
#: this many times — see `_doomed_skip_reason`. Deliberately much lower than
#: `_MAX_ITEM_RETRY_ATTEMPTS` above: a deterministic failure does not need
#: five tries to be trusted, only enough to rule out a one-off (a partial
#: download corrupting the file that one time).
_DOOMED_SKIP_MIN_ATTEMPTS = 2
#: TCRD-296 gap #74 (live finding 2026-09-04 09:20-10:00Z): a resync of a
#: 282k-document site spent its first hours replaying 253 previously-failed
#: documents in one folder — 162 `worker_crash` (the same workbook SIGKILLed
#: by the memory guard on every replay), 71 `timeout` (the same files hit
#: the time budget every run), 16 `libreoffice_no_output` — at roughly 5
#: documents per 10 minutes, because each replay pays the FULL rescue chain
#: again for nothing, and because these files never get a cTag, EVERY
#: future crawl/resync re-downloads and re-converts them too.
#: `timeout`/`memory_kill`/`worker_crash` are deliberately excluded from
#: `DETERMINISTIC_ERROR_CLASSES` (they are environmental — a busy host, a
#: transient resource ceiling — not a property of the document), but the
#: SAME file crashing or timing out the converter three times running, on
#: UNCHANGED bytes, is doomed for this product's purposes too: what actually
#: changes the outcome is an operator fix (raise the memory ceiling, split
#: the workbook), never a fourth automatic retry. See
#: `_REPEATED_FAILURE_ERROR_CLASSES` and `_doomed_skip_reason`. Higher than
#: `_DOOMED_SKIP_MIN_ATTEMPTS` above: a crash or timeout is more plausibly a
#: one-off (a noisy neighbour on the host, not a byte pattern) than a
#: deterministic reject, so it earns one extra attempt before this module
#: stops trying automatically.
_DOOMED_SKIP_MIN_ATTEMPTS_REPEATED = 3
#: The three environmental classes eligible for the repeated-failure doomed
#: rule above — string literals, not an import of `connectors.sharepoint.
#: convert.ERROR_CLASS_*`, mirroring how `DETERMINISTIC_ERROR_CLASSES` is
#: reached elsewhere in this module: via a LOCAL import inside the function
#: that needs it, never a module-level one. Deliberately NOT merged into
#: `DETERMINISTIC_ERROR_CLASSES` — a repeated environmental failure is
#: trusted at a higher attempt count and reported under its own reason
#: (`doomed_after_repeated_<class>`, see `_doomed_skip_reason`) so the run
#: report and the fleet view can tell "the bytes are rejected" apart from
#: "this keeps crashing or timing out the converter".
_REPEATED_FAILURE_ERROR_CLASSES = frozenset({"timeout", "memory_kill", "worker_crash"})
#: How often `_RunRecorder.maybe_checkpoint` is allowed to write PROGRESS
#: (never the resume state above) between delta-page boundaries: at most
#: once per this many seconds, or once per `_PROGRESS_CHECKPOINT_EVERY_
#: ITEMS` newly finished items, whichever comes first. A page can span many
#: minutes of real download/convert/anonymize/ingest work once downloads
#: actually succeed, and without this an operator watches "0 files
#: processed" for that whole window despite the crawl demonstrably working.
_PROGRESS_CHECKPOINT_INTERVAL_S = 5.0
_PROGRESS_CHECKPOINT_EVERY_ITEMS = 10
#: Streaming download chunk.
_DOWNLOAD_CHUNK = 1 << 20

#: ``parentReference.path`` prefix Graph puts in front of a drive-relative
#: path. Linear-time, anchored, bounded — no ReDoS surface (playbook §5).
_DRIVE_ROOT_PREFIX_RE = re.compile(r"^/drives/[^/]+/root:?")


class CrawlError(RuntimeError):
    """The crawl cannot run or cannot be trusted to be complete.

    Raised for configuration faults (no resolvable scope, an exclusion list
    that cannot be applied) and for a transport failure that exhausted its
    budget. Never carries a token, key, or certificate in its message.
    """


class GraphGone(Exception):
    """HTTP 410 — a ``deltaLink`` expired; that drive needs a full resync."""


class CrawlTimeout(CrawlError):
    """The run exceeded ``extraction.timeout_s``.

    Raised between files and between delta pages, i.e. always at a point
    where the crawl state on disk is consistent: the run aborts through the
    same path a :class:`GraphThrottled` abort takes, which persists the
    state and the (interrupted) report, and the next run resumes from the
    deltaLink + cTags already written. See also :class:`CrawlStopped` — the
    admin-requested counterpart to this deadline.
    """


class CrawlStopped(CrawlError):
    """An admin asked this run to stop (``POST …/extraction/stop`` —
    owner-frustration fix, 2026-09-01: "can't see it, can't stop it").

    Raised at the SAME quiescent points :class:`CrawlTimeout` is — see
    :func:`request_stop`, :func:`_clear_stale_stop` and :class:`_StopWatcher`
    below — so the crawl state on disk is exactly as consistent as a
    timeout's: the next run resumes from the persisted deltaLink + cTags.
    """


class GraphThrottled(CrawlError):
    """429s exceeded this request's attempt / total-wait budget."""


#: ``interrupted_reason`` for a run that stopped early at a KNOWN-CONSISTENT
#: point — the deltaLink/cTag state on disk describes exactly what was
#: ingested, so a caller may tell the operator the next run resumes from
#: there. Anything else records ``"error"``: after an unexpected exception
#: nothing is known about how far the state file got, and "your work is safe"
#: must never be guessed.
#:
#: Order is irrelevant (the classes are disjoint siblings), but the mapping
#: is a tuple rather than a dict because ``isinstance`` — not an exact type
#: lookup — is what has to decide, so a future subclass of any of them
#: inherits the right reason instead of silently falling through to "error".
_STOP_REASONS: tuple[tuple[type[BaseException], str], ...] = (
    (CrawlTimeout, "timeout"),
    (CrawlStopped, "stopped"),
    (GraphThrottled, "throttled"),
)


def _stop_reason(exc: BaseException) -> str:
    """``"timeout"`` / ``"stopped"`` / ``"throttled"`` / ``"error"`` for an
    aborted run."""
    return next((reason for cls, reason in _STOP_REASONS if isinstance(exc, cls)), "error")


# --------------------------------------------------------------------------
# Cooperative stop — the owner's other complaint ("can't stop it"), fixed
# WITHOUT a new store: the signal lives on the connection row's own
# ``config.extraction`` sub-object — the same JSON column
# ``_record_extraction_dispatch`` already writes ``last_run_at``/
# ``last_job_id`` into, one key over. That is what makes this work on BOTH
# app-state backends unlike ``extraction_runs`` (PG-only, A3): a DuckDB
# instance has always had ``source_connections``/``config_patch``.
#
# ``extraction`` is already carried forward whole by the generic connection
# editor (``app.api.admin_sharepoint.SHAREPOINT_SERVER_WRITTEN_CONFIG_KEYS``),
# so an admin editing the connection's name or certificate cannot erase a
# pending stop request any more than it can erase the dispatch bookkeeping
# next to it.
# --------------------------------------------------------------------------

#: The key itself, inside ``config["extraction"]`` — named once so the
#: three functions below and the admin endpoint that writes it
#: (``app/api/admin_extraction.py``) cannot spell it two different ways.
STOP_REQUESTED_AT_KEY = "stop_requested_at"


def request_stop(connection_id: str) -> str:
    """Ask this connection's crawl to stop at its next quiescent point.

    Returns the ISO timestamp recorded. Safe to call whether or not a run is
    currently active: a stop requested while nothing is running simply
    waits on the connection row until the NEXT run starts, at which point
    :func:`_clear_stale_stop` clears it unconsumed — a stop meant for a run
    that already finished (or never started) must never reach forward and
    kill a future, unrelated one.
    """
    from src.repositories import source_connections_repo

    repo = source_connections_repo()
    row = repo.get(connection_id) or {}
    extraction = dict((row.get("config") or {}).get("extraction") or {})
    stamp = _now_iso()
    extraction[STOP_REQUESTED_AT_KEY] = stamp
    repo.config_patch(connection_id, {"extraction": extraction})
    return stamp


def _clear_stale_stop(connection_id: str) -> None:
    """Clear any stop flag left over from a PREVIOUS run, at the START of
    THIS one. A stop requested for a run that already finished, failed, or
    never started must not be honored by the next, unrelated run — this is
    what keeps the flag from reaching forward past the run it was meant to
    stop.
    """
    from src.repositories import source_connections_repo

    repo = source_connections_repo()
    row = repo.get(connection_id) or {}
    extraction = dict((row.get("config") or {}).get("extraction") or {})
    if extraction.pop(STOP_REQUESTED_AT_KEY, None) is not None:
        repo.config_patch(connection_id, {"extraction": extraction})


def _stop_requested(connection_id: str) -> Optional[str]:
    """This connection's live stop flag, or ``None``.

    A fresh repo read every call — deliberately, not cached — because the
    signal is written by a DIFFERENT process (the admin API handling
    ``POST …/extraction/stop``) than the one running this crawl.
    """
    from src.repositories import source_connections_repo

    row = source_connections_repo().get(connection_id)
    if not row:
        return None
    extraction = (row.get("config") or {}).get("extraction") or {}
    stamp = extraction.get(STOP_REQUESTED_AT_KEY)
    return str(stamp) if stamp else None


#: Cadence for the file-boundary stop check. Between delta pages the check
#: is unconditional (a 200-row page is already several network round trips,
#: so one more row read is noise there) — but a repo read on EVERY file
#: would not be, on a fast, mostly-``unchanged`` re-crawl of a large estate.
#: Charged only once every this many COMPLETED items instead.
_STOP_CHECK_EVERY_ITEMS = 10


class _StopWatcher:
    """Polls :func:`_stop_requested` at the crawl's existing quiescent
    points — the cooperative-stop counterpart to :class:`_Deadline`.

    Unlike the deadline (an in-memory clock comparison), honoring a stop
    costs a repo read every time it is checked, which is why — unlike the
    deadline — the two call sites below use a DIFFERENT cadence: always
    between delta pages (:meth:`check_page_boundary`), but only every
    :data:`_STOP_CHECK_EVERY_ITEMS` completed items between files
    (:meth:`maybe_check_item_boundary`). Both raise :class:`CrawlStopped` at
    a boundary the resume guarantee already treats as consistent, exactly
    like a timeout.
    """

    def __init__(self, connection_id: str, *, every: int = _STOP_CHECK_EVERY_ITEMS) -> None:
        self.connection_id = connection_id
        self.every = max(1, int(every))
        self._last_checked = 0

    def check_page_boundary(self) -> None:
        self._raise_if_stopped()

    def maybe_check_item_boundary(self, items_done: int) -> None:
        if items_done - self._last_checked < self.every:
            return
        self._last_checked = items_done
        self._raise_if_stopped()

    def _raise_if_stopped(self) -> None:
        stamp = _stop_requested(self.connection_id)
        if stamp:
            raise CrawlStopped(f"stop requested at {stamp} — stopping; the next run resumes")


# --------------------------------------------------------------------------
# Crawl state (per connection): the per-drive deltaLink + per-item cTag map.
#
# Backed by ``connectors.sharepoint.state_store`` (``kind="crawl"``): a
# Postgres row when the active app-state backend is Postgres — so ANY
# extraction worker on ANY host can resume this connection's crawl, not just
# the one whose local disk holds the file — and the pre-existing per-
# connection JSON file otherwise (DuckDB, frozen app-state backend, A3). Not
# kept on the connection row's own `config` (where `acl_sync` keeps its own
# bookkeeping) for one reason: the cTag map is one entry per crawled
# document — six figures on a real estate — and a JSON column re-serialized
# on every checkpoint is the wrong home for that. The deltaLinks alone would
# fit; splitting the two halves across two stores would just create a way
# for them to disagree.
# --------------------------------------------------------------------------

#: ONE writer for the state store, and one mutator for the in-memory state it
#: is serialized from. Under in-page concurrency several items finish inside
#: one page and each writes its own cTag; the page boundary then serializes
#: the whole dict. A `json.dumps` (file backend) or the PG upsert's own
#: `json.dumps` racing a `dict.__setitem__` is a "dictionary changed size
#: during iteration" crash on the exact write the resume guarantee depends
#: on, so both sides take this lock. Re-entrant because the 410-resync path
#: mutates `delta_links` and then calls :func:`save_state` while still
#: holding it.
_state_lock = threading.RLock()


def state_path(connection_id: str) -> Path:
    """``<state dir>/sharepoint_crawl/<connection_id>.json`` — the DuckDB
    fallback (and, until imported, the Postgres path's own source of truth)
    location. See ``connectors.sharepoint.state_store.file_state_path``.
    """
    from connectors.sharepoint.state_store import StateStoreError, file_state_path

    try:
        return file_state_path("crawl", connection_id)
    except StateStoreError as exc:
        raise CrawlError(str(exc)) from exc


def _crawl_state_kind(shard_key: Optional[str] = None) -> str:
    """``"crawl"`` for the connection-level row; ``"crawl:<shard_key>"`` for
    one delta unit's own row (2026-09-03 auto-parallel-crawl design §4.2,
    migration ``0103_crawl_shards``) — a shard child never shares a state
    row with a sibling or with the connection-level row it seeds its
    ``legacy_ctags`` from."""
    return "crawl" if shard_key is None else f"crawl:{shard_key}"


#: ``ctags``/``failed_items``/``empty_items`` — the per-FILE collections
#: split OUT of the connection's hot ``sharepoint_connection_state`` row on
#: Postgres (migration ``0110_sharepoint_crawl_items``): a connection with a
#: few hundred thousand documents grew these three maps to tens of
#: megabytes combined, and every checkpoint rewrote that whole payload —
#: Postgres always produces a brand-new toasted value for a changed
#: ``jsonb`` column, even via ``jsonb_set`` targeting one key, so every
#: checkpoint orphaned the PREVIOUS copy's TOAST chunks (measured on a live
#: instance: ~21,900 dead tuples/minute, ~60 GB/day of table growth while a
#: crawl runs — reclaimed for reuse by autovacuum but never returned to the
#: OS). See :func:`load_state`/:func:`save_state` for the split, and
#: :class:`_TrackedDict`/:func:`_drain_item_deltas` for how a checkpoint
#: flushes only what actually changed.
_ITEM_TABLE_FIELDS: Tuple[str, ...] = ("ctags", "failed_items", "empty_items")


class _TrackedDict(dict):
    """A plain ``dict`` that also tracks which keys were set/removed since
    the last :meth:`drain_dirty` call — what lets :func:`save_state` flush
    only THIS checkpoint's changes into the per-item Postgres table
    (:data:`_ITEM_TABLE_FIELDS`) instead of rewriting the whole map.

    Every existing ``ctags[stable_id] = ...`` / ``failed_items.pop(sid,
    None)`` call site in this module keeps working completely unmodified —
    this is API-identical to a plain dict for every operation those sites
    use (``[]=``, ``.pop``, ``.get``, ``.items``, ``.values``, ``in``,
    ``len``).

    Mirrors ``connectors.sharepoint.facts_extraction._PartitionDocsLedger``
    (same problem, same shape) — kept as its own small class rather than a
    shared import: the two live in different modules for a reason (this
    module's own "a corrupt facts pass must never cost the crawl its
    deltaLinks" isolation argument), and neither should reach into the
    other's internals for something this small.
    """

    def __init__(self, initial: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(initial or {})
        self._dirty_set: set = set()
        self._dirty_removed: set = set()

    def __setitem__(self, key: str, value: Any) -> None:  # noqa: D105
        super().__setitem__(key, value)
        self._dirty_set.add(key)
        self._dirty_removed.discard(key)

    def pop(self, key: str, *default: Any) -> Any:  # noqa: D102
        had_key = key in self
        result = super().pop(key, *default)
        if had_key:
            self._dirty_removed.add(key)
            self._dirty_set.discard(key)
        return result

    def drain_dirty(self) -> Tuple[Dict[str, Any], List[str]]:
        """Every key set/removed since the last drain — ``(set_entries,
        removed)`` — and clears the tracking."""
        set_entries = {key: self[key] for key in self._dirty_set if key in self}
        removed = sorted(self._dirty_removed)
        self._dirty_set.clear()
        self._dirty_removed.clear()
        return set_entries, removed


def _drain_item_deltas(state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Snapshot what changed in ``ctags``/``failed_items``/``empty_items``
    since the last :func:`save_state` call — always returns all three
    fields, each ``{"set": {...}, "removed": [...], "reset": bool}``.

    ``reset`` is true when the caller reassigned the whole key to a plain
    ``dict`` rather than mutating a :class:`_TrackedDict` in place — the
    resync (``state["failed_items"] = {}``) and sharded-finalize
    (``state["ctags"] = {}``) call sites. The per-item store must then WIPE
    this field's column for every row of ``(connection_id, kind)`` before
    applying ``set`` — which, for every current reset call site, is always
    empty; the general case replays whatever the plain dict actually held,
    so a future caller that reassigns to a non-empty dict is handled the
    same way as "wipe, then set this as the new complete state".
    """
    result: Dict[str, Dict[str, Any]] = {}
    for field_name in _ITEM_TABLE_FIELDS:
        value = state.get(field_name)
        if isinstance(value, _TrackedDict):
            set_entries, removed = value.drain_dirty()
            result[field_name] = {"set": set_entries, "removed": removed, "reset": False}
        elif isinstance(value, dict):
            result[field_name] = {"set": dict(value), "removed": [], "reset": True}
        else:
            result[field_name] = {"set": {}, "removed": [], "reset": False}
    return result


def load_state(connection_id: str, shard_key: Optional[str] = None) -> Dict[str, Any]:
    """Read this connection's crawl state, tolerating a torn/absent file or
    a never-before-seen connection.

    Unreadable/missing state must not wedge every future run: the worst
    case of starting over is re-work (already-ingested documents upsert to
    a no-op), while refusing to run is a permanent outage.

    ``shard_key`` (optional — the shard-crawl seam, Task 1's ``state_for``)
    reads a SHARD's own per-delta-unit row instead of the connection-level
    one — see :func:`_crawl_state_kind`. ``None`` (every caller before
    sharding existed) is today's connection-wide row, unchanged.

    ``ctags``/``failed_items``/``empty_items`` (:data:`_ITEM_TABLE_FIELDS`)
    are popped off whatever the hot blob returned and either restored
    as-is (DuckDB fallback — they never left the blob) or replaced by
    fresh :class:`_TrackedDict`\\ s loaded from the per-item Postgres table,
    migrating a still-embedded legacy copy the first time this
    ``(connection_id, kind)`` is touched after the split (see
    ``connectors.sharepoint.state_store.crawl_items_get``). Shapes,
    unchanged by the split:

    * ``ctags``: ``stable_id -> cTag`` — the delta-detection cursor.
    * ``failed_items``: ``stable_id -> {state_key, path, item, attempts,
      ..., given_up}`` — see the module docstring's "a per-item failure
      never advances past itself" and :func:`_note_retry` /
      :func:`_retry_failed_items`.
    * ``empty_items``: ``stable_id -> {state_key, path, item,
      first_seen_at, last_seen_at}`` — every item this connection has seen
      convert to ``convert_empty`` (converted fine, no text at all — the
      scan-OCR candidate population). NOT replayed by the ordinary per-run
      backlog (:func:`_retry_failed_items`): re-running an empty document
      changes nothing while scan OCR is off, so every ordinary crawl would
      otherwise pay to re-walk the whole backlog for no reason. Replayed
      only by an explicit admin ``retry_empty`` run
      (:func:`_retry_empty_items`) — e.g. once an operator turns
      ``extraction.scan_ocr.enabled`` on and wants the existing backlog
      reconsidered. See :func:`_note_empty` for the same
      :data:`_FAILED_ITEMS_CAP` bound ``failed_items`` observes.
    """
    from connectors.sharepoint import state_store

    kind = _crawl_state_kind(shard_key)
    state: Dict[str, Any] = state_store.get(kind, connection_id) or {}
    state.setdefault("delta_links", {})

    legacy = {field_name: state.pop(field_name, None) or {} for field_name in _ITEM_TABLE_FIELDS}
    items = state_store.crawl_items_get(kind, connection_id, legacy=legacy)
    if items is None:
        # DuckDB fallback — unchanged: these three collections travel
        # inside the very same payload `state_store.get` just returned.
        for field_name in _ITEM_TABLE_FIELDS:
            state[field_name] = legacy[field_name]
    else:
        for field_name in _ITEM_TABLE_FIELDS:
            state[field_name] = _TrackedDict(items.get(field_name) or {})
    return state


def save_state(connection_id: str, state: Dict[str, Any], shard_key: Optional[str] = None) -> None:
    """Persist this connection's crawl state — a Postgres upsert, or an
    atomic file replace (tmp + ``os.replace``) on the DuckDB fallback.

    Serialized on :data:`_state_lock`: one writer at a time, and never
    concurrent with an in-page cTag write (which takes the same lock), so
    what lands is always a self-consistent snapshot.

    ``shard_key`` — see :func:`load_state`'s docstring; the write-side half
    of the same seam. A shard child's own ``save_for`` closure (Task 4)
    passes its target's ``state_key`` here so its checkpoint never lands in
    the connection-level row.

    On Postgres, :data:`_ITEM_TABLE_FIELDS` are flushed to the per-item
    table FIRST (its own transaction, committed) and excluded from the hot
    blob written second — the same "cTags on disk before the deltaLink
    advances" ordering the module docstring's page-boundary comment already
    relies on, now spanning two statements instead of one: a crash between
    them leaves the per-item table ahead of the hot blob, so a resumed
    crawl at worst RE-VERIFIES (never skips) this checkpoint's own files,
    exactly the safe direction the original single-write invariant gave.
    On the DuckDB fallback the three fields are restored into the hot blob
    exactly as before — a single atomic file replace, unchanged.
    """
    from connectors.sharepoint import state_store

    kind = _crawl_state_kind(shard_key)
    with _state_lock:
        deltas = _drain_item_deltas(state)
        pg_owns_items = state_store.crawl_items_apply(kind, connection_id, deltas)
        hot = {k: v for k, v in state.items() if k not in _ITEM_TABLE_FIELDS}
        if not pg_owns_items:
            for field_name in _ITEM_TABLE_FIELDS:
                if field_name in state:
                    hot[field_name] = state[field_name]
        state_store.put(kind, connection_id, hot)


# --------------------------------------------------------------------------
# Run recording — the second destination for the checkpoint this crawl
# already writes (2026-08-31 extraction-observability-ui design §7.1).
#
# Everything in this section is OBSERVABILITY, never load-bearing: the
# recorder swallows every failure of its own (including the typed
# `RequiresPostgresBackend` a DuckDB-backed instance raises the moment the
# PG-only repo is resolved) and the crawl runs on unchanged. A crawl that
# cannot be watched is worse than one that is; a crawl that FAILS because
# nobody could watch it is worse still.
# --------------------------------------------------------------------------


class _RunRecorder:
    """Writes this run's row: open at start, update at every existing
    checkpoint, finalize on done / interrupt / crash.

    Outcome precedence is severity-first — ``failed`` beats ``interrupted``.
    A crashed crawl is BOTH "did not finish" and "broke"; recording it as
    the benign outcome (and inviting the operator to trust the resume copy
    that attaches to it) is exactly the unverified-renders-healthy failure
    the design forbids. Only a cancellation/shutdown signal
    (``CancelledError``, ``KeyboardInterrupt``, ``SystemExit``) records as
    ``interrupted``; every other exception records as ``failed`` with its
    message.
    """

    def __init__(
        self,
        connection_id: str,
        *,
        job_id: Optional[str] = None,
        sweep_stale: bool = True,
        parent_run_id: Optional[str] = None,
        shard_key: Optional[str] = None,
        shard_label: Optional[str] = None,
        shards_total: Optional[int] = None,
    ) -> None:
        self.connection_id = connection_id
        self.job_id = job_id
        self.run_id: Optional[str] = None
        self._repo: Any = None
        #: Whether :meth:`start` sweeps this connection's leftover `running`
        #: rows before opening a new one (see `abandon_stale_running`).
        #: ``True`` (today's behaviour) for a whole-connection or PARENT run;
        #: a shard CHILD run (2026-09-03 auto-parallel-crawl design §4.3)
        #: passes ``False`` — a sibling shard's still-`running` row for the
        #: SAME connection is not abandoned, it is a peer, and sweeping it
        #: here would race the peer's own finalize.
        self._sweep_stale = sweep_stale
        #: Shard-crawl columns (migration ``0103_crawl_shards``) — all
        #: ``None`` for an inline or PARENT (planner) run, which is exactly
        #: today's row shape. A shard CHILD passes its own ``parent_run_id``/
        #: ``shard_key``/``shard_label``; a PARENT passes ``shards_total``
        #: (never the other three — a parent is not itself a shard).
        self._parent_run_id = parent_run_id
        self._shard_key = shard_key
        self._shard_label = shard_label
        self._shards_total = shards_total
        # Bookkeeping for `maybe_checkpoint` — a SEPARATE, rate-limited
        # sibling of `checkpoint`, never the crawl's own resume state.
        self._progress_lock = threading.Lock()
        self._last_progress_at = 0.0
        self._last_progress_items_done = 0
        # Bookkeeping for `maybe_checkpoint_facts` — kept SEPARATE from the
        # crawl's own above rather than reused: the two phases never run
        # concurrently for one run, but they count different units (files
        # vs. documents), and sharing the item-count field would start the
        # facts phase however many files short of the crawl's own tally,
        # silencing the item-count threshold for the rest of the pass.
        self._last_facts_progress_at = 0.0
        self._last_facts_progress_docs_done = 0

    def _resolve(self) -> Any:
        if self._repo is None:
            from src.repositories import extraction_runs_repo

            self._repo = extraction_runs_repo()
        return self._repo

    def start(self, *, phase: str = "crawl", clock: Callable[[], float] = time.monotonic) -> None:
        # Measured from here, not from process epoch: a run whose first
        # delta page takes a while must not have its very first item
        # trigger `maybe_checkpoint` purely because "now - 0" is huge.
        # ``clock`` is a test seam only — production never passes one.
        #
        # ``phase`` (2026-09-04 finding #65 item 3) lets a PARENT row open
        # as ``"planning"`` before the planner ever calls Graph, instead of
        # the row only existing (silently, as ``"crawl"``) once a plan is
        # already built — see ``_plan_or_run_inline``.
        self._last_progress_at = clock()
        try:
            repo = self._resolve()
        except Exception as exc:  # noqa: BLE001 — recording is never load-bearing
            self.run_id = None
            logger.info(
                "sharepoint crawl: run recording unavailable for connection %s (%s) — crawling anyway",
                self.connection_id,
                type(exc).__name__,
            )
            return
        # BEFORE opening this run's own row: only one crawl per connection
        # runs at a time (the trigger's own idempotency dedup on the owning
        # job), so a row still `running` here cannot be us — it is a
        # previous worker's crawl that died without ever calling `finish`.
        # Closing it now is what stops the source card from reading a
        # run that will never move again (see `abandon_stale_running`).
        # Skipped when `_sweep_stale` is False — a shard child's siblings
        # are legitimately still `running` for the same connection.
        if self._sweep_stale:
            try:
                abandoned = repo.abandon_stale_running(self.connection_id)
                if abandoned:
                    logger.warning(
                        "sharepoint crawl: closed %d abandoned run row(s) for connection %s before starting a new one",
                        len(abandoned),
                        self.connection_id,
                    )
            except Exception as exc:  # noqa: BLE001 — never blocks the new run
                logger.debug(
                    "sharepoint crawl: could not sweep abandoned runs for connection %s (%s) — continuing",
                    self.connection_id,
                    type(exc).__name__,
                )
        try:
            self.run_id = repo.start(
                connection_id=self.connection_id,
                job_id=self.job_id,
                phase=phase,
                parent_run_id=self._parent_run_id,
                shard_key=self._shard_key,
                shard_label=self._shard_label,
                shards_total=self._shards_total,
            )
        except Exception as exc:  # noqa: BLE001 — recording is never load-bearing
            self.run_id = None
            logger.info(
                "sharepoint crawl: run recording unavailable for connection %s (%s) — crawling anyway",
                self.connection_id,
                type(exc).__name__,
            )

    def checkpoint(self, stats: "CrawlStats") -> None:
        if not self.run_id:
            return
        try:
            self._resolve().checkpoint(
                self.run_id,
                phase="crawl",
                files_seen=stats.items_seen,
                files_done=stats.items_done,
                # The delta feed can always hand back another page, so
                # enumeration is never "done" until the run itself is.
                enumeration_done=False,
                progress=_progress_snapshot(stats),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("sharepoint crawl: run checkpoint failed (%s) — continuing", type(exc).__name__)
        # A shard CHILD's own checkpoint also bumps its PARENT's
        # `checkpoint_at` (design §4.3) — so a live parent whose children
        # are all still crawling never reads as stale to the liveness check
        # that derives "is this run alive" from checkpoint age
        # (`app/api/admin_extraction.py`), even though the parent's OWN
        # row stopped writing the moment it finished enqueueing. A no-op
        # for every non-child run (`_parent_run_id is None`).
        if self._parent_run_id:
            try:
                self._resolve().bump_parent_checkpoint(self._parent_run_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "sharepoint crawl: could not bump parent run %s checkpoint (%s) — continuing",
                    self._parent_run_id,
                    type(exc).__name__,
                )

    def maybe_checkpoint(self, stats: "CrawlStats", *, clock: Callable[[], float] = time.monotonic) -> None:
        """A RATE-LIMITED sibling of :meth:`checkpoint`, called after every
        item — not just at the delta-page boundary.

        A delta PAGE is up to :data:`_DELTA_PAGE_SIZE` (200) items, and once
        downloads actually succeed (as opposed to failing instantly), one
        page can be many minutes of real download/convert/anonymize/ingest
        work. `checkpoint` alone left an operator watching `files_done: 0`
        for that whole window even while the crawl was demonstrably
        working — the same "unverified renders healthy" failure the status
        guard elsewhere in this module exists to avoid, just for PROGRESS
        instead of OUTCOME.

        Fires at most once per :data:`_PROGRESS_CHECKPOINT_INTERVAL_S`
        seconds or :data:`_PROGRESS_CHECKPOINT_EVERY_ITEMS` newly finished
        items, whichever comes first, so a slow single file (elapsed time)
        and a fast run of small ones (item count) both get seen. Writes to
        the exact same destination `checkpoint` does
        (`files_seen`/`files_done`/`progress`/`checkpoint_at`) — never the
        crawl's own resume state (`deltaLink`/cTags in the state file),
        which still persists only at the page boundary in `_crawl_drive`
        and is unaffected by this.
        """
        if not self.run_id:
            return
        now = clock()
        with self._progress_lock:
            items_since = stats.items_done - self._last_progress_items_done
            due = (now - self._last_progress_at) >= _PROGRESS_CHECKPOINT_INTERVAL_S
            due = due or items_since >= _PROGRESS_CHECKPOINT_EVERY_ITEMS
            if not due:
                return
            self._last_progress_at = now
            self._last_progress_items_done = stats.items_done
        self.checkpoint(stats)

    def checkpoint_facts(
        self, stats: "CrawlStats", *, docs_done: int, docs_total: int, current_path: Optional[str] = None
    ) -> None:
        """The facts phase's own checkpoint (owner-frustration fix,
        2026-09-02): a healthy multi-hour facts pass never wrote here at
        all, so :data:`_STALL_AFTER_S`-derived liveness in
        ``app/api/admin_extraction.py`` declared it dead the moment it ran
        longer than the crawl phase's own checkpoint cadence — exactly
        backwards, since the facts phase is the run's most expensive part.

        Writes ``phase="facts"`` — the row stops claiming "crawl" for a
        stage that finished long ago — and a ``progress`` block LAYERED
        onto the crawl's own last snapshot rather than replacing it:
        `files_seen`/`files_done`/`new`/`changed`/... stay exactly what
        the crawl left them (still an honest fact about this run), because
        a `progress` write REPLACES the whole JSONB blob (see
        ``ExtractionRunsPgRepository.checkpoint``), and the crawl's own
        counters are not reported anywhere else while this run is still
        ``running``. `enumeration_done=True`: the crawl's own file
        enumeration genuinely finished before this stage could start.
        """
        if not self.run_id:
            return
        progress = _progress_snapshot(stats)
        progress["activity"] = {
            "phase": "facts",
            "current_path": current_path,
            "current_started_at": _now_iso() if current_path else None,
            "recent": progress["activity"].get("recent", []),
        }
        progress["facts"] = {"docs_done": docs_done, "docs_total": docs_total}
        try:
            self._resolve().checkpoint(
                self.run_id,
                phase="facts",
                files_seen=stats.items_seen,
                files_done=stats.items_done,
                enumeration_done=True,
                progress=progress,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("sharepoint crawl: facts checkpoint failed (%s) — continuing", type(exc).__name__)

    def maybe_checkpoint_facts(
        self,
        stats: "CrawlStats",
        *,
        docs_done: int,
        docs_total: int,
        current_path: Optional[str] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Rate-limited sibling of :meth:`checkpoint_facts`, same cadence
        rule as :meth:`maybe_checkpoint` (:data:`_PROGRESS_CHECKPOINT_
        INTERVAL_S` / :data:`_PROGRESS_CHECKPOINT_EVERY_ITEMS`) — over its
        OWN bookkeeping fields (`_last_facts_progress_at`/`_last_facts_
        progress_docs_done`), never the crawl phase's, so the very first
        call after the crawl hands off is always due.
        """
        if not self.run_id:
            return
        now = clock()
        with self._progress_lock:
            items_since = docs_done - self._last_facts_progress_docs_done
            due = (now - self._last_facts_progress_at) >= _PROGRESS_CHECKPOINT_INTERVAL_S
            due = due or items_since >= _PROGRESS_CHECKPOINT_EVERY_ITEMS
            if not due:
                return
            self._last_facts_progress_at = now
            self._last_facts_progress_docs_done = docs_done
        self.checkpoint_facts(stats, docs_done=docs_done, docs_total=docs_total, current_path=current_path)

    def finish(
        self,
        stats: "CrawlStats",
        *,
        status: str,
        report: Optional[Dict[str, Any]] = None,
        usage: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        if not self.run_id:
            return
        try:
            from src.repositories.extraction_runs_pg import cap_skips

            self._resolve().finish(
                self.run_id,
                status=status,
                report=report or {},
                skips=cap_skips(_skip_rows(stats), total=_skip_total(stats)),
                # Per-stage token accounting, keyed by stage: `ner` (the
                # anonymize seam's LLM detector, read via `_detector_usage`)
                # and `facts` (the LLM extraction stage, when switched on).
                # `{}` means "no tokens spent" — a different claim from
                # "$0.00", and the UI must keep saying so. An absent stage
                # spent nothing (regex tier, stage off), never $0.
                usage=usage or {},
                files_seen=stats.items_seen,
                files_done=stats.items_done,
                error=error,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("sharepoint crawl: run finalize failed (%s) — continuing", type(exc).__name__)

    def checkpoint_planning(self, folders_done: int, folders_total: int) -> None:
        """The PLANNING phase's own checkpoint (2026-09-04 finding #65 item
        3: a large site's ``compute_shard_plan`` used to run for 20+ minutes
        with no run row and no log line at all). Writes ``phase="planning"``
        and ``progress={"planning": {"folders_done", "folders_total"},
        "activity": {"phase": "planning", ...}}`` — the SAME projection
        shape ``checkpoint_facts`` layers its own ``progress["facts"]``
        onto. The ``planning`` block is read by the fleet view via the
        additive ``planning_progress`` key on ``_run_out``
        (``app/api/admin_extraction.py``); the ``activity`` block reuses the
        SAME field the source card already renders live activity from
        (``_extActivityHtml``), so "planning" shows there for free with no
        new front-end code. Wired as
        :func:`connectors.sharepoint.shard_plan.compute_shard_plan`'s own
        ``on_progress`` callback."""
        if not self.run_id:
            return
        try:
            self._resolve().checkpoint(
                self.run_id,
                phase="planning",
                files_seen=0,
                files_done=0,
                enumeration_done=False,
                progress={
                    "planning": {"folders_done": folders_done, "folders_total": folders_total},
                    "activity": {
                        "phase": "planning",
                        "current_path": f"{folders_done}/{folders_total} folders counted" if folders_total else None,
                        "current_started_at": None,
                        "recent": [],
                    },
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("sharepoint crawl: planning checkpoint failed (%s) — continuing", type(exc).__name__)

    def mark_planned(self, shards_total: int) -> None:
        """The plan is built and about to be enqueued — flips this row's
        ``phase`` to ``"plan"`` and sets ``shards_total`` (unknown at
        :meth:`start` time, since this row opens BEFORE planning runs —
        finding #65 item 3). ``shards_total`` is otherwise an INSERT-only
        column (:meth:`ExtractionRunsPgRepository.start`); this is the one
        place it is set after the fact."""
        if not self.run_id:
            return
        try:
            self._resolve().mark_planned(self.run_id, shards_total=shards_total)
        except Exception as exc:  # noqa: BLE001
            logger.debug("sharepoint crawl: mark_planned failed (%s) — continuing", type(exc).__name__)

    def finish_planning_as_inline_fallback(self, *, reason: str) -> None:
        """The planner opened this row (finding #65 item 3) but then decided
        NOT to shard — no usable signal within the search budget (finding
        #65 item 4) or a Graph/scope-resolution error mid-plan. Closes it as
        a trivial, honest ``done`` row (`report={"mode": "inline (planner
        fallback)", "reason": ...}`) rather than leaving it `running`
        forever or silently repurposing it: the actual crawl that follows
        opens its OWN fresh row exactly as it always has, so `abandon_stale_
        running`'s "one running row per connection" invariant never has to
        reason about a half-finished planning row."""
        if not self.run_id:
            return
        try:
            self._resolve().finish(
                self.run_id,
                status="done",
                report={"mode": "inline (planner fallback)", "reason": reason},
                usage={},
                skips={},
                files_seen=0,
                files_done=0,
                error=None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("sharepoint crawl: could not close the planning row (%s) — continuing", type(exc).__name__)


def _progress_snapshot(stats: "CrawlStats") -> Dict[str, Any]:
    """The ABSOLUTE counters a live run may honestly show. No fraction, no
    percentage, no ETA: ``files_per_s`` counts only new+changed documents,
    so a remaining-time figure derived from it is wrong by construction on
    any run with a meaningful `unchanged` share."""
    return {
        "files_done": stats.items_done,
        "new": stats.new,
        "changed": stats.changed,
        "unchanged": stats.unchanged,
        "renamed": stats.renamed,
        "deleted": stats.deleted,
        "downloads": stats.downloads,
        "bytes_downloaded": stats.bytes_downloaded,
        "bytes_downloaded_human": human_bytes(stats.bytes_downloaded),
        "errors": stats.errors,
        "http_429": stats.http_429,
        "throttle_wait_s": round(stats.throttle_wait_s, 1),
        "oversize_files": stats.oversize_files,
        # `extraction.crawl.min_modified` age filter — see `CrawlStats.
        # filtered_by_age`/`age_unknown` and the gate in `_process_item`.
        # Surfaced mid-run (not only in the finished `report()`) so an
        # operator watching a live crawl can tell whether the cutoff is
        # doing anything before the run finishes. Both zero when no cutoff
        # is configured.
        "filtered_by_age": stats.filtered_by_age,
        "age_unknown": stats.age_unknown,
        "elapsed_s": round(max(time.monotonic() - stats.started, 0.0), 1),
        # What the crawl is touching RIGHT NOW (owner-frustration fix,
        # 2026-09-01: "I can't see what's happening in the extraction") —
        # see `CrawlStats.activity_snapshot`. Read by the status endpoint's
        # `running` payload; absent from a finished run's stored `report`
        # (a different dict — see `CrawlStats.report`), which is the honest
        # answer for a run with nothing left in flight.
        "activity": stats.activity_snapshot(phase="crawl"),
    }


def _skip_rows(stats: "CrawlStats") -> List[Dict[str, Any]]:
    """The skips this run can name a PATH for. Only oversize skips keep
    paths (`note_oversize`); convert/anonymize/permission refusals keep
    counts alone, and are reported as counts by :func:`_skip_total` rather
    than invented as rows."""
    return [
        {
            "path": entry.get("path"),
            "reason": "oversize",
            "detail": f"{human_bytes(int(entry.get('size') or 0))} — over the size cap, never downloaded",
        }
        for entry in stats.oversize_largest
    ]


def _skip_total(stats: "CrawlStats") -> int:
    """Every document this run did NOT index, path or no path — so "20
    listed" can never be mistaken for "20 skipped"."""
    return (
        stats.oversize_files
        + stats.convert_failed
        + stats.anonymize_failed
        + stats.excluded_subtree_skips
        + stats.permission_skips
        + stats.filtered_by_age
        + stats.skipped_doomed
    )


def _retry_backlog_snapshot(state: Dict[str, Any]) -> Dict[str, Any]:
    """The STANDING view of the item-failure queue, read straight from the
    state file rather than this run's own counters — so an operator sees
    "3 documents are stuck" on every report from here on, not only on the
    run where the third one crossed the retry bound.

    ``pending`` still gets retried every run; ``given_up`` stopped being
    retried after :data:`_MAX_ITEM_RETRY_ATTEMPTS` failures and needs a
    human (fix the file, or force a ``resync`` — see :func:`_apply_resync`).

    ``doomed`` (2026-09-04 finding #66 item 2; extended by TCRD-296 gap #74 to
    a document that has repeatedly crashed or timed out the converter, not
    just one it deterministically rejects — see :func:`_doomed_classification`)
    is the STANDING count of entries :func:`_doomed_skip_reason` would skip
    on the very next run — a SUBSET of ``pending`` (a doomed item is never
    given up on; it is simply not attempted), so an operator sees "N are
    stuck AND M of those are not even being tried" rather than a single
    conflated number. ``doomed_sample``'s ``reason_type`` distinguishes a
    deterministic reject (``"doomed"``) from a repeated crash/timeout
    (``"doomed_after_repeated_<class>"``) per entry.
    """
    failed_items = state.get("failed_items") or {}
    given_up = [entry for entry in failed_items.values() if isinstance(entry, dict) and entry.get("given_up")]
    pending = len(failed_items) - len(given_up)
    doomed = [
        (entry, reason_type)
        for entry in failed_items.values()
        if isinstance(entry, dict)
        and (reason_type := _doomed_classification(entry.get("error_class"), int(entry.get("attempts", 0)))) is not None
    ]
    return {
        "pending": pending,
        "given_up": len(given_up),
        "given_up_sample": [
            {"path": entry.get("path"), "attempts": entry.get("attempts")} for entry in given_up[:_OVERSIZE_SAMPLE]
        ],
        "doomed": len(doomed),
        "doomed_sample": [
            {
                "path": entry.get("path"),
                "attempts": entry.get("attempts"),
                "error_class": entry.get("error_class"),
                "reason_type": reason_type,
            }
            for entry, reason_type in doomed[:_OVERSIZE_SAMPLE]
        ],
    }


def _record_status_for(exc: BaseException) -> str:
    """Severity-first outcome for a crawl that raised.

    A cancellation or a shutdown signal is an ``interrupted`` run — it did
    what it did and the next run resumes from the persisted cTags. Anything
    else is a ``failed`` run, and must never be softened into the benign
    outcome.
    """
    if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
        return "interrupted"
    return "failed"


#: A run that finished WITHOUT raising still owes the operator an honest
#: outcome word: fewer than this many per-file errors is a stray blip — a
#: run that otherwise found nothing new/changed to do (a healthy, idle
#: steady-state pass) must not have its status flipped by one transient
#: fault. At or above it, with zero documents actually landed, the run did
#: not accomplish anything and calling it `"done"` — the production incident
#: this guards against recorded 1263 per-file errors and 0 ingested
#: documents, reported as `done` — is the exact unverified-renders-healthy
#: failure this whole recorder exists to avoid.
_UNPRODUCTIVE_RUN_MIN_ERRORS = 5


def _ingested_nothing_despite_errors(stats: "CrawlStats") -> bool:
    """True for a run that completed (no exception) but accomplished
    nothing: at least :data:`_UNPRODUCTIVE_RUN_MIN_ERRORS` per-file errors
    and zero new/changed documents.

    Deliberately narrow. `errors` only counts a fault the crawl could not
    recover from (download/convert/ingest failure, or a whole scope it could
    not read) — the routine skip reasons (`permission_skips`,
    `excluded_subtree_skips`, `filtered_by_age`, oversize, `anonymize_failed`)
    are deliberate decisions, not failures, and never count here, so a normal run that
    politely skipped a great many documents is not mistaken for a broken
    one. `new`/`changed` are zero-checked rather than compared to `errors`
    as a ratio: with both at zero, everything this run attempted to land
    failed by construction — "erred on almost everything" needs no separate
    ratio once nothing landed at all.
    """
    return stats.errors >= _UNPRODUCTIVE_RUN_MIN_ERRORS and stats.new == 0 and stats.changed == 0


# --------------------------------------------------------------------------
# Run report
# --------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def human_bytes(n: int) -> str:
    size = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"  # pragma: no cover — the loop always returns


@dataclass
class CrawlStats:
    """What the run cost and what it refused to do. Every skip is counted:
    an unindexed document must never be invisible in the report.

    **Every counter here is mutated concurrently.** In-page item concurrency
    means several pipelines increment ``new`` / ``errors`` / ``bytes_
    downloaded`` at once, and the blocking convert/anonymize/ingest section of
    each runs on a worker THREAD (a bounded pool, see
    :func:`_run_blocking`), so "the event loop
    only switches at await points" is not the guarantee it would be for a pure
    coroutine. ``x += 1`` is a read-modify-write and loses increments under
    that; an undercounted skip is a document that silently did not get indexed,
    which is the one thing this report exists to make impossible. So all
    accumulation goes through :meth:`add` (and :meth:`note_oversize`) under
    :attr:`_lock` — never a bare ``+=`` from crawl code.
    """

    started: float = field(default_factory=time.monotonic)
    started_at: str = field(default_factory=_now_iso)
    requests: int = 0
    retries: int = 0
    http_429: int = 0
    throttle_wait_s: float = 0.0
    retry_wait_s: float = 0.0
    token_refreshes: int = 0
    delta_resyncs: int = 0
    #: Items replayed from a PRIOR run's failure queue that succeeded this
    #: time (cleared from ``failed_items``) / that hit :data:`_MAX_ITEM_
    #: RETRY_ATTEMPTS` and were given up on THIS run. Not the same axis as
    #: ``errors`` above — a fresh failure this run is counted there; these
    #: two are about the queue of PAST failures. See :func:`_note_retry`.
    item_retry_recovered: int = 0
    item_retry_given_up: int = 0
    scopes: int = 0
    drives: int = 0
    downloads: int = 0
    bytes_downloaded: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    #: A subset of what would otherwise have been counted ``unchanged`` —
    #: the item's cTag/eTag still matches (content unchanged), but its
    #: ``name``/``parentReference.path`` no longer matches the stored
    #: ``corpus_files`` row (a rename or a move within the same drive). The
    #: row's ``path``/``filename`` are updated in place, no download, no
    #: convert, no re-ingest — see ``_Ingestor.rename`` and the gate in
    #: ``_process_item``.
    renamed: int = 0
    deleted: int = 0
    errors: int = 0
    convert_failed: int = 0
    anonymize_failed: int = 0
    excluded_subtree_skips: int = 0
    permission_skips: int = 0
    #: Files skipped by the ``extraction.crawl.min_modified`` age filter
    #: (strictly before the connection's cutoff) / kept because this crawl
    #: could not determine their age at all — see `resolve_min_modified`
    #: and the gate in `_process_item`. Both zero when no cutoff is set.
    filtered_by_age: int = 0
    age_unknown: int = 0
    oversize_files: int = 0
    oversize_bytes: int = 0
    oversize_largest: List[Dict[str, Any]] = field(default_factory=list)
    #: Itemized per-file faults this run could not recover from — download,
    #: convert, or ingest — bounded the same way ``oversize_largest`` is.
    #: ``anonymize_failed`` and the routine skip reasons are deliberate
    #: decisions, not failures, and never land here; only the reasons that
    #: also bump :attr:`errors` do. See :meth:`note_error`.
    errors_detail: List[Dict[str, Any]] = field(default_factory=list)
    #: A document that could not be added to the corpus at the CONVERT
    #: stage — ``convert_failed`` (a backend was tried and failed: a
    #: markitdown/pypdfium2 exception, a native crash, a per-item timeout —
    #: the caller words all three into one ``detail`` string already) or
    #: ``convert_empty`` (converted fine, produced no text). Unlike
    #: ``errors_detail`` above, an admin can act on this list directly —
    #: it carries ``item_id``/``drive_id`` (:meth:`note_failed_item`), which
    #: is what an operator-requested retry (``retry_failed``) needs to name
    #: the exact item, and it survives past this run's ``errors_detail``
    #: sample because it is capped far larger
    #: (:data:`_FAILED_ITEMS_CAP` = 5000, not :data:`_ERROR_SAMPLE`). Never
    #: the raw ``path`` for an anonymize-marked scope's item — see
    #: :meth:`note_failed_item`.
    failed_items: List[Dict[str, Any]] = field(default_factory=list)
    #: How many :meth:`note_failed_item` calls this run made, UNCAPPED —
    #: the true count behind :attr:`failed_items`, so ``report()`` can say
    #: honestly whether the stored list is the whole story.
    _failed_items_seen: int = field(default=0, repr=False, compare=False)
    #: A file no conversion backend even attempts — video/audio containers
    #: with no usable codec path, Power BI ``.pbix``, OneNote ``.one``, and
    #: similar formats (see ``src.ingest.convert.
    #: UnsupportedConversionFormat``). Deliberately NOT an error: nothing was
    #: attempted and nothing failed, so it must never inflate ``errors`` or
    #: ``convert_failed`` the way a genuine conversion failure does — see
    #: :meth:`note_skipped_unsupported`.
    skipped_unsupported: int = 0
    #: Itemized counterpart to :attr:`skipped_unsupported`, same shape and
    #: same cap as :attr:`failed_items` — kept as a SEPARATE list (never
    #: merged into ``failed_items``) so a caller can tell "we didn't even
    #: try" from "we tried and it didn't work" without inspecting each row.
    skipped_items: List[Dict[str, Any]] = field(default_factory=list)
    #: Uncapped count behind :attr:`skipped_items`, mirroring
    #: :attr:`_failed_items_seen`.
    _skipped_items_seen: int = field(default=0, repr=False, compare=False)
    #: :attr:`skipped_unsupported`, broken down by suffix (no leading dot,
    #: lower-case; ``""`` for a file with none) — every
    #: :meth:`note_skipped_unsupported` call bumps this, whether the item was
    #: classified by :data:`_DEFAULT_UNSUPPORTED_EXTENSIONS` before any
    #: download, or discovered mid-conversion via
    #: ``UnsupportedConversionFormat``. Uncapped: the key space is bounded by
    #: distinct extensions actually seen, never by item count.
    skipped_unsupported_by_extension: Dict[str, int] = field(default_factory=dict)
    #: Documents skipped WITHOUT a download because their most recent
    #: recorded failure is deterministic and has repeated enough to trust —
    #: see :func:`_doomed_skip_reason` (2026-09-04 finding #66 item 2).
    #: Never bumps ``errors``/``convert_failed``: nothing was attempted THIS
    #: run, mirroring :attr:`skipped_unsupported`'s own "we didn't even try"
    #: accounting, just for a different reason (a format that WOULD be
    #: attempted but has already proven futile on THIS file's own bytes).
    skipped_doomed: int = 0
    #: Itemized counterpart, same shape/cap discipline as :attr:`skipped_items`.
    skipped_doomed_items: List[Dict[str, Any]] = field(default_factory=list)
    #: Uncapped count behind :attr:`skipped_doomed_items`, mirroring
    #: :attr:`_skipped_items_seen`.
    _skipped_doomed_seen: int = field(default=0, repr=False, compare=False)
    #: How many successfully-ingested documents needed the conversion rescue
    #: chain (``src.ingest.convert.ConvertResult.rescue``),
    #: broken down by which rung succeeded: ``"libreoffice_resave"``
    #: (resave-and-retry), ``"csv_fallback"``, ``"pdf_fallback"``. A
    #: document that converted cleanly on the first try never bumps this —
    #: see :meth:`note_conversion_rescue`.
    conversion_rescued: Dict[str, int] = field(default_factory=dict)
    #: Delta rows this run has ENUMERATED and rows it has FINISHED handling.
    #: Not in :meth:`report` — the report's contract is unchanged — but read
    #: by the run recorder so a live run has honest ABSOLUTE counters (a
    #: fraction would be a lie here: this crawl enumerates and processes in
    #: lockstep per 200-row delta page, so the two are equal at every
    #: checkpoint and there is no meaningful denominator to divide by).
    items_seen: int = 0
    items_done: int = 0
    #: In-page item concurrency CEILING for this run (1 = sequential): the
    #: configured value, or the per-run payload override that replaced it.
    concurrency: int = 1
    #: What ``extraction.crawler.concurrency`` alone said — kept next to the
    #: effective ceiling so a payload override is visible as an override.
    concurrency_configured: int = 1
    #: "config" | "payload" | "adaptive" — where the EFFECTIVE in-flight
    #: target came from. "adaptive" wins when throttling pulled it below the
    #: ceiling, because that is the number that actually governed the run.
    concurrency_source: str = "config"
    #: The adaptive target the run ENDED on, the lowest it ever reached, how
    #: many times it was halved, and whether it bottomed out at 1. Together
    #: these are the operator's view of the tenant pushing back — the run is
    #: never aborted by a downshift, so without them it would be invisible.
    concurrency_effective_max: int = 1
    concurrency_min_target: int = 1
    concurrency_downshifts: int = 0
    concurrency_floor_hit: bool = False
    #: Peak number of item pipelines in flight at once, observed. Read next to
    #: `concurrency` it answers the only question the knob raises: did the pool
    #: actually fill, or is something else (page size, tenant throttling) the
    #: real bound?
    max_in_flight: int = 0
    #: Live in-flight count — bookkeeping for `max_in_flight`, not reported.
    in_flight: int = 0
    #: Summed wall time spent inside the per-item pipeline across all workers.
    #: Against the run's own `duration_s` this is the honest overlap figure: a
    #: perfectly serial run has item_seconds ≈ duration_s, a run at N-way
    #: overlap approaches N × duration_s.
    item_seconds: float = 0.0
    #: Items currently being downloaded/converted/ingested, for the live
    #: checkpoint's ``activity`` block (owner-frustration fix, 2026-09-01:
    #: "I can't see what it's doing") — ``{token: (path, started_at_iso)}``.
    #: Under concurrency several of these are non-empty at once; the
    #: checkpoint picks whichever ONE is still there when it reads this dict
    #: (:meth:`activity_snapshot`) — the block exists to prove the crawl is
    #: alive, not to enumerate every worker. Paths only, NEVER file content —
    #: they are drive-relative paths already handled as ordinary (if
    #: untrusted-ish) data everywhere else in this module, and this surface
    #: is admin-only (``extraction_runs`` sits behind ``require_admin``), so
    #: no extra redaction is needed here beyond that existing gate.
    _in_flight_paths: Dict[int, Tuple[str, str]] = field(default_factory=dict, repr=False, compare=False)
    _in_flight_token: int = field(default=0, repr=False, compare=False)
    #: Last up to 5 completed items, newest first — ``{path, outcome}``. Not
    #: part of :meth:`report`'s contract (that stays unchanged); read only by
    #: :meth:`activity_snapshot`.
    recent: List[Dict[str, Any]] = field(default_factory=list, repr=False, compare=False)
    #: How many successfully-ingested files (``new + changed``) had already
    #: crossed a ``extraction.facts.stream_every`` threshold the last time
    #: :meth:`facts_stream_due` fired — bookkeeping for that knob only, not
    #: part of :meth:`report`'s contract.
    _facts_stream_last_enqueued: int = field(default=0, repr=False, compare=False)
    #: Guards every counter above. Not compared, not printed — it is machinery.
    _lock: Any = field(default_factory=threading.RLock, repr=False, compare=False)

    def add(self, **deltas: float) -> None:
        """Atomically accumulate one or more counters: ``stats.add(new=1)``.

        Unknown names raise rather than quietly minting an attribute — a typo
        here would be an invisible, permanently-zero counter in the report.
        """
        with self._lock:
            for name, delta in deltas.items():
                current = getattr(self, name)  # AttributeError on a typo, deliberately
                setattr(self, name, current + delta)

    def enter_item(self) -> None:
        """One item pipeline started — track the pool's observed peak."""
        with self._lock:
            self.in_flight += 1
            if self.in_flight > self.max_in_flight:
                self.max_in_flight = self.in_flight

    def exit_item(self, seconds: float) -> None:
        """One item pipeline finished (successfully or not)."""
        with self._lock:
            self.in_flight -= 1
            self.item_seconds += max(0.0, seconds)

    def facts_stream_due(self, stream_every: int) -> bool:
        """True at most once per ``stream_every`` newly-ingested files —
        ``new + changed`` crossing another multiple since the last time this
        returned ``True``. ``stream_every <= 0`` (the knob off) always
        returns ``False``.

        The check and the bookkeeping update happen under the same lock as
        every other counter, so two callers racing the same crossing can
        never both see ``True`` for it — in practice this is only ever
        called from one coroutine at a time (drives and pages both run
        strictly sequentially within a run, see :func:`_crawl_drive`'s
        docstring), but the lock costs nothing and keeps this field on the
        same discipline as its neighbours rather than being the one
        exception.
        """
        if stream_every <= 0:
            return False
        with self._lock:
            ingested = int(self.new + self.changed)
            if ingested - self._facts_stream_last_enqueued >= stream_every:
                self._facts_stream_last_enqueued = ingested
                return True
            return False

    def throttle_snapshot(self) -> Tuple[int, float]:
        """``(http_429, throttle_wait_s)`` read atomically together.

        The adaptive governor diffs two of these across a page; reading the
        two counters separately could straddle a concurrent update and
        manufacture a burst that never happened.
        """
        with self._lock:
            return self.http_429, self.throttle_wait_s

    def note_concurrency(self, *, target: int, downshift: bool = False) -> None:
        """Record where the adaptive in-flight target has moved to."""
        with self._lock:
            self.concurrency_effective_max = target
            self.concurrency_min_target = min(self.concurrency_min_target, target)
            if downshift:
                self.concurrency_downshifts += 1
                self.concurrency_source = "adaptive"
            if target <= 1 and self.concurrency > 1:
                self.concurrency_floor_hit = True

    def note_oversize(self, path: str, size: int) -> None:
        size = int(size or 0)
        with self._lock:
            self.oversize_files += 1
            self.oversize_bytes += size
            self.oversize_largest.append({"path": path, "size": size})
            self.oversize_largest.sort(key=lambda e: -int(e["size"]))
            del self.oversize_largest[_OVERSIZE_SAMPLE:]

    def note_error(self, path: str, reason: str, *, detail: str = "", status_code: Optional[int] = None) -> None:
        """One per-file fault, itemized — the difference between a bare
        error COUNT and a diagnosable run. Callers still call :meth:`add`
        for the ``errors``/``convert_failed`` counters themselves; this only
        appends the row a caller who can name a path is able to give.
        ``detail`` must already be a caller-composed, safe-to-log string
        (a status code and an upstream error body/exception message, never a
        token, a certificate, or file content) — this method does not scrub
        it further.
        """
        with self._lock:
            self.errors_detail.append({"path": path, "reason": reason, "detail": detail, "status_code": status_code})
            del self.errors_detail[_ERROR_SAMPLE:]

    def note_failed_item(
        self,
        *,
        path: Optional[str],
        item_id: str,
        drive_id: str,
        reason_type: str,
        reason: str,
        suffix: str,
    ) -> None:
        """One document that could not be added to the corpus at the
        convert stage — ``reason_type`` is ``"convert_failed"`` or
        ``"convert_empty"`` (see :attr:`failed_items`'s docstring).

        ``path`` must already be ``None`` for an anonymize-marked scope's
        item — the caller (:func:`_process_item`) decides that, mirroring
        the existing rule that keeps only the exception TYPE, never the
        message, for that scope's ``convert_failed`` detail (see
        :func:`_convert_failure_detail`). ``item_id``/``drive_id`` are Graph
        object identifiers, not document content, so they are kept
        regardless of ``anonymize`` — they are what a later
        ``retry_failed`` run, or an admin reading this list, needs to name
        the item. ``reason`` is truncated to 200 characters — this is a
        summary row, not the full ``errors_detail`` message.
        """
        with self._lock:
            self._failed_items_seen += 1
            self.failed_items.append(
                {
                    "path": path,
                    "item_id": item_id,
                    "drive_id": drive_id,
                    "reason_type": reason_type,
                    "reason": (reason or "")[:200],
                    "suffix": suffix,
                }
            )
            del self.failed_items[_FAILED_ITEMS_CAP:]

    def note_skipped_unsupported(
        self,
        *,
        path: Optional[str],
        item_id: str,
        drive_id: str,
        reason: str,
        suffix: str,
    ) -> None:
        """One document no conversion backend even attempted — never an
        error, never a retry candidate (see :attr:`skipped_unsupported`'s
        docstring). Same anonymize rule as :meth:`note_failed_item`: ``path``
        is ``None`` when the caller's scope is anonymize-marked."""
        with self._lock:
            self.skipped_unsupported += 1
            self._skipped_items_seen += 1
            self.skipped_items.append(
                {
                    "path": path,
                    "item_id": item_id,
                    "drive_id": drive_id,
                    "reason_type": "unsupported_type",
                    "reason": (reason or "")[:200],
                    "suffix": suffix,
                }
            )
            del self.skipped_items[_FAILED_ITEMS_CAP:]
            ext_key = (suffix or "").lower().lstrip(".")
            self.skipped_unsupported_by_extension[ext_key] = self.skipped_unsupported_by_extension.get(ext_key, 0) + 1

    def note_skipped_doomed(
        self,
        *,
        path: Optional[str],
        item_id: str,
        drive_id: str,
        reason: str,
        suffix: str,
        error_class: str,
        reason_type: str = "doomed",
    ) -> None:
        """One document skipped WITHOUT a download because its recorded
        failure history says it is doomed — see :attr:`skipped_doomed`'s
        docstring. Same anonymize rule as :meth:`note_failed_item`: ``path``
        is ``None`` when the caller's scope is anonymize-marked.

        ``reason_type`` (TCRD-296 gap #74) is ``"doomed"`` for a
        deterministic-reject skip, or ``"doomed_after_repeated_<class>"`` for
        a document that has repeatedly crashed or timed out the converter
        instead — see :func:`_doomed_classification` — so a reader of the
        run report or the fleet view can tell the two apart without parsing
        ``reason``'s free-form text.
        """
        with self._lock:
            self.skipped_doomed += 1
            self._skipped_doomed_seen += 1
            self.skipped_doomed_items.append(
                {
                    "path": path,
                    "item_id": item_id,
                    "drive_id": drive_id,
                    "reason_type": reason_type,
                    "reason": (reason or "")[:200],
                    "suffix": suffix,
                    "error_class": error_class,
                }
            )
            del self.skipped_doomed_items[_FAILED_ITEMS_CAP:]

    def note_conversion_rescue(self, rescue: str) -> None:
        """One successfully-ingested document that needed the conversion
        rescue chain — bumps :attr:`conversion_rescued`'s count for
        ``rescue`` (``"libreoffice_resave"`` / ``"csv_fallback"`` /
        ``"pdf_fallback"``). Called only for a non-empty ``rescue`` — the
        ordinary "converted cleanly, no rescue needed" case never touches
        this counter at all."""
        with self._lock:
            self.conversion_rescued[rescue] = self.conversion_rescued.get(rescue, 0) + 1

    def enter_item_activity(self, path: str) -> int:
        """One file's download/convert/ingest pipeline STARTING, for the
        live ``activity`` checkpoint block. Returns a token to pass back to
        :meth:`exit_item_activity` — under concurrency several items are
        in flight at once, and a token is what lets a fast neighbour's exit
        remove exactly ITS OWN entry rather than a slower one's."""
        with self._lock:
            self._in_flight_token += 1
            token = self._in_flight_token
            self._in_flight_paths[token] = (path, _now_iso())
        return token

    def exit_item_activity(self, token: int, path: str, outcome: str, *, rescue: str = "") -> None:
        """The pipeline for ``token`` finished (whatever the outcome) —
        drop it from the in-flight set and push it onto ``recent``.
        ``rescue`` (empty for the ordinary case) is this ITEM's own
        per-file conversion detail — which rescue-chain rung succeeded, if
        any — the live-checkpoint counterpart to the run-wide
        :attr:`conversion_rescued` total."""
        with self._lock:
            self._in_flight_paths.pop(token, None)
            self.recent.insert(0, {"path": path, "outcome": outcome, "rescue": rescue})
            del self.recent[_RECENT_ACTIVITY_SAMPLE:]

    def activity_snapshot(self, *, phase: str) -> Dict[str, Any]:
        """``{phase, current_path, current_started_at, recent}`` for the live
        checkpoint. ``current_path`` names ANY one in-flight item — under
        concurrency several are true at once, and this block exists to prove
        the crawl is alive, not to enumerate every worker."""
        with self._lock:
            current_path: Optional[str] = None
            current_started_at: Optional[str] = None
            if self._in_flight_paths:
                current_path, current_started_at = next(iter(self._in_flight_paths.values()))
            return {
                "phase": phase,
                "current_path": current_path,
                "current_started_at": current_started_at,
                "recent": list(self.recent[:_RECENT_ACTIVITY_SAMPLE]),
            }

    def report(
        self,
        *,
        max_file_mb: int,
        interrupted: bool = False,
        interrupted_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        from src.repositories.extraction_runs_pg import cap_skips

        elapsed = max(time.monotonic() - self.started, 1e-6)
        processed = self.new + self.changed
        return {
            "mode": "builtin",
            "started_at": self.started_at,
            "finished_at": _now_iso(),
            "duration_s": round(elapsed, 1),
            "interrupted": interrupted,
            # Why the run stopped early: one of `_STOP_REASONS`' values
            # ("timeout" when extraction.timeout_s expired, "throttled" when
            # the tenant's 429 budget ran out), "error" for anything else, or
            # None on a clean pass. An operator reading a short report must be
            # able to tell "this is all there was" from "this is where we ran
            # out of clock". The named reasons are exactly the stops that
            # leave consistent state on disk, so a reader may say "the next
            # run resumes" for them and must not for "error".
            "interrupted_reason": interrupted_reason,
            "scopes": self.scopes,
            "drives": self.drives,
            "new": self.new,
            "changed": self.changed,
            "unchanged": self.unchanged,
            "renamed": self.renamed,
            "deleted": self.deleted,
            "errors": self.errors,
            "convert_failed": self.convert_failed,
            "anonymize_failed": self.anonymize_failed,
            # Successfully-ingested documents that needed the conversion
            # rescue chain (`src.ingest.convert.ConvertResult.
            # rescue`), broken down by which rung succeeded — see
            # `conversion_rescued`'s docstring. Never counted in
            # `convert_failed`: a rescue that succeeded is a success.
            "conversion_rescued": dict(self.conversion_rescued),
            # Never an error — see `skipped_unsupported`'s docstring: no
            # backend was even attempted, so nothing failed.
            "skipped_unsupported": self.skipped_unsupported,
            # Same total, broken down by suffix — see
            # `skipped_unsupported_by_extension`'s docstring.
            "skipped_unsupported_by_extension": dict(self.skipped_unsupported_by_extension),
            "excluded_subtree_skips": self.excluded_subtree_skips,
            "permission_skips": self.permission_skips,
            "filtered_by_age": self.filtered_by_age,
            "age_unknown": self.age_unknown,
            "files_per_s": round(processed / elapsed, 3),
            "requests": self.requests,
            "retries": self.retries,
            "http_429": self.http_429,
            "throttle_wait_s": round(self.throttle_wait_s, 1),
            "retry_wait_s": round(self.retry_wait_s, 1),
            "token_refreshes": self.token_refreshes,
            "delta_resyncs": self.delta_resyncs,
            # The item-failure queue THIS run touched — how many prior
            # failures finally landed, and how many just crossed the retry
            # bound. `retry_backlog` (below, added by the caller once the
            # state file is final) is the standing count an operator reads
            # to know whether anything is still stuck.
            "item_retry_recovered": self.item_retry_recovered,
            "item_retry_given_up": self.item_retry_given_up,
            # What the pool was ALLOWED to do, what the tenant let it do, and
            # what it actually did. `max_in_flight` below `effective_max`
            # means something other than the knob bounded the run; a non-zero
            # `downshifts` means the tenant did, which is a fact an operator
            # has to be able to SEE rather than infer from a slow run.
            "concurrency": {
                "configured": self.concurrency_configured,
                "requested": self.concurrency,
                "source": self.concurrency_source,
                "effective_max": self.concurrency_effective_max,
                "min_target": self.concurrency_min_target,
                "downshifts": self.concurrency_downshifts,
                "floor_hit": self.concurrency_floor_hit,
            },
            "max_in_flight": self.max_in_flight,
            "item_seconds": round(self.item_seconds, 1),
            "downloads": self.downloads,
            "bytes_downloaded": self.bytes_downloaded,
            "bytes_downloaded_human": human_bytes(self.bytes_downloaded),
            "max_file_mb": max_file_mb or "unlimited",
            "skipped_oversize": {
                "files": self.oversize_files,
                "bytes": self.oversize_bytes,
                "bytes_human": human_bytes(self.oversize_bytes),
                "largest": list(self.oversize_largest),
            },
            # The itemized counterpart to the bare `errors` count above —
            # same envelope `extraction_runs.skips` uses (`items`/`listed`/
            # `total`/`truncated`), reused rather than reinvented so the two
            # "how much did we not index, and can we name it" surfaces never
            # drift on shape. `total` can exceed `listed`: a scope-level
            # fault (the caller's own `scope_errors` list) bumps `errors`
            # but has no single file to name, so it is counted here without
            # a row of its own.
            "errors_detail": cap_skips(list(self.errors_detail), total=self.errors),
            # Retry-able convert-stage failures (`failed_items`'s docstring)
            # — the surface an admin reads to see WHICH documents are
            # missing and `retry_failed` (`run_builtin_crawl`) works from.
            # A PLAIN list, not `cap_skips`'s envelope: that helper's own
            # cap (`extraction_runs_pg._SKIPS_CAP` = 200) is a different,
            # smaller number than this list's own (`_FAILED_ITEMS_CAP` =
            # 5000, already applied at `note_failed_item()` time), so
            # reusing it here would silently re-truncate down to 200.
            # `failed_items_truncated` names the same "list is shorter than
            # the true count" fact `cap_skips`'s `truncated` would, just
            # against this list's own cap.
            "failed_items": list(self.failed_items),
            "failed_items_truncated": self._failed_items_seen > len(self.failed_items),
            # Never attempted, never an error — see `skipped_unsupported`'s
            # docstring. Same shape/cap discipline as `failed_items` above.
            "skipped_items": list(self.skipped_items),
            "skipped_items_truncated": self._skipped_items_seen > len(self.skipped_items),
            # Skipped WITHOUT a download because the failure history says
            # this document is doomed — see `skipped_doomed`'s own docstring
            # (2026-09-04 finding #66 item 2). Never an error, never counted
            # in `convert_failed`: nothing was attempted THIS run.
            "skipped_doomed": self.skipped_doomed,
            "skipped_doomed_items": list(self.skipped_doomed_items),
            "skipped_doomed_items_truncated": self._skipped_doomed_seen > len(self.skipped_doomed_items),
        }


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------


async def _sleep(seconds: float) -> None:
    """The crawl's only wall-clock wait, behind one seam.

    Every backoff and every honored ``Retry-After`` goes through here, so a
    test can assert the BOUNDS of the 429/retry policy — the part that
    matters — without spending those seconds.
    """
    await asyncio.sleep(seconds)


def _require_graph_url(url: str) -> str:
    """Refuse to attach the bearer token to anything but Graph.

    ``@odata.nextLink``/``@odata.deltaLink`` are absolute URLs read out of a
    response body (and, after a resume, out of a file on disk) and replayed
    with the ``Authorization`` header — i.e. a credential-egress destination
    taken from data, exactly what the security playbook's host-allowlist rule
    covers.
    """
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != _GRAPH_HOST:
        raise CrawlError(f"refusing to send a Graph token to a non-Graph URL: {parts.scheme}://{parts.hostname}")
    return url


async def _stream_body_to_temp(resp: httpx.Response, tmp_path: Path, max_bytes: int, item_id: str) -> int:
    """Stream ``resp``'s body into ``tmp_path``, enforcing ``max_bytes`` as it
    goes. Shared by :meth:`GraphTransport.download_to_temp`'s direct-200 and
    followed-redirect-200 paths, so the size cap applies identically to
    either — a redirect must never become a way around it."""
    written = 0
    with open(tmp_path, "wb") as fh:
        async for chunk in resp.aiter_bytes(_DOWNLOAD_CHUNK):
            written += len(chunk)
            if max_bytes and written > max_bytes:
                raise CrawlError(f"download exceeded the {max_bytes}-byte cap for item {item_id}")
            fh.write(chunk)
    return written


class GraphAuth:
    """App-only token with proactive mid-crawl refresh.

    A client-credentials token lives ~1h; a full crawl outlives it several
    times over. Re-acquired once the live one is inside :data:`REFRESH_MARGIN`
    of expiry — before requests start failing, not after. The exchange itself
    is ``graph_client.get_app_token``: this class holds a token's lifetime,
    it does not reimplement the certificate-credential flow.

    **One refresh, not N.** With concurrent in-flight requests the expiry
    window is crossed by every one of them at once, and a 401 storm arrives
    at every one of them at once. Both paths therefore go through
    :attr:`_lock` and re-check under it: the first caller performs the
    exchange, the rest observe the fresh token and reuse it. Without that,
    a crawl at concurrency N burns N certificate exchanges per expiry and
    per 401 — against a tenant that is already rate-limiting it.

    The lock is an ``asyncio.Lock`` rather than a ``threading.Lock`` because
    every token holder is a coroutine on this run's single event loop; the
    worker threads this crawl uses run the convert/ingest section, which
    never touches a token.
    """

    #: Refresh 5 min early: clock skew plus requests already in flight.
    REFRESH_MARGIN = 300.0
    #: Graph's app-only tokens are ~1h; assume that when nothing says.
    _DEFAULT_TTL = 3600.0

    def __init__(
        self,
        *,
        acquire: Callable[[], Any],
        stats: CrawlStats,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._acquire = acquire
        self._stats = stats
        self._clock = clock
        self._token: Optional[str] = None
        self.expires_at = 0.0
        self._lock = asyncio.Lock()

    def _fresh(self) -> bool:
        return self._token is not None and self._clock() < self.expires_at - self.REFRESH_MARGIN

    async def token(self) -> str:
        if self._fresh():
            assert self._token is not None
            return self._token
        async with self._lock:
            # Re-checked under the lock: while this caller waited, another
            # may have done the exchange already.
            if self._fresh():
                assert self._token is not None
                return self._token
            return await self._acquire_locked()

    async def refresh(self) -> str:
        """Force an exchange (the 401 path). Callers that know WHICH token
        they saw fail should prefer :meth:`refresh_stale`, which coalesces a
        concurrent 401 storm into one exchange."""
        async with self._lock:
            return await self._acquire_locked()

    async def refresh_stale(self, observed: Optional[str]) -> str:
        """Refresh only if ``observed`` is still the live token.

        Under concurrency, N in-flight requests can each meet a 401 against
        the SAME expired token. The first one through replaces it; the others
        would otherwise each buy an identical new token (and each count a
        `token_refreshes`, overstating what the run actually did). Seeing a
        token other than the one they failed with is proof the refresh they
        needed has already happened.
        """
        async with self._lock:
            if self._token is not None and observed is not None and self._token != observed:
                return self._token
            return await self._acquire_locked()

    async def _acquire_locked(self) -> str:
        """The exchange itself. Callers hold :attr:`_lock`."""
        self._token = str(await self._acquire())
        self.expires_at = self._clock() + self._DEFAULT_TTL
        self._stats.add(token_refreshes=1)
        return self._token


class GraphTransport:
    """Every Graph call the crawl makes, under one retry policy.

    Keeping the policy here is what lets the crawl body stay a plain loop:
    the ``Authorization`` header is re-stamped per request from
    :class:`GraphAuth` (a token expiring mid-crawl is a non-event), 429 is
    honored but bounded, 5xx/transport faults retry with jittered backoff,
    401 forces exactly one refresh, and 410 raises :class:`GraphGone`
    because a dead deltaLink is a caller decision, never a retry.
    """

    def __init__(
        self,
        auth: GraphAuth,
        stats: CrawlStats,
        *,
        sleep: Optional[Callable[[float], Any]] = None,
        max_attempts: int = _MAX_ATTEMPTS,
        max_throttle_wait_s: float = _MAX_THROTTLE_WAIT_S,
        max_single_wait_s: float = _MAX_SINGLE_WAIT_S,
    ) -> None:
        self.auth = auth
        self.stats = stats
        self._sleep_fn = sleep
        self.max_attempts = max(1, int(max_attempts))
        self.max_throttle_wait_s = float(max_throttle_wait_s)
        self.max_single_wait_s = float(max_single_wait_s)

    def _backoff_seconds(self, attempt: int) -> float:
        return min(_BACKOFF_BASE_S * (2 ** (attempt - 1)), self.max_single_wait_s) + random.uniform(0, 1)

    def _retry_after(self, response: httpx.Response, attempt: int) -> float:
        raw = response.headers.get("Retry-After")
        try:
            wait = float(raw) if raw is not None else self._backoff_seconds(attempt)
        except (TypeError, ValueError):
            wait = self._backoff_seconds(attempt)  # an HTTP-date, or garbage
        return max(0.0, min(wait, self.max_single_wait_s))

    async def _authorized(self) -> Tuple[Dict[str, str], str]:
        """``(headers, token)``. The token is returned as well as stamped so
        a 401 can name WHICH token failed — :meth:`GraphAuth.refresh_stale`
        needs that to coalesce a concurrent 401 storm into one exchange."""
        token = await self.auth.token()
        return {"Authorization": f"Bearer {token}"}, token

    async def _wait(self, seconds: float) -> None:
        """The crawl's ONLY wall-clock wait, resolved at call time through
        the module-level :func:`_sleep` seam — so a test can make a bounded
        429/backoff policy assertion without actually sleeping through it."""
        await (self._sleep_fn or _sleep)(seconds)

    async def get_json(self, url: str) -> Dict[str, Any]:
        """One GET returning a JSON object, under the full retry policy."""
        _require_graph_url(url)
        attempt = 0
        throttle_wait = 0.0
        refreshed = False
        used_token: Optional[str] = None
        while True:
            attempt += 1
            self.stats.add(requests=1)
            try:
                headers, used_token = await self._authorized()
                async with graph_client._http_client() as client:
                    resp = await client.get(url, headers=headers, timeout=_REQUEST_TIMEOUT_S)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt >= self.max_attempts:
                    raise CrawlError(f"graph GET failed after {attempt} attempts: {type(exc).__name__}") from exc
                await self._sleep_backoff(attempt, type(exc).__name__)
                continue

            action, wait = self._classify(resp, attempt, throttle_wait, refreshed)
            if action == "ok":
                body = resp.json()
                return body if isinstance(body, dict) else {"value": body}
            if action == "gone":
                raise GraphGone(url)
            if action == "throttled":
                raise GraphThrottled(f"429 budget exhausted after {attempt} attempts / {throttle_wait:.0f}s of waiting")
            if action == "refresh":
                refreshed = True
                self.stats.add(retries=1)
                logger.info("sharepoint crawl: 401 — forcing a token refresh")
                await self.auth.refresh_stale(used_token)
                continue
            if action == "throttle":
                throttle_wait += wait
                self.stats.add(http_429=1, throttle_wait_s=wait, retries=1)
                logger.info("sharepoint crawl: 429 — pausing %.0fs (attempt %d)", wait, attempt)
                await self._wait(wait)
                continue
            if action == "retry":
                await self._sleep_backoff(attempt, f"HTTP {resp.status_code}")
                continue
            # The existing typed Graph error, not a bare CrawlError: it carries
            # the status, which is how the caller tells a routine permissions
            # fact (403/404 on one drive of a many-drive site) from a broken
            # connection — the same distinction `graph_client.search_folders`
            # already draws.
            raise SharePointGraphError(f"graph GET failed: HTTP {resp.status_code}", status_code=resp.status_code)

    def _classify(self, resp: httpx.Response, attempt: int, throttle_wait: float, refreshed: bool) -> Tuple[str, float]:
        """``(action, wait)`` for one response — the whole status policy in
        one place, so :meth:`get_json` and :meth:`download_to_temp` cannot
        drift on what a 429 or a 410 means."""
        status = resp.status_code
        if 200 <= status < 300:
            return "ok", 0.0
        if status == 429:
            wait = self._retry_after(resp, attempt)
            if attempt >= self.max_attempts or throttle_wait + wait > self.max_throttle_wait_s:
                self.stats.add(http_429=1)
                return "throttled", 0.0
            return "throttle", wait
        if status == 410:
            return "gone", 0.0
        if status == 401 and not refreshed and attempt < self.max_attempts:
            return "refresh", 0.0
        if status in _RETRY_STATUS and attempt < self.max_attempts:
            return "retry", 0.0
        return "fail", 0.0

    async def _sleep_backoff(self, attempt: int, why: str) -> None:
        wait = self._backoff_seconds(attempt)
        self.stats.add(retries=1, retry_wait_s=wait)
        logger.info("sharepoint crawl: %s — retry %d/%d in %.1fs", why, attempt, self.max_attempts, wait)
        await self._wait(wait)

    async def download_to_temp(self, drive_id: str, item_id: str, name: str, *, max_bytes: int) -> Path:
        """Stream one item's bytes to a temp file and return its path.

        Streaming — never buffering the body — is what makes an unlimited
        size cap safe: a 4 GB file costs a temp file, not 4 GB of RSS. The
        caller ALWAYS deletes the returned path; on any failure in here the
        partial file is removed before the exception propagates, so a local
        copy never outlives the attempt that made it.

        ``max_bytes`` is a second, independent guard on top of the caller's
        ``size``-based skip: Graph's reported ``size`` is metadata, and a
        response that disagrees with it must not be able to fill the disk.

        **The 302.** Graph answers ``GET .../content`` with a redirect to a
        pre-authenticated URL on a DIFFERENT host (blob storage, never
        ``graph.microsoft.com``) rather than the bytes themselves. httpx does
        not follow redirects by default, and turning that on
        (``follow_redirects=True``) would not be safe here even so: httpx
        replays the ``Authorization`` header across a redirect, which would
        hand the Graph bearer token to that other host. So a 3xx with a
        ``Location`` is followed by hand, with a SEPARATE, unauthenticated
        request — never through :func:`_require_graph_url`, deliberately,
        since that guard exists to keep the bearer token off a non-Graph
        host, and this second request carries no token to protect. The size
        cap and the temp-file cleanup guarantee apply identically to
        whichever response actually carried the bytes.
        """
        url = _require_graph_url(f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}/content")
        fd, tmp_name = tempfile.mkstemp(suffix=Path(name or "").suffix)
        os.close(fd)
        tmp_path = Path(tmp_name)
        attempt = 0
        throttle_wait = 0.0
        refreshed = False
        used_token: Optional[str] = None
        try:
            while True:
                attempt += 1
                self.stats.add(requests=1)
                try:
                    headers, used_token = await self._authorized()
                    async with graph_client._http_client() as client:
                        redirect_location: Optional[str] = None
                        async with client.stream("GET", url, headers=headers, timeout=_REQUEST_TIMEOUT_S) as resp:
                            if 300 <= resp.status_code < 400:
                                redirect_location = resp.headers.get("Location")
                            if redirect_location is None:
                                action, wait = self._classify(resp, attempt, throttle_wait, refreshed)
                                if action == "ok":
                                    written = await _stream_body_to_temp(resp, tmp_path, max_bytes, item_id)
                                    self.stats.add(downloads=1, bytes_downloaded=written)
                                    return tmp_path
                                await resp.aread()  # drain before deciding, so the connection is reusable
                            else:
                                await resp.aread()  # drain the redirect body before following it

                        if redirect_location is not None:
                            # No Authorization header on this one — see the docstring.
                            async with client.stream("GET", redirect_location, timeout=_REQUEST_TIMEOUT_S) as resp:
                                action, wait = self._classify(resp, attempt, throttle_wait, refreshed)
                                if action == "ok":
                                    written = await _stream_body_to_temp(resp, tmp_path, max_bytes, item_id)
                                    self.stats.add(downloads=1, bytes_downloaded=written)
                                    return tmp_path
                                await resp.aread()
                except (httpx.TransportError, httpx.TimeoutException) as exc:
                    if attempt >= self.max_attempts:
                        raise CrawlError(f"download failed after {attempt} attempts: {type(exc).__name__}") from exc
                    await self._sleep_backoff(attempt, type(exc).__name__)
                    continue

                if action == "gone":
                    raise GraphGone(url)
                if action == "throttled":
                    raise GraphThrottled(f"429 budget exhausted downloading item {item_id}")
                if action == "refresh":
                    refreshed = True
                    self.stats.add(retries=1)
                    await self.auth.refresh_stale(used_token)
                    continue
                if action == "throttle":
                    throttle_wait += wait
                    self.stats.add(http_429=1, throttle_wait_s=wait, retries=1)
                    await self._wait(wait)
                    continue
                if action == "retry":
                    await self._sleep_backoff(attempt, "download HTTP error")
                    continue
                raise SharePointGraphError(
                    f"download of item {item_id} failed: HTTP {resp.status_code}", status_code=resp.status_code
                )
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise


# --------------------------------------------------------------------------
# Convert / anonymize seams
#
# Both modules are written independently of this one. They are imported
# LAZILY through these two wrappers so (a) this module imports cleanly on an
# instance where they are absent, and (b) a test has exactly one attribute to
# substitute instead of patching an import machinery.
# --------------------------------------------------------------------------


def convert_to_markdown(path: Path, mime: str, *, source_path: Optional[str] = None) -> Any:
    """``src.ingest.convert.convert_to_markdown`` — the seam.

    ``source_path`` (the document's ORIGINAL drive-relative path, as opposed
    to ``path``, the local temp file) rides through to the scan-OCR triage
    stage-0 path rules — see that module's own docstring. Optional and
    keyword-only so every existing caller (real or a test double) that never
    passes it keeps working unchanged.

    Returns that module's ``ConvertResult`` (``.markdown``, ``.engine``).
    An ``ImportError`` propagates: with no converter there is nothing to
    ingest, so the run fails clean rather than silently indexing nothing.
    """
    from src.ingest.convert import convert_to_markdown as _convert

    return _convert(path, mime, source_path=source_path)


def anonymize_markdown(text: str, *, key: bytes, detector: Any = None) -> Any:
    """``src.anonymization.anonymize_markdown`` — the seam.

    Returns that module's ``AnonymizeResult`` (``.text``, ``.replaced``).
    ``detector`` is ``None`` for the deterministic regex tier (the module's
    own default) or the hybrid regex+LLM detector built by
    :func:`_entity_detector`. Callers treat ANY failure here — ``ImportError``
    and the LLM tier's ``DetectionUnavailable`` included — as "this document
    cannot be anonymized", which for an anonymize-marked scope means it is
    skipped, never ingested raw.
    """
    from src.anonymization import anonymize_markdown as _anonymize

    return _anonymize(text, key=key, detector=detector)


# --------------------------------------------------------------------------
# Scope -> drives
# --------------------------------------------------------------------------


class _Deadline:
    """The run's wall-clock bound (``extraction.timeout_s``).

    Checked at the two points where stopping is free — between files and
    between delta pages — rather than interrupting mid-download, because the
    resume guarantee depends on state being written at exactly those
    boundaries. ``timeout_s <= 0`` means unbounded.
    """

    def __init__(self, timeout_s: float, *, clock: Optional[Callable[[], float]] = None) -> None:
        # Resolved through the module attribute at call time, not bound as a
        # default argument at import time — the same reason every other seam
        # in this module defers: a test must be able to substitute it.
        self._clock = clock or (lambda: time.monotonic())
        self.timeout_s = float(timeout_s or 0)
        self.expires_at: Optional[float] = self._clock() + self.timeout_s if self.timeout_s > 0 else None

    def expired(self) -> bool:
        return self.expires_at is not None and self._clock() >= self.expires_at

    def check(self) -> None:
        """Raise :class:`CrawlTimeout` if the budget is spent."""
        if self.expired():
            raise CrawlTimeout(f"extraction.timeout_s ({self.timeout_s:.0f}s) elapsed — stopping; the next run resumes")


@dataclass(frozen=True)
class DriveTarget:
    """One delta-enumerable unit of work: a drive, optionally rooted at a
    folder inside it (a folder scope), plus the state key its deltaLink is
    stored under."""

    drive_id: str
    drive_name: Optional[str]
    #: ``None`` for a whole drive; a folder item id for a folder scope.
    root_item_id: Optional[str] = None
    #: This target's own drive-relative path prefix — ``""`` when
    #: ``root_item_id`` is ``None`` (the target IS the drive root), else
    #: the folder scope's own path (``acl_sync.scope_rel_root``'s output).
    #: Consumed only by the shard planner (``connectors.sharepoint.
    #: shard_plan.compute_shard_plan``), to build a folder-scoped
    #: remainder's ``exclude_prefixes`` as full drive-relative paths — the
    #: same shape ``_drive_relative_path`` computes for an item at crawl
    #: time — instead of paths relative to this target's own root, which
    #: would silently fail to match and let the remainder re-walk its own
    #: already-packed siblings.
    root_path: str = ""

    @property
    def state_key(self) -> str:
        return f"{self.drive_id}:{self.root_item_id}" if self.root_item_id else self.drive_id

    @property
    def delta_url(self) -> str:
        if self.root_item_id:
            return f"{GRAPH_BASE}/drives/{self.drive_id}/items/{self.root_item_id}/delta"
        return f"{GRAPH_BASE}/drives/{self.drive_id}/root/delta"


def _scope_kind(source_scope_id: str) -> str:
    """Site / drive / folder, decided STRUCTURALLY from the Graph id shape —
    it must work for scope rows confirmed before this module existed, with
    no Graph round trip."""
    if "," in source_scope_id:
        return "site"
    if source_scope_id.startswith("b!"):
        return "drive"
    return "folder"


async def _drive_targets(transport: GraphTransport, scope: Dict[str, Any]) -> List[DriveTarget]:
    """Resolve one confirmed scope row to the drives to delta-enumerate."""
    source_scope_id = str(scope.get("source_scope_id") or "")
    kind = _scope_kind(source_scope_id)
    if kind == "drive":
        return [DriveTarget(drive_id=source_scope_id, drive_name=scope.get("display_path"))]
    if kind == "folder":
        drive_id = scope.get("drive_id")
        if not drive_id:
            # Same posture as `acl_sync._sync_scope`: a folder scope without a
            # persisted drive_id is a wizard gap, reported per-scope rather
            # than crashing the connection's whole run.
            raise CrawlError(f"folder scope {source_scope_id!r} has no drive_id on its scope row")
        return [
            DriveTarget(
                drive_id=str(drive_id),
                drive_name=scope.get("display_path"),
                root_item_id=source_scope_id,
                root_path=scope_rel_root(scope.get("display_path") or ""),
            )
        ]

    body = await transport.get_json(f"{GRAPH_BASE}/sites/{source_scope_id}/drives?$select=id,name")
    return [
        DriveTarget(drive_id=drive["id"], drive_name=drive.get("name"))
        for drive in body.get("value", [])
        if drive.get("id")
    ]


@dataclass(frozen=True)
class _ExclusionIndex:
    """One scope's ``excluded_subtrees`` entries, split by the TWO different
    match rules a ``kind`` demands (2026-08-31 sweep v2 / TCRD-284):

    * ``kind=="folder"`` (or a legacy entry with no ``kind`` at all — the
      exclusion semantics every entry had before kinds existed) excludes
      everything AT OR UNDER its path — component-safe prefix, same rule
      :func:`_under_prefix` already applied.
    * ``kind=="file"`` excludes EXACTLY that one item — matched by
      drive-relative path AND by its stable id, never as a prefix, so a
      sibling whose name merely starts with the excluded file's name is
      never swept up with it (the bug a bare prefix test would have had).

    Empty by default — a scope with nothing excluded, or
    ``include_excluded_subtrees``, gets one with no members and every match
    below is trivially false.
    """

    folder_prefixes: Tuple[str, ...] = ()
    file_paths: "frozenset[str]" = frozenset()
    file_ids: "frozenset[str]" = frozenset()


def _excluded_file(path: str, stable_id: str, index: _ExclusionIndex) -> bool:
    """Whether one crawled item is a ``kind=="file"`` exclusion — checked by
    stable id (the entry's own ``item_id``, the identifier this crawler
    already keys everything on) OR by exact drive-relative path, per the
    module's matching semantics. Never a prefix test: that is
    :func:`_under_prefix`'s job, reserved for folder-kind entries."""
    return stable_id in index.file_ids or (bool(path) and path in index.file_paths)


async def _excluded_path_prefixes(transport: GraphTransport, scope: Dict[str, Any]) -> _ExclusionIndex:
    """This scope's exclusions, resolved to drive-relative paths and split
    into folder-prefix vs. file-exact matches (see :class:`_ExclusionIndex`).

    Fast path (TCRD-284): an entry already carrying ``rel_path`` — every
    entry the sweep has written since the rel-path/kind fields shipped — is
    used AS-IS, no Graph call. A legacy entry (pre those fields, no
    ``rel_path``) falls back to resolving its ``item_id`` against Graph
    exactly as before, and is treated as a folder prefix (it predates
    ``kind`` too).

    Fail-closed: a legacy entry whose Graph resolution fails raises, and the
    caller skips the whole scope. Crawling a scope whose exclusions could not
    be applied would ingest exactly the content an admin excluded.

    A scope carrying ``include_excluded_subtrees`` is exempt — the admin
    decided that audience may see the content — matching the same override
    ``app/api/admin_sharepoint.py::confirm_scope`` writes (spec §6.3's
    ``should_not`` per-subtree override).
    """
    if scope.get("include_excluded_subtrees"):
        return _ExclusionIndex()
    excluded = scope.get("excluded_subtrees")
    if not isinstance(excluded, list) or not excluded:
        return _ExclusionIndex()
    drive_id = scope.get("drive_id")

    folder_prefixes: List[str] = []
    file_paths: set = set()
    file_ids: set = set()

    for entry in excluded:
        if not isinstance(entry, dict) or not entry.get("item_id"):
            continue
        item_id = str(entry["item_id"])
        rel_path = entry.get("rel_path")

        if not rel_path:
            if not drive_id:
                raise CrawlError(
                    f"scope {scope.get('source_scope_id')!r} has excluded subtrees but no drive_id — "
                    "cannot resolve them to paths, refusing to crawl it"
                )
            body = await transport.get_json(
                f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}?$select=id,name,parentReference"
            )
            rel_path = _drive_relative_path(body.get("parentReference") or {}, str(body.get("name") or ""))
        rel_path = str(rel_path)
        if not rel_path:
            continue

        if entry.get("kind") == "file":
            file_paths.add(rel_path)
            file_ids.add(f"graph:{item_id}")
        else:
            folder_prefixes.append(rel_path)

    return _ExclusionIndex(
        folder_prefixes=tuple(folder_prefixes), file_paths=frozenset(file_paths), file_ids=frozenset(file_ids)
    )


def _exclusion_index_with_extra_prefixes(index: _ExclusionIndex, extra: Sequence[str]) -> _ExclusionIndex:
    """A copy of ``index`` with ``extra`` folder-prefix exclusions merged in
    on top of whatever the scope's own ``excluded_subtrees`` already
    excludes — the remainder-shard mechanism (2026-09-03 auto-parallel-crawl
    design §4.1 point 3): a remainder shard's ``exclude_prefixes`` (every
    folder path a sibling shard already owns) must be respected the SAME way
    an ordinary ``kind=="folder"`` exclusion is (:func:`_under_prefix`,
    applied in :func:`_process_item`), never a second, separate check.
    ``extra`` empty (every caller before sharding existed) returns ``index``
    unchanged, not a copy."""
    if not extra:
        return index
    return _ExclusionIndex(
        folder_prefixes=tuple(index.folder_prefixes) + tuple(extra),
        file_paths=index.file_paths,
        file_ids=index.file_ids,
    )


# --------------------------------------------------------------------------
# Item handling
# --------------------------------------------------------------------------


def _should_skip_name(name: str) -> bool:
    return name in _SKIP_NAMES or name.startswith(_SKIP_PREFIXES)


def _drive_relative_path(parent_reference: Dict[str, Any], name: str) -> str:
    """``parentReference.path`` + item name, with Graph's
    ``/drives/<id>/root:`` prefix stripped — the DRIVE-relative path the
    zone router and the collections ``path`` key both speak."""
    parent = str(parent_reference.get("path") or "")
    rel = _DRIVE_ROOT_PREFIX_RE.sub("", parent).strip("/")
    return f"{rel}/{name}".strip("/") if rel else name.strip("/")


def _under_prefix(path: str, prefixes: Sequence[str]) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


#: Sibling of ``connectors.sharepoint.facts_extraction``'s ``retry_mode``
#: override and :data:`STOP_REQUESTED_AT_KEY` above: another per-connection
#: extraction lever living on ``connection.config.extraction.<leaf>``,
#: carried forward on every generic connection edit. Unlike ``retry_mode``
#: this one has no instance-level fallback — a "changed on/after this date"
#: cutoff is inherently connection-specific (a fresh site vs. a decade-old
#: archive), so there is nothing sensible to fall back TO; absent or
#: invalid simply means "no filter", i.e. the crawl behaves exactly as it
#: always has. Kept as the DEFAULT once a scope may carry its own override
#: (see :data:`SCOPE_MIN_MODIFIED_KEY` below, TCRD-296 gap #80) — a scope
#: that never sets one still gets exactly this behavior.
MIN_MODIFIED_KEY = "min_modified"

#: The per-SCOPE age filter (TCRD-296 gap #80 — "the modified-since filter
#: belongs to the scope, not the connection's extraction-config drawer"):
#: ``scope["min_modified"]``, an ISO ``YYYY-MM-DD`` string or absent/``None``.
#: A scope that does not set this falls back to the connection-wide
#: :data:`MIN_MODIFIED_KEY` default — the old, only-ever-possible behavior,
#: pinned by a test so existing instances keep crawling exactly as they did
#: before this field existed.
SCOPE_MIN_MODIFIED_KEY = "min_modified"


def resolve_min_modified(
    connection: Optional[Dict[str, Any]] = None,
    *,
    scope: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[date], str]:
    """``(cutoff, source)`` for the effective age filter — a SCOPE's own
    override first, the connection-wide default second, absent means no
    filter.

    ``scope["min_modified"]`` (an ISO ``YYYY-MM-DD`` string) wins when
    present and valid — ``source: "scope"``. Otherwise falls back to
    ``connection.config.extraction.crawl.min_modified`` — ``source:
    "connection"`` when that is set and valid. ``source: "none"`` when
    neither is set, OR when whichever ONE the callsite supplied is present
    but not a parseable ISO date (logged and ignored — never a crash: a
    malformed override must not abort a crawl, it just runs unfiltered,
    exactly as if nothing were set at all). ``scope=None`` (every pre-TCRD-296
    callsite that only cares about the connection-wide default, e.g. the
    ``…/extraction/crawl-config`` drawer endpoint) skips the scope check
    entirely and behaves byte-for-byte as before this parameter existed.
    """
    if scope:
        raw_scope = scope.get(SCOPE_MIN_MODIFIED_KEY)
        if isinstance(raw_scope, str) and raw_scope.strip():
            candidate = raw_scope.strip()
            try:
                return date.fromisoformat(candidate), "scope"
            except ValueError:
                logger.warning(
                    "sharepoint crawl: scope %s min_modified=%r is not an ISO YYYY-MM-DD date — "
                    "ignoring, falling back to the connection default",
                    scope.get("source_scope_id"),
                    raw_scope,
                )
    if connection:
        raw = (((connection.get("config") or {}).get("extraction") or {}).get("crawl") or {}).get(MIN_MODIFIED_KEY)
        if isinstance(raw, str) and raw.strip():
            candidate = raw.strip()
            try:
                return date.fromisoformat(candidate), "connection"
            except ValueError:
                logger.warning(
                    "sharepoint crawl: connection %s config.extraction.crawl.min_modified=%r is not an "
                    "ISO YYYY-MM-DD date — ignoring, crawl runs unfiltered",
                    connection.get("id"),
                    raw,
                )
    return None, "none"


#: Sibling of :data:`MIN_MODIFIED_KEY` above — another per-connection crawl
#: lever living on ``connection.config.extraction.crawl.<leaf>`` (D.16, "keep
#: a site current without an operator"). Unlike ``min_modified`` this one
#: DOES have a sensible instance-level fallback (:data:`CRAWL_SCHEDULE_
#: INSTANCE`, the default): most connections should just follow the one
#: instance-wide sweep cadence (``extraction.schedule``), and only a site
#: with its own change-tempo needs an override.
CRAWL_SCHEDULE_KEY = "schedule"
#: Never picked up by the scheduled sweep, regardless of the instance-wide
#: cadence — the connection is crawled only via a manual trigger.
CRAWL_SCHEDULE_OFF = "off"
#: The default: follow the instance-wide ``extraction.schedule`` cadence,
#: exactly as every connection did before this per-connection override
#: existed.
CRAWL_SCHEDULE_INSTANCE = "instance"


def is_valid_crawl_schedule(value: Any) -> bool:
    """True for :data:`CRAWL_SCHEDULE_OFF`, :data:`CRAWL_SCHEDULE_INSTANCE`,
    or any cadence string :func:`src.scheduler.is_valid_schedule` itself
    accepts (``"every Nm"``/``"every Nh"``, ``"daily HH:MM[,HH:MM,...]"``,
    ``"cron <5-field expr>"``) — the SAME grammar ``extraction.schedule``
    already uses instance-wide, so this codebase has exactly one cadence
    syntax, not two. Anything else (``None``, empty, malformed) is False.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    candidate = value.strip()
    if candidate in (CRAWL_SCHEDULE_OFF, CRAWL_SCHEDULE_INSTANCE):
        return True
    from src.scheduler import is_valid_schedule

    return is_valid_schedule(candidate)


def resolve_crawl_schedule(connection: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
    """``(schedule, source)`` for this connection's own crawl cadence —
    ``connection.config.extraction.crawl.schedule``, defaulting to
    :data:`CRAWL_SCHEDULE_INSTANCE` (follow the instance-wide
    ``extraction.schedule`` sweep) when absent or invalid.

    ``source`` is ``"connection"`` for a valid stored override, ``"default"``
    when absent OR present but not a value :func:`is_valid_crawl_schedule`
    accepts (logged and ignored — a malformed override must not abort the
    sweep's due-check, it just falls back to the instance cadence, exactly
    as if nothing were set at all).
    """
    if connection:
        raw = (((connection.get("config") or {}).get("extraction") or {}).get("crawl") or {}).get(CRAWL_SCHEDULE_KEY)
        if isinstance(raw, str) and raw.strip():
            candidate = raw.strip()
            if is_valid_crawl_schedule(candidate):
                return candidate, "connection"
            logger.warning(
                "sharepoint crawl: connection %s config.extraction.crawl.schedule=%r is not a valid "
                "cadence — ignoring, following the instance-wide schedule",
                connection.get("id"),
                raw,
            )
    return CRAWL_SCHEDULE_INSTANCE, "default"


def _item_modified_at(item: Dict[str, Any]) -> Optional[datetime]:
    """This item's last-modified timestamp for the ``min_modified`` gate —
    Graph's own ``lastModifiedDateTime`` first, the ``fileSystemInfo``
    mirror (present when the tenant's sync client stamped a local mtime)
    second. ``None`` when neither is present or parseable — the gate below
    treats that as "keep it": an item whose age cannot be determined must
    never be silently dropped.

    Graph writes ``...Z``; normalized to ``...+00:00`` the same way
    :func:`connectors.sharepoint.subscriptions._parse_iso` does, so both
    modules read the identical timestamp shape identically.
    """
    raw = item.get("lastModifiedDateTime") or (item.get("fileSystemInfo") or {}).get("lastModifiedDateTime")
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _zone_routes_for_scope(connection: Dict[str, Any], source_scope_id: str) -> Dict[str, List[Tuple[str, str]]]:
    """This scope's ACTIVE permission zones (2026-08-31 plan, Task 3/4;
    TCRD-284 wires them into the builtin crawler's own routing), grouped by
    drive and sorted DEEPEST-``rel_path``-first — so :func:`_route_collection`
    can return the first match and get "nested zones: deepest wins" for
    free. A dissolved zone is never in here (:func:`active_zone_rows`
    already filtered it out), so its content falls straight through to the
    scope's own collection — the exact re-homing-on-dissolve behavior the
    sweep's own retroactive cleanup (``acl_sync._cleanup_connection_content``)
    already assumes.

    Keyed by ``zone["drive_id"]``, not the scope row's own (frequently
    absent, and meaningless for a site scope that fans out to many drives):
    a zone always carries the same drive id its root folder lives on
    (``acl_sync._reconcile_zones`` copies it straight from the parent scope
    at zone-creation time) — the same drive a :class:`DriveTarget` crawls —
    so matching on ``target.drive_id`` at lookup time is the correct join
    key regardless of the scope's own kind.
    """
    by_drive: Dict[str, List[Tuple[str, str]]] = {}
    for zone in active_zone_rows(connection):
        if zone.get("parent_scope_id") != source_scope_id:
            continue
        drive_id = zone.get("drive_id")
        rel_path = zone.get("rel_path")
        collection_id = zone.get("collection_id")
        if not drive_id or not rel_path or not collection_id:
            continue
        by_drive.setdefault(str(drive_id), []).append((str(rel_path), str(collection_id)))
    for routes in by_drive.values():
        routes.sort(key=lambda pair: -len(pair[0]))
    return by_drive


@dataclass
class _ScopeContext:
    """Everything the per-item pipeline needs about the scope it is in."""

    source_scope_id: str
    collection_id: str
    anonymize: bool
    exclusions: _ExclusionIndex
    zone_routes_by_drive: Dict[str, List[Tuple[str, str]]] = field(default_factory=dict)
    #: This SCOPE's resolved :func:`resolve_min_modified` cutoff (its own
    #: override, or the connection default it falls back to), or ``None``
    #: for no filter at all (TCRD-296 gap #80). Resolved once per scope by
    #: the caller — every per-item pipeline reads it straight off here.
    min_modified: Optional[date] = None
    #: ``stable_id -> cTag`` seed from the CONNECTION-level ``crawl`` state
    #: row, consulted read-only when a shard's own per-delta-unit state row
    #: has no entry yet (2026-09-03 auto-parallel-crawl design §4.2): sharding
    #: an already-crawled drive must re-enumerate it (a shard's own state row
    #: starts empty) without re-downloading every file that has not actually
    #: changed. Empty for the inline (unsharded) path, where the single
    #: connection-wide state dict already IS ``ctags`` and this would be
    #: redundant. Never written to — only :func:`_process_item`'s "already
    #: seen" check reads it, and only the ACTIVE state's own ``ctags`` is
    #: ever updated on success.
    legacy_ctags: Mapping[str, str] = field(default_factory=dict)

    def candidate_collection_ids(self, drive_id: str) -> List[str]:
        """This scope's own collection, then every zone collection this
        drive routes to (deepest first) — the set a DELETED item (which
        carries no ``parentReference`` to route by path) might have been
        ingested into, so a deletion can find it wherever it actually
        landed."""
        ids = [self.collection_id]
        ids.extend(collection_id for _prefix, collection_id in self.zone_routes_by_drive.get(drive_id, ()))
        return ids


def _route_collection(path: str, drive_id: str, ctx: _ScopeContext) -> str:
    """The collection one crawled file lands in (TCRD-284): the DEEPEST
    active permission zone whose ``rel_path`` contains ``path``
    (component-safe — ``path == prefix or path.startswith(prefix + "/")``),
    else the scope's own collection. ``ctx.zone_routes_by_drive`` is
    pre-sorted deepest-prefix-first, so the first match wins.

    Agreement with :mod:`connectors.sharepoint.ingest_gate` is load-bearing,
    not incidental: that module refuses, server-side, any document whose
    path falls under an ACTIVE zone but landed in a DIFFERENT collection
    than that zone's own (``source_acl_zone_mismatch``) — this function is
    what keeps a correctly-routed crawl from EVER tripping it.
    """
    for prefix, collection_id in ctx.zone_routes_by_drive.get(drive_id, ()):
        if path == prefix or path.startswith(prefix + "/"):
            return collection_id
    return ctx.collection_id


class _Ingestor:
    """Writes a converted document into a collection through the SAME
    internal path a wizard upload takes.

    ``app.api.collections._upsert_corpus_file`` is the whole point: it
    matches on ``(collection, source_stable_id)`` FIRST and ``(collection,
    path)`` second, preserves the row id on any match, purges derived data
    only when content actually changed, and leaves an unchanged, already
    indexed row alone. That is what makes a re-crawl (and a resume) idempotent
    without this module knowing anything about chunks, claims, or blobs.
    """

    def __init__(self) -> None:
        # Resolved ONCE, up front: the mapping table is Postgres-only, so a
        # DuckDB-backed instance fails before a single file is downloaded
        # rather than partway through a crawl — the same posture
        # `upload_files` takes for a source-anchored batch.
        from src.repositories import corpus_file_sources_repo

        self._sources_repo = corpus_file_sources_repo()

    def ingest(
        self,
        *,
        collection_id: str,
        stable_id: str,
        path: str,
        filename: str,
        markdown: str,
        source_sha256: str,
    ) -> Tuple[str, bool]:
        """Store + upsert + (re)ingest one converted document.

        Returns ``(file_id, was_new)``.

        Raises when ``ingest_file`` itself marks the row ``rejected`` (a
        chunking/storage-layer failure on already-converted text — e.g. a
        PostgreSQL ``text`` column refusing a stray control character, live
        finding 2026-09: 261 documents). ``ingest_file`` catches its own
        failures and never raises, which is correct for the ``corpus_files``
        row (an admin reading Collections must see WHY it is rejected), but
        this method staying equally silent would strand the document: the
        CALLER (``_process_item``) persists this item's cTag unconditionally
        right after ``ingest()`` returns, and Graph's delta feed only
        re-offers an item once it CHANGES upstream — so a rejected document
        would never come back around on a normal re-crawl, forever, with
        nothing wrong with the SOURCE file at all. Raising here instead
        routes it through ``_process_item``'s existing ``ingest_failed``
        handling: :func:`_note_retry` queues it for every future run
        regardless of what delta reports, exactly like a download or convert
        failure, until it either succeeds or exhausts
        :data:`_MAX_ITEM_RETRY_ATTEMPTS`.
        """
        from app.api.collections import _upsert_corpus_file
        from src.file_storage import store_corpus_bytes

        data = markdown.encode("utf-8")
        stored = store_corpus_bytes(collection_id, filename, data)
        existed = self._sources_repo.resolve(collection_id, stable_id) is not None
        file_id, needs_processing, _claims_purged = _upsert_corpus_file(
            collection_id,
            path=path,
            stable_id=stable_id,
            # The content hash of the ORIGINAL source bytes, not of the
            # markdown — `corpus_files.sha256` already holds the latter.
            source_doc_id=source_sha256[:16],
            source_sha256_meta=source_sha256,
            filename=filename,
            sha256=stored.sha256,
            file_type=stored.ext.lstrip(".") or None,
            size_bytes=stored.size_bytes,
            storage_path=stored.storage_path,
            sources_repo=self._sources_repo,
        )
        if needs_processing:
            from src.ingest.runner import ingest_file

            # Inline, not a background task: this runs inside the worker's
            # own EXTRACTION lane slot, which is exactly where chunking is
            # supposed to happen, and finishing each document before fetching
            # the next keeps the crawl's memory flat. `preloaded_text=markdown`
            # skips `ingest_file`'s own disk re-read of the SAME content
            # `store_corpus_bytes` just wrote — a redundant full-size copy of
            # the converted markdown, on a parent thread, that this crawl's
            # own memory-pressure finding named as a contributor.
            status = ingest_file(file_id, preloaded_text=markdown)
            if status == "rejected":
                from src.repositories import corpus_files_repo

                row = corpus_files_repo().get(file_id)
                reason = ((row or {}).get("processing_detail") or {}).get("reason", "unknown reason")
                raise RuntimeError(f"ingest_file rejected {filename}: {reason}")
        return file_id, not existed

    def rename(self, *, collection_id: str, stable_id: str, path: str, filename: str) -> bool:
        """A rename/move whose CONTENT is unchanged — the caller
        (:func:`_process_item`) already proved that via the cTag/eTag
        equality gate before calling this. Updates ONLY ``corpus_files.
        path``/``filename`` for the row this stable id already resolves to
        (:meth:`CorpusFilesRepository.update_path`) — no download, no
        convert, no re-ingest, no chunk/claim churn.

        Returns ``True`` iff a row was found AND its stored path/filename
        actually differed (the caller counts this as ``renamed``).
        ``False`` — never raises — when this stable id has no resolved row
        yet (a crawl-state/corpus inconsistency a future content change
        self-heals) or the stored path/filename already match (nothing to
        do); the caller then counts the item ``unchanged``, exactly as it
        would have before this rename gate existed.
        """
        file_id = self._sources_repo.resolve(collection_id, stable_id)
        if not file_id:
            return False
        from src.repositories import corpus_files_repo

        repo = corpus_files_repo()
        row = repo.get(file_id)
        if row is None:
            return False
        if row.get("path") == path and row.get("filename") == filename:
            return False
        repo.update_path(file_id, path=path, filename=filename)
        return True

    def delete(self, collection_id: str, stable_id: str) -> bool:
        """Remove the file a deleted source item anchors, if any.

        Same two steps ``DELETE /api/collections/{id}/files/{file_id}`` takes
        — purge the row (blobs, chunks, derived tables, bundle children) and
        then sweep fact-graph subjects the cascade left with zero claims —
        so a document deleted at the source leaves nothing behind that a
        hand delete would have cleaned up.
        """
        from app.api.collections import _purge_file_row, _sweep_facts_orphans_after_delete
        from src.repositories import corpus_files_repo

        file_id = self._sources_repo.resolve(collection_id, stable_id)
        if not file_id:
            return False
        row = corpus_files_repo().get(file_id)
        if row is None:
            return False
        _purge_file_row(collection_id, row)
        _sweep_facts_orphans_after_delete(trigger="sharepoint_crawl_delete")
        return True


#: Ingest-step transient-DB retry policy (TCRD-296 C.11 — live finding,
#: 2026-09: a pool-starved worker raised ``sqlalchemy.exc.TimeoutError:
#: QueuePool limit ...`` from :meth:`_Ingestor.ingest`, and the worker's own
#: exception path finalizes a raised error on the FIRST attempt by design
#: (``app/worker/runtime.py::_run_one``) — so a 30-second connection-pool
#: hiccup turned a 4-hour crawl into one ``failed`` run). Deliberately
#: shorter than :class:`GraphTransport`'s own retry policy above: a DB
#: hiccup resolves in seconds, not the minutes a throttled tenant can take.
_INGEST_RETRY_ATTEMPTS = 5
_INGEST_RETRY_BASE_S = 1.0
_INGEST_RETRY_MAX_S = 16.0


def _ingest_retry_sleep(seconds: float) -> None:
    """The ingest-retry helper's only wall-clock wait, behind one seam — a
    test can monkeypatch this to assert the retry COUNT without spending
    the seconds. Synchronous (unlike the transport's own :func:`_sleep`):
    :func:`_ingest_with_retry` always runs OFF the event loop (see
    :func:`_run_blocking` — inline at concurrency 1, on the worker pool
    otherwise), so a blocking ``time.sleep`` here never stalls anything
    else this crawl is doing.
    """
    time.sleep(seconds)


def _ingest_with_retry(ingestor: "_Ingestor", **kwargs: Any) -> Tuple[str, bool]:
    """Call ``ingestor.ingest(**kwargs)`` with bounded retry for a
    TRANSIENT infrastructure fault only (TCRD-296 C.11 — see
    :data:`_INGEST_RETRY_ATTEMPTS`'s docstring for the live finding).

    :func:`src.db_transient.is_transient_db_error` is the closed set worth
    retrying — a SQLAlchemy pool-wait timeout, a dropped/reset connection, a
    deadlock, or a serialization failure. Never for
    ``IntegrityError``/``DataError``/a programming error: those are a bad
    row or a bad statement, and retrying either only delays a failure that
    will never resolve itself.

    A non-transient failure raises IMMEDIATELY (attempt 1, no retry) — the
    exact pre-existing behavior. A transient one that survives every retry
    also raises, once :data:`_INGEST_RETRY_ATTEMPTS` is exhausted — the
    caller (:func:`_process_item`) still records it as ``ingest_failed`` in
    ``failed_items`` and the crawl continues (module docstring's "A
    per-item failure never advances past itself"); this function only ever
    turns a SHORT infrastructure hiccup into an in-process wait instead of
    losing the item to that same handling.
    """
    from src.db_transient import is_transient_db_error

    attempt = 0
    while True:
        attempt += 1
        try:
            return ingestor.ingest(**kwargs)
        except Exception as exc:  # noqa: BLE001 — reclassified immediately below
            if attempt >= _INGEST_RETRY_ATTEMPTS or not is_transient_db_error(exc):
                raise
            wait = min(_INGEST_RETRY_BASE_S * (2 ** (attempt - 1)), _INGEST_RETRY_MAX_S) + random.uniform(0, 1)
            logger.warning(
                "sharepoint crawl: transient ingest error (%s) — retry %d/%d in %.1fs",
                type(exc).__name__,
                attempt,
                _INGEST_RETRY_ATTEMPTS,
                wait,
            )
            _ingest_retry_sleep(wait)


# --------------------------------------------------------------------------
# The crawl
# --------------------------------------------------------------------------


def _max_file_bytes(max_file_mb: int) -> int:
    """0 (or negative) means unlimited — downloads stream to a temp file."""
    return max(0, int(max_file_mb)) * 1024 * 1024


async def _run_blocking(pool: Optional[ThreadPoolExecutor], fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a BLOCKING step, on ``pool`` when the run is parallel.

    Convert, anonymize, hash and ingest are synchronous CPU/DB work. On the
    event loop they stall every other item's download for their whole
    duration, which would make "concurrency: 6" buy almost nothing — the six
    downloads would queue behind one markitdown call. Off the loop they
    overlap with downloads and with each other.

    The pool is passed in rather than taken from ``asyncio.to_thread``'s
    default executor on purpose: that one is sized ``min(32, cpu+4)``, so on
    a 4-core host a configured concurrency above 8 would silently not
    happen — the knob would stop meaning what it says.

    ``pool=None`` (concurrency 1) makes the call INLINE, so the sequential
    path is exactly the pre-parallel one: same call, same thread, same
    ordering, no executor in the picture at all.
    """
    if pool is None:
        return fn(*args, **kwargs)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(pool, functools.partial(fn, *args, **kwargs))


# --------------------------------------------------------------------------
# Conversion process isolation
#
# A native crash inside a conversion backend (observed on a live deployment:
# a `trap int3` abort inside libpdfium.so, reached via pypdfium2) takes down
# the WHOLE interpreter — no Python exception is raised, so the per-file
# `except Exception` around `convert_to_markdown` below is unreachable by
# construction. `ThreadPoolExecutor` cannot help: a fatal signal kills the
# process the thread runs in, worker thread and all. Only an OS PROCESS
# boundary survives that, which is what this section builds: one dedicated,
# reused child process per concurrency slot, talking to the parent over a
# duplex `Pipe`.
# --------------------------------------------------------------------------


@dataclass
class _ConvertOutcome:
    """One conversion attempt's result, as returned by
    :meth:`_ConvertProcessPool.convert`.

    ``detail_type`` (``type(exc).__name__``) is always safe to log and
    persist. ``detail_message`` (``str(exc)``) may quote a fragment of the
    document the child just read, so whether it is safe to KEEP is a
    per-scope decision — an anonymize-marked scope's whole premise is that
    document content never reaches storage in readable form — made by
    :func:`_prepare_document`, which already owns the anonymize decision,
    not by anything upstream of it.

    ``rescue`` (only meaningful when ``ok``) mirrors ``connectors.sharepoint.
    convert.ConvertResult.rescue`` — which rung of the conversion rescue
    chain succeeded, or ``""`` when none was needed. Never document content
    (it is one of a fixed, small vocabulary of rung names), so unlike
    ``detail_message`` it needs no anonymize-scope gating.

    ``error_class`` (only meaningful when NOT ``ok``) mirrors
    ``src.ingest.convert.ConversionError.error_class`` — one of
    that module's ``ERROR_CLASS_*`` constants, or ``""`` when the exception
    this child caught had no opinion (a bare ``Exception`` the worker never
    taught to classify). Same "fixed, small vocabulary" reasoning as
    ``rescue``: never document content, never scope-gated.
    """

    ok: bool
    markdown: str = ""
    detail_type: str = ""
    detail_message: str = ""
    rescue: str = ""
    error_class: str = ""


@dataclass
class _ConvertReply:
    """The actual wire message :func:`_convert_worker_main` sends back — a
    superset of :class:`_ConvertOutcome` carrying ``rss_bytes`` (this
    worker's own peak RSS right after the attempt, success or failure).
    :meth:`_ConvertProcessPool.convert` reads ``rss_bytes`` to decide
    whether to recycle the slot (see the class docstring's "RECYCLING"
    section) and then discards it — callers outside this module's
    recycling logic see only the plain :class:`_ConvertOutcome`, which has
    no business carrying a process-internal metric.
    """

    outcome: _ConvertOutcome
    rss_bytes: int = 0


class _ConvertCrashed(Exception):
    """The child process handling this call died from a signal (or exited
    non-zero without ever answering) instead of returning a result.

    Raised only inside :meth:`_ConvertProcessPool.convert`, on the PARENT
    side — never crosses a process boundary itself. The caller
    (:func:`_prepare_document`) treats it exactly like an ordinary
    conversion exception: the file is counted as ``convert_failed`` and the
    crawl moves on. ``signal_name`` is the best identification available
    (a POSIX signal name, ``"exit code N"``, or ``"unknown"`` when the
    worker was already gone before this call) — never document content, so
    it is always safe to keep and log regardless of scope.
    """

    def __init__(self, signal_name: str) -> None:
        self.signal_name = signal_name
        super().__init__(f"conversion worker terminated ({signal_name})")


class _ConvertTimedOut(Exception):
    """The child process handling this call did not answer within the
    per-item time bound (``timeout_s`` — see :data:`_DEFAULT_ITEM_TIMEOUT_S`)
    and was killed.

    Raised only inside :meth:`_ConvertProcessPool.convert`, on the PARENT
    side, after the stuck worker has already been reclaimed (SIGKILL, then
    the pre-forked spare promoted when one is ready — see
    :meth:`_ConvertProcessPool._reclaim_timed_out_slot`). Deliberately a
    SEPARATE type from :class:`_ConvertCrashed`, worded differently by the
    caller (:func:`_prepare_document`): this worker did not crash, it was
    still alive and simply too slow, so calling it a "crash" would misname
    the failure for an operator reading the run report. Treated the same
    way in every other respect — the file is counted as ``convert_failed``
    and the crawl moves on.
    """

    def __init__(self, timeout_s: float) -> None:
        self.timeout_s = timeout_s
        super().__init__(f"conversion worker did not answer within {timeout_s:.0f}s")


class _ConvertMemoryGuard(Exception):
    """The child process handling this call was SIGKILLed by the PARENT
    itself for crossing ``convert_child_max_rss_mb`` (see
    :data:`_DEFAULT_CONVERT_CHILD_MAX_RSS_MB`) — a live RSS ceiling the
    parent polls for, independent of the child's OWN ``RLIMIT_AS``.

    Raised only inside :meth:`_ConvertProcessPool.convert`, on the PARENT
    side, after the offending worker has already been reclaimed (SIGKILL,
    then the pre-forked spare promoted when one is ready — see
    :meth:`_ConvertProcessPool._reclaim_timed_out_slot`, reused unchanged
    for this guard: the recovery is identical to a per-item timeout, only
    the trigger differs). Deliberately a SEPARATE type from
    :class:`_ConvertCrashed`: a bare SIGKILL discovered via ``EOFError`` on
    the pipe could have come from OUTSIDE this process's own accounting
    (the kernel's OOM killer, a host-level watchdog) and may not be this
    file's fault (see :func:`_convert_crash_detail`'s external-pressure
    wording) — but THIS SIGKILL was issued by this pool itself, for a
    reason it can name with certainty, so the caller
    (:func:`_prepare_document`) words it as attributable, not as
    "may not be this file's fault".
    """

    def __init__(self, rss_bytes: int, limit_bytes: int) -> None:
        self.rss_bytes = rss_bytes
        self.limit_bytes = limit_bytes
        rss_mb = rss_bytes / (1024 * 1024)
        limit_mb = limit_bytes / (1024 * 1024)
        super().__init__(
            f"conversion worker grew {rss_mb:.0f} MB RSS during one conversion, past the {limit_mb:.0f} MB guard, and was killed"
        )


def _peak_rss_bytes() -> int:
    """This (calling) process's peak resident-set size, in bytes.

    ``resource.ru_maxrss`` is kilobytes on Linux — this module's deployment
    target, and where the memory growth this guards against was observed —
    but bytes on macOS/BSD, where this repo's tests run; normalized here
    once so every caller gets bytes regardless of platform. A HIGH-WATER
    MARK, not current usage, on purpose: it never drops back down even if
    the child frees memory afterward, which is the right signal for "has
    this worker EVER shown itself to be a memory hog" — a transient dip
    must not reset the recycle clock.
    """
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak * 1024 if sys.platform == "linux" else peak


def _own_vsize_bytes() -> int:
    """This (calling) process's OWN current virtual memory size, in bytes —
    read from ``/proc/self/status``'s ``VmSize`` line (kB). Best-effort and
    silent: any read/parse failure, or ``/proc`` simply not existing
    (notably macOS, where this repo's own tests run), returns ``0`` — the
    caller's "unavailable" sentinel — never raises.

    Called from two places for two different reasons that both need the
    SAME number. Inside a freshly forked conversion child, right after
    fork and before any allocation of its own, this IS the PARENT's VmSize
    at the moment of fork: virtual address space is COPIED across
    ``fork()``, not re-measured, and nothing has grown it yet — see
    :func:`_install_memory_limit`. And in the parent, immediately before
    :meth:`_ConvertProcessPool.start` forks anything, it is a close
    estimate of what every child about to be forked will inherit — good
    enough for the one INFO line reporting the ceiling an operator will
    actually see enforced.
    """
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmSize:"):
                    return int(line.split()[1]) * 1024  # kB -> bytes
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _child_rss_bytes(pid: Optional[int]) -> Optional[int]:
    """``pid``'s current resident-set size (``VmRSS``), in bytes, read from
    ``/proc/<pid>/status`` — the PARENT-side counterpart to
    :func:`_peak_rss_bytes` (which only ever reads its OWN
    ``/proc/self/status``, from inside the process being measured). Used
    by :meth:`_ConvertProcessPool._await_reply` to watch a CHILD's live RSS
    while a conversion is in flight — see :data:`_DEFAULT_CONVERT_CHILD_MAX_RSS_MB`.

    Returns ``None`` — never raises — on any failure: an absent/vanished
    ``pid``, no ``/proc`` at all (notably macOS, where this repo's own
    tests run), or a malformed line. A platform without ``/proc``, or a
    child that exited between the pool's own liveness check and this read,
    can therefore only turn one poll of the RSS watchdog into a no-op,
    never a broken crawl.
    """
    if not pid:
        return None
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024  # kB -> bytes
    except (OSError, ValueError, IndexError):
        return None
    return None


def _effective_memory_limit_bytes(limit_bytes: int) -> int:
    """``limit_bytes`` interpreted as HEADROOM above this process's own
    current VmSize, not an absolute ceiling — see
    :func:`_install_memory_limit` for the live-deployment finding that made
    this necessary. Falls back to ``limit_bytes`` alone (the pre-fix
    behaviour) when this process's own VmSize cannot be read (non-Linux,
    or a malformed/inaccessible ``/proc/self/status``).
    """
    own_vsize = _own_vsize_bytes()
    return own_vsize + limit_bytes if own_vsize > 0 else limit_bytes


def _install_memory_limit(limit_bytes: int) -> None:
    """Cap THIS (child) process's own virtual address space at
    ``limit_bytes`` HEADROOM above its own inherited VmSize — see
    :data:`_DEFAULT_CONVERT_CHILD_MEMORY_LIMIT_MB` for why a ceiling exists
    at all. Call ONCE, right after fork, before the first document — the
    ceiling applies for the rest of this process's life.

    ``RLIMIT_AS`` (not ``RLIMIT_DATA``, which modern glibc's ``mmap``-backed
    large-allocation path bypasses entirely past its threshold, and not
    ``RLIMIT_RSS``, a pure no-op on Linux since kernel 2.6.9) is the one
    resource limit that reliably turns "this process is about to blow
    through its budget" into a plain Python ``MemoryError`` at the
    allocation that crosses it — caught by :func:`_convert_worker_main`'s
    own ``except Exception``, exactly like any other conversion failure,
    ATTRIBUTED to the file whose conversion was in progress.

    ``limit_bytes`` used to be installed as an ABSOLUTE ceiling. Live-
    deployment finding (2026-09, a 64-vCPU extraction worker): the forking
    worker process's OWN VmSize was already ~2.2 GB — mostly interpreter
    and library address space reserved at import time, not physically
    resident, and scaling with host CPU count via numpy/OpenBLAS thread
    buffers — well past the 1536 MB default before a single document was
    ever converted. Every forked child inherits that same footprint at
    fork, so it was born already "over" an absolute 1536 MB ceiling, and
    ``import markitdown``/``import pypdfium2`` failed immediately with a
    ``MemoryError`` that (before :class:`MissingConversionDependency`
    learned to carry its cause) read as "not installed". ``limit_bytes`` is
    now HEADROOM above whatever THIS process's own VmSize turns out to be
    at fork time (:func:`_effective_memory_limit_bytes`), never an
    absolute number guessed independently of it.

    Best-effort and silent: ``RLIMIT_AS`` is not settable on every
    platform — notably macOS, where this repo's own tests run, refuses to
    lower it at all — so a platform that cannot install this safety net
    still converts, rather than refusing to start. Linux (this module's
    deployment target, and where the memory pressure this guards against
    was observed) enforces it reliably. 0 disables the cap outright.
    """
    if limit_bytes <= 0:
        return
    effective = _effective_memory_limit_bytes(limit_bytes)
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (effective, effective))
    except (ValueError, OSError, AttributeError):
        pass


#: Signals a conversion child must answer with the DEFAULT action (die), not
#: with whatever Python-level handler it inherited across ``fork`` from the
#: process that forked it. Live-deployment finding (2026-09, a 64-vCPU
#: extraction worker running 7 crawls): the worker runs under uvicorn, which
#: installs a Python handler for SIGTERM/SIGINT that merely flags the server
#: loop to exit — a no-op in a forked child that never runs that loop. Every
#: child inherited it, so :meth:`_ConvertProcessPool._close_slot`'s
#: ``terminate()`` (SIGTERM) on a recycled or crashed slot was IGNORED, and
#: the ``join(timeout=5)`` after it simply expired. The retiree then sat in
#: ``conn.recv()`` forever: its pipe's parent end was closed by the parent,
#: but every SIBLING forked after it still held an inherited copy of that
#: fd, so no EOF ever arrived. Each recycle leaked one ~0.8 GB process; the
#: worker grew ~10 GB/min and was OOM-killed (200 GB) within the hour.
_CHILD_DEFAULT_SIGNALS = (signal.SIGTERM, signal.SIGINT)


def _reset_inherited_signal_handlers() -> None:
    """Restore the DEFAULT disposition of :data:`_CHILD_DEFAULT_SIGNALS` in
    THIS (child) process — call once, right after fork, before anything
    else. See :data:`_CHILD_DEFAULT_SIGNALS` for the live finding this
    answers. Best-effort: a platform or thread that cannot set a handler
    (``ValueError`` outside the main thread, ``OSError``) still converts —
    :meth:`_ConvertProcessPool._close_slot`'s SIGKILL escalation is the
    guarantee, this is what makes the polite path work at all."""
    for sig in _CHILD_DEFAULT_SIGNALS:
        try:
            signal.signal(sig, signal.SIG_DFL)
        except (ValueError, OSError):
            pass


def _retire_process(proc: Any, *, grace_s: float = 5.0) -> None:
    """Stop ``proc`` and REAP it, whatever it thinks about SIGTERM.

    ``terminate()`` (SIGTERM) first, so a cooperative child can exit
    cleanly; if it is still alive after ``grace_s`` — a child that
    inherited a signal handler across fork (see
    :data:`_CHILD_DEFAULT_SIGNALS`), or one wedged in native code — escalate
    to ``kill()`` (SIGKILL, which nothing in user space can ignore) and
    join again. Never returns with a live process it was asked to retire,
    and never leaves a zombie: both joins reap.
    """
    if proc is None or not proc.is_alive():
        return
    proc.terminate()
    proc.join(timeout=grace_s)
    if proc.is_alive():
        proc.kill()
        proc.join(timeout=grace_s)


def _convert_worker_main(conn: Connection, memory_limit_bytes: int = 0, max_output_bytes: int = 0) -> None:
    """Entry point for a dedicated conversion child process — runs ONLY
    inside a forked child, never called directly.

    Installs this worker's own memory ceiling (see
    :func:`_install_memory_limit`) once, then loops reading
    ``(tmp_path_str, mime, source_path)`` off ``conn`` and replying with a
    :class:`_ConvertReply`. An ordinary Python exception from
    :func:`convert_to_markdown` — including a ``MemoryError`` from hitting
    that ceiling — is caught HERE, exactly like the pre-isolation code did,
    and turned into the same kind of failure — both ``type(exc).__name__``
    and ``str(exc)`` cross back (see :class:`_ConvertOutcome` for why
    sending both is safe: what to DO with the message is the parent's
    scope-aware decision, not this function's). A native crash — or a
    SIGKILL from memory pressure OUTSIDE this process's own control, the
    one case the memory ceiling above cannot turn into an ordinary
    exception, because the kernel does not ask first — bypasses this
    function's `try/except` entirely by definition; the parent notices this
    worker is gone via the pipe closing (``EOFError`` on its next
    ``recv``), not via anything sent from here.

    A conversion that SUCCEEDS but produces markdown over
    ``max_output_bytes`` (see :data:`_DEFAULT_MAX_CONVERTED_MB`) is turned
    into the same kind of ``ok=False`` reply as an ordinary exception —
    ``detail_type="ConvertedTooLarge"`` — built WITHOUT the oversized
    ``markdown`` ever touching a :class:`_ConvertOutcome`, so it never
    crosses ``conn.send`` at all.
    """
    _reset_inherited_signal_handlers()
    _install_memory_limit(memory_limit_bytes)
    # What THIS child has added on top of what it inherited: ``ru_maxrss``
    # (and every other RSS figure) of a forked child starts at the parent's
    # own resident set — 10+ GB on a crawl parent that has run for hours —
    # so reporting the raw peak made every slot look "over the recycle
    # ceiling" after its very first document, burn its spares within a few
    # documents and then run unbounded for the rest of the page (live
    # finding, 2026-09). The pool's recycle decision only ever wants the
    # growth, so that is what every reply carries.
    start_peak = _peak_rss_bytes()

    def _growth() -> int:
        return max(0, _peak_rss_bytes() - start_peak)

    while True:
        try:
            task = conn.recv()
        except (EOFError, OSError):
            return
        if task is None:  # shutdown sentinel
            return
        tmp_path_str, mime, source_path = task
        try:
            converted = convert_to_markdown(Path(tmp_path_str), mime, source_path=source_path)
            markdown = str(getattr(converted, "markdown", "") or "")
            rescue = str(getattr(converted, "rescue", "") or "")
        except Exception as exc:  # noqa: BLE001 — this file's failure, not the worker's
            outcome = _ConvertOutcome(
                ok=False,
                detail_type=type(exc).__name__,
                detail_message=str(exc),
                error_class=getattr(exc, "error_class", None) or "",
            )
            try:
                conn.send(_ConvertReply(outcome=outcome, rss_bytes=_growth()))
            except OSError:
                return
            continue
        if max_output_bytes > 0:
            size = len(markdown.encode("utf-8"))
            if size > max_output_bytes:
                outcome = _ConvertOutcome(
                    ok=False,
                    detail_type="ConvertedTooLarge",
                    detail_message=(
                        f"converted output ({human_bytes(size)}) exceeds the {human_bytes(max_output_bytes)} cap"
                    ),
                )
                try:
                    conn.send(_ConvertReply(outcome=outcome, rss_bytes=_growth()))
                except OSError:
                    return
                continue
        try:
            conn.send(
                _ConvertReply(outcome=_ConvertOutcome(ok=True, markdown=markdown, rescue=rescue), rss_bytes=_growth())
            )
        except OSError:
            return


class _ConvertProcessPool:
    """A small, reused pool of persistent worker PROCESSES dedicated to the
    convert step of one crawl run.

    One process per concurrency slot (``0..size-1``), forked once (see
    :meth:`start`) and reused — spawning a fresh interpreter per file would
    pay a full cold start (plus re-importing markitdown/pypdfium2) on every
    single document, which for a thousand-file crawl dwarfs the conversion
    itself. Passing the temp file's PATH rather than its bytes keeps each
    round trip to two short strings.

    Deliberately ``fork``, never ``spawn``:

    * ``fork`` is what makes a test's ``monkeypatch.setattr(crawler,
      "convert_to_markdown", ...)`` reach the child at all. ``spawn`` starts
      a brand new interpreter that re-imports this module fresh and never
      sees a patch applied to the ALREADY-RUNNING parent's copy; ``fork``
      copies the parent's memory as it stood at fork time, patch included,
      which is also just a few ms instead of a few hundred.
    * Every worker's ``Process`` and its duplex ``Connection`` are 1:1 — no
      shared queue, no ambiguity about which process died: a slot's own
      crash is detected by ``EOFError`` on ITS OWN connection (the OS always
      closes the write end when a process exits, whatever the cause), and
      its exact ``exitcode`` (negative == killed by that signal number) is
      read directly off THAT ``Process`` object. That is deliberately NOT
      ``concurrent.futures.ProcessPoolExecutor``: its only public failure is
      a generic ``BrokenProcessPool`` with no per-worker detail, and a crash
      there poisons the ENTIRE pool rather than the one slot that died.

    Every FORK this pool ever does — the initial :meth:`start` and every
    :meth:`repair` — must happen from a point the CALLER has proven is
    single-threaded (the top of a run, or a delta-page boundary once that
    page's item-concurrency ``ThreadPoolExecutor`` has been joined).
    ``fork()`` while another thread holds a C-level lock (malloc, DuckDB,
    OpenSSL, ...) can hand the child a lock that will never be released —
    this pool trusts its caller for that timing rather than re-deriving it.

    RECYCLING (owner-reported, live-deployment finding, 2026-09-01): crash
    isolation alone turns "one bad file kills the worker" into "the worker
    SURVIVES but converts nothing else" — markitdown/pypdfium2 hold onto
    memory per document, so a slot that never dies just keeps running, and
    its RSS climbs without bound over a large crawl until the container's
    memory cgroup starts SIGKILLing whichever child allocates next,
    INDISCRIMINATELY (see :data:`_DEFAULT_CONVERT_RECYCLE_AFTER_DOCS`). A
    slot's process is therefore replaced after ``recycle_after_docs``
    documents or once its peak RSS crosses ``recycle_rss_bytes``, whichever
    comes first.

    The same single-threaded-fork constraint applies to a RECYCLE as to any
    other fork, but a recycle is decided inside :meth:`convert` itself —
    the one method that runs on a worker THREAD, mid-page, with siblings
    still active, i.e. never at a safe point. Three ways to reconcile that
    were weighed:

    1. Recycle only at the next :meth:`repair` (a genuine safe point).
       Rejected: a default page is up to 200 items, so a slot could convert
       4-5x its budget before a page boundary ever arrives — exactly the
       "the budget is meaningless past 200" gap this exists to close.
    2. Use ``spawn`` for a recycle's replacement only — ``spawn`` needs no
       single-threaded window at all, since it starts a brand new
       interpreter rather than forking this one, so it sidesteps the
       constraint entirely. Rejected: a ``spawn``-started replacement
       re-imports this module fresh in the new interpreter, so it would
       stop seeing a test's ``monkeypatch.setattr(crawler,
       "convert_to_markdown", ...)`` the moment a slot recycles mid-test —
       the exact problem that made this class choose ``fork`` in the first
       place — silently diverging from every OTHER worker in the pool and
       from itself before its own first recycle.
    3. **Chosen: pre-fork a SPARE per slot from a safe point, swap it in
       when the budget is hit.** :meth:`start` forks both the ACTIVE worker
       and an idle SPARE for every slot; :meth:`repair` (a safe point,
       called after every page) tops up any slot whose spare was consumed.
       The swap itself — retire the active, promote the spare — does no
       ``fork()`` at all, only a termination signal to the retiree and a
       pointer reassignment, so it is safe from ANY thread, including a
       worker thread mid-page. The spare is forked through the same
       ``fork`` context as everything else, so it inherits the SAME
       monkeypatched state a test applied before the run started, keeping
       option 2's failure mode off the table. Cost: double the idle process
       count versus options 1/2 — acceptable, since an idle (never-yet-used)
       spare's own memory footprint is just import overhead, not yet the
       per-document accumulation this whole mechanism exists to bound.

    A slot with no spare ready when its budget is hit (it already consumed
    every spare this page, before the last :meth:`repair` had a chance to
    refill them) simply keeps running past its budget until the next safe
    point — bounded staleness, never unbounded growth, and never a
    correctness issue: :meth:`convert` still returns every file's real
    outcome either way.

    The SAME swap primitive also repairs a mid-page CRASH instantly when a
    spare happens to be ready, rather than leaving the slot down for the
    rest of the page (the pre-recycling behavior, still exactly what
    happens when no spare is available). The crashed file's own outcome is
    unaffected either way — still counted ``convert_failed``, still logged
    with its signal — only whether a DIFFERENT, later file on the same slot
    in the same page has to wait for the next page boundary changes.

    N SPARES PER SLOT (second live-deployment finding, 2026-09):  a single
    spare per slot is exactly ONE mid-page recycle/crash of insurance — a
    slot that hits its budget or crashes a SECOND time in the same page,
    before the next :meth:`repair` refills it, had nothing left to swap in
    and fell back to the pre-spares behaviour (unbounded RSS growth for a
    recycle, a dead slot for a crash) for the rest of that page. ``spares``
    is now a per-slot QUEUE of ``spares_per_slot`` (default
    :data:`_DEFAULT_CONVERT_SPARES_PER_SLOT`) pre-forked standbys, consumed
    FIFO by :meth:`_swap_in_spare` and topped back up to the configured
    count by :meth:`repair` — same fork sites, same single-threaded
    constraint, just more of them. This does not raise the ceiling on how
    MANY times a slot can fail in one page before it runs dry again, it
    only raises it from one to ``spares_per_slot`` — the cost is one more
    idle (import-only) process per slot per unit of insurance, so it is a
    knob, not a fixed multiplier.

    Does ``repair`` run MORE OFTEN than once per (up to 200-item) delta
    page to shrink that residual gap further? No additional safe point
    exists in the CONCURRENT branch (``concurrency > 1``) without either
    forking from a live worker thread (the exact hazard this whole
    constraint exists to avoid) or splitting one page into several
    thread-pool lifetimes (defeating the overlap the page's own
    concurrency exists to provide) — :func:`_process_page`'s
    ``ThreadPoolExecutor`` is created once and joined once per page, by
    design, and the join is the ONLY point between its creation and the
    caller's next page fetch where no worker thread is alive. The
    SEQUENTIAL branch (``concurrency == 1``) and :func:`_retry_failed_items`
    both already call :meth:`repair` before EVERY item, since neither ever
    holds a thread pool at all — narrowing the concurrent branch's cadence
    below "once per page" is therefore not available cheaply; widening the
    spare queue is the lever this module actually has.

    Every worker this pool ever forks — active or spare — also gets its own
    ``RLIMIT_AS`` ceiling (``memory_limit_bytes``, installed inside the
    child by :func:`_install_memory_limit`; see
    :data:`_DEFAULT_CONVERT_CHILD_MEMORY_LIMIT_MB` for the full reasoning),
    so a single pathological document raises an ATTRIBUTABLE
    ``MemoryError`` for the file that caused it instead of pressuring the
    whole container and getting an arbitrary sibling SIGKILLed. A SEPARATE
    ceiling, ``max_output_bytes`` (see :data:`_DEFAULT_MAX_CONVERTED_MB`),
    bounds the CONVERTED TEXT a worker is allowed to send back across the
    pipe — the two are independent: a document can convert well within its
    own RLIMIT_AS the whole time and still produce more markdown than the
    parent should ever be handed at once.

    RSS WATCHDOG (third live-deployment finding, 2026-09): ``RLIMIT_AS``
    above only bounds VIRTUAL address space, and is HEADROOM above the
    worker's own VmSize at FORK time — on a crawl parent that has grown
    for hours (VmSize 6-15 GB), a single child converting one huge document
    can still reach 10-17 GB RSS before that (already-large) ceiling ever
    fires. ``max_rss_bytes`` (see :data:`_DEFAULT_CONVERT_CHILD_MAX_RSS_MB`)
    is a SEPARATE, absolute ceiling the PARENT polls for directly — see
    :meth:`_await_reply` — rather than relying on the child's own
    accounting at all. Reuses the exact recovery :meth:`_reclaim_timed_out_slot`
    already provides for a per-item timeout (SIGKILL, then promote a spare
    when one is ready); only the trigger and the exception type
    (:class:`_ConvertMemoryGuard`) differ.
    """

    def __init__(
        self,
        size: int,
        *,
        ctx: Optional[Any] = None,
        recycle_after_docs: int = _DEFAULT_CONVERT_RECYCLE_AFTER_DOCS,
        recycle_rss_bytes: int = _DEFAULT_CONVERT_RECYCLE_RSS_MB * 1024 * 1024,
        memory_limit_bytes: int = _DEFAULT_CONVERT_CHILD_MEMORY_LIMIT_MB * 1024 * 1024,
        timeout_s: float = 0.0,
        max_output_bytes: int = _DEFAULT_MAX_CONVERTED_MB * 1024 * 1024,
        max_rss_bytes: int = 0,
        spares_per_slot: int = _DEFAULT_CONVERT_SPARES_PER_SLOT,
    ) -> None:
        self._ctx = ctx or multiprocessing.get_context("fork")
        self._size = max(1, int(size))
        self._recycle_after_docs = max(0, int(recycle_after_docs))
        self._recycle_rss_bytes = max(0, int(recycle_rss_bytes))
        self._memory_limit_bytes = max(0, int(memory_limit_bytes))
        #: Per-item CONVERSION bound — see :data:`_DEFAULT_ITEM_TIMEOUT_S`.
        #: 0 disables it (block on ``conn.recv()`` exactly as before this
        #: existed).
        self._timeout_s = max(0.0, float(timeout_s))
        self._max_output_bytes = max(0, int(max_output_bytes))
        #: Per-item RSS bound the PARENT itself polls for — see
        #: :data:`_DEFAULT_CONVERT_CHILD_MAX_RSS_MB`. 0 disables the watchdog
        #: (no polling at all when this AND ``timeout_s`` are both 0 — see
        #: :meth:`_await_reply`).
        self._max_rss_bytes = max(0, int(max_rss_bytes))
        #: How many pre-forked standbys :meth:`start`/:meth:`repair` keep
        #: ready per slot — see :data:`_DEFAULT_CONVERT_SPARES_PER_SLOT`.
        self._spares_per_slot = max(0, int(spares_per_slot))
        self._procs: List[Optional[Any]] = [None] * self._size
        self._conns: List[Optional[Connection]] = [None] * self._size
        self._doc_counts: List[int] = [0] * self._size
        #: Pre-forked, idle standby QUEUES, one list per slot — see the
        #: class docstring's "N SPARES PER SLOT" section. Consumed FIFO
        #: (``pop(0)``) by :meth:`_swap_in_spare`, refilled up to
        #: ``spares_per_slot`` by :meth:`start`/:meth:`repair`.
        self._spare_procs: List[List[Any]] = [[] for _ in range(self._size)]
        self._spare_conns: List[List[Connection]] = [[] for _ in range(self._size)]

    def start(self) -> None:
        """Fork every slot's ACTIVE worker, and top its SPARE queue up to
        ``spares_per_slot``, wherever either is short. Call only from a
        single-threaded context — see the class docstring.

        Logs ONE INFO line naming the effective absolute ceiling every
        conversion child forked from here will get (this process's own
        current VmSize plus the configured headroom — see
        :func:`_effective_memory_limit_bytes`) whenever this call actually
        forks something and the cap is not disabled (``memory_limit_bytes
        <= 0``) — an operator reading the logs after this fix should see
        the real number a child was actually capped at, not have to derive
        it from the raw config value and their own guess at this worker's
        footprint.
        """
        spawned_any = False
        for slot in range(self._size):
            if self._procs[slot] is None:
                self._spawn(slot)
                spawned_any = True
            while len(self._spare_procs[slot]) < self._spares_per_slot:
                self._spawn_spare(slot)
                spawned_any = True
        if spawned_any and self._memory_limit_bytes > 0:
            effective = _effective_memory_limit_bytes(self._memory_limit_bytes)
            logger.info(
                "sharepoint crawl: conversion child RLIMIT_AS ceiling ~%.0f MiB "
                "(%.0f MiB headroom above this worker's own ~%.0f MiB VmSize)",
                effective / (1024 * 1024),
                self._memory_limit_bytes / (1024 * 1024),
                (effective - self._memory_limit_bytes) / (1024 * 1024),
            )

    def _spawn(self, slot: int) -> None:
        parent_conn, child_conn = self._ctx.Pipe(duplex=True)
        proc = self._ctx.Process(
            target=_convert_worker_main,
            args=(child_conn, self._memory_limit_bytes, self._max_output_bytes),
            daemon=True,
            name=f"sp-convert-{slot}",
        )
        proc.start()
        child_conn.close()  # the parent only ever uses its own end
        self._procs[slot] = proc
        self._conns[slot] = parent_conn
        self._doc_counts[slot] = 0

    def _spawn_spare(self, slot: int) -> None:
        parent_conn, child_conn = self._ctx.Pipe(duplex=True)
        proc = self._ctx.Process(
            target=_convert_worker_main,
            args=(child_conn, self._memory_limit_bytes, self._max_output_bytes),
            daemon=True,
            name=f"sp-convert-{slot}-spare",
        )
        proc.start()
        child_conn.close()
        self._spare_procs[slot].append(proc)
        self._spare_conns[slot].append(parent_conn)

    def convert(
        self,
        slot: int,
        tmp_path: Path,
        mime: str,
        *,
        source_path: Optional[str] = None,
        timeout_s: Optional[float] = None,
    ) -> _ConvertOutcome:
        """Blocking. Runs ``tmp_path`` through slot ``slot``'s dedicated
        worker and returns its outcome, or raises :class:`_ConvertCrashed`
        when that worker died instead of answering, :class:`_ConvertTimedOut`
        when it is still alive but did not answer within ``timeout_s`` (see
        :data:`_DEFAULT_ITEM_TIMEOUT_S`), or :class:`_ConvertMemoryGuard`
        when its RSS crossed ``max_rss_bytes`` (see
        :data:`_DEFAULT_CONVERT_CHILD_MAX_RSS_MB`) — the caller turns any of
        the three into the same ``convert_failed`` outcome an ordinary
        exception would, worded for what actually happened. Also where
        recycling (see the class docstring) is decided and, when a spare is
        ready, carried out — after this call's own result is already
        determined, so a recycle never changes what THIS file's outcome
        was.

        ``timeout_s`` overrides THIS call's own deadline — ``None`` (every
        pre-existing caller) keeps using the pool's own ``timeout_s`` set at
        construction. ``_prepare_document`` passes a SIZE-SCALED value here
        (``src.ingest.convert.conversion_budget_seconds``) so one
        large document gets a longer budget without raising the ceiling for
        every other file the pool ever converts — see that function's
        docstring for the live finding (221 large xlsx/xlsm files that hit
        the previously-flat bound) this fixes.
        """
        proc = self._procs[slot]
        conn = self._conns[slot]
        if proc is None or conn is None or not proc.is_alive():
            detail = self._exit_detail(proc)
            self._swap_in_spare(slot)  # best-effort recovery for the NEXT file
            raise _ConvertCrashed(detail)
        effective_timeout_s = self._timeout_s if timeout_s is None else max(0.0, timeout_s)
        try:
            conn.send((str(tmp_path), mime, source_path))
            reply = self._await_reply(slot, proc, conn, effective_timeout_s)
        except (EOFError, OSError):
            detail = self._exit_detail(proc)
            self._swap_in_spare(slot)
            raise _ConvertCrashed(detail) from None
        self._doc_counts[slot] += 1
        over_doc_budget = bool(self._recycle_after_docs) and self._doc_counts[slot] >= self._recycle_after_docs
        over_rss_ceiling = bool(self._recycle_rss_bytes) and reply.rss_bytes >= self._recycle_rss_bytes
        if over_doc_budget or over_rss_ceiling:
            self._swap_in_spare(slot)
        return reply.outcome

    def _await_reply(self, slot: int, proc: Any, conn: Connection, timeout_s: float) -> "_ConvertReply":
        """Block for slot ``slot``'s reply, honoring BOTH the per-item time
        bound (``timeout_s`` — THIS call's own, not necessarily the pool's
        ``self._timeout_s``; see :meth:`convert`'s docstring) and the
        per-item RSS watchdog (``max_rss_bytes``) — whichever fires first.

        With both disabled this is exactly the pre-watchdog behaviour: one
        unconditional, un-polled ``conn.recv()`` — no periodic wakeups at
        all. Otherwise it polls in
        :data:`_RSS_WATCHDOG_POLL_INTERVAL_S`-second increments (capped by
        whatever remains of ``timeout_s`` when that bound is also active),
        reading the CHILD's own ``/proc/<pid>/status`` on every increment
        the timeout does not fire on. Raises :class:`_ConvertTimedOut` /
        :class:`_ConvertMemoryGuard` — after reclaiming the slot exactly
        the same way (SIGKILL, then :meth:`_swap_in_spare` when a spare is
        ready) — whichever bound crosses first; a caller's ``except
        (EOFError, OSError)`` around THIS call is for a crash discovered
        mid-wait instead, a different case from either guard firing
        cleanly.
        """
        if timeout_s <= 0 and self._max_rss_bytes <= 0:
            return conn.recv()
        deadline = time.monotonic() + timeout_s if timeout_s > 0 else None
        # The watchdog bounds how much THIS conversion grows the child, never
        # the child's absolute RSS. A forked child's ``VmRSS`` starts out as
        # every copy-on-write page it shares with the parent — on a crawl
        # parent that has grown for hours that alone is 9-15 GB, so an
        # absolute ceiling killed every child at its first poll (live
        # finding, 2026-09: ~2 200 documents failed in 15 minutes, first by
        # the guard, then by the slots it had left dead). The baseline is the
        # first reading of this call; ``None`` (no ``/proc``, child already
        # gone) disarms the guard for this call exactly like a failed poll.
        baseline = _child_rss_bytes(getattr(proc, "pid", None)) if self._max_rss_bytes > 0 else None
        while True:
            wait_for = _RSS_WATCHDOG_POLL_INTERVAL_S
            if deadline is not None:
                wait_for = max(0.0, min(wait_for, deadline - time.monotonic()))
            if conn.poll(wait_for):
                return conn.recv()
            if deadline is not None and time.monotonic() >= deadline:
                # Still alive, just too slow — reclaim the slot (SIGKILL,
                # since a genuine native hang can freely ignore SIGTERM) and
                # tell the caller this was a TIMEOUT, not a crash.
                self._reclaim_timed_out_slot(slot)
                raise _ConvertTimedOut(timeout_s)
            if self._max_rss_bytes > 0 and baseline is not None:
                rss_bytes = _child_rss_bytes(getattr(proc, "pid", None))
                growth = rss_bytes - baseline if rss_bytes is not None else None
                if growth is not None and growth >= self._max_rss_bytes:
                    # Still alive, just too big — same reclaim path as a
                    # timeout (SIGKILL, then swap in a spare), a different
                    # trigger and a differently-worded exception. The
                    # reported number is the GROWTH this conversion caused,
                    # the only figure the guard is entitled to blame on it.
                    self._reclaim_timed_out_slot(slot)
                    raise _ConvertMemoryGuard(growth, self._max_rss_bytes)

    def _exit_detail(self, proc: Optional[Any]) -> str:
        if proc is None:
            return "unknown"
        proc.join(timeout=5)
        code = proc.exitcode
        if code is None:
            return "unknown"
        if code < 0:
            try:
                return signal.Signals(-code).name
            except ValueError:
                return f"signal {-code}"
        if code > 0:
            return f"exit code {code}"
        return "unknown"

    def _reclaim_timed_out_slot(self, slot: int) -> None:
        """Forcibly reclaim slot ``slot`` after its worker failed to answer
        within ``timeout_s`` (see :data:`_DEFAULT_ITEM_TIMEOUT_S`) OR
        crossed the RSS watchdog's ceiling (see
        :data:`_DEFAULT_CONVERT_CHILD_MAX_RSS_MB`) — the name predates the
        second trigger, but the reclaim itself is IDENTICAL for both: only
        the caller's exception type differs.

        SIGKILL, never :meth:`Process.terminate`'s SIGTERM: a worker stuck
        inside native conversion code — the same class of failure crash
        isolation already guards against, just hanging (or growing)
        instead of aborting — can freely ignore a termination request, and
        this path must not itself risk hanging waiting for a process that
        will never cooperate. Recovery mirrors a crash: :meth:`_swap_in_spare`
        promotes the next pre-forked SPARE immediately when one is ready
        (safe from ANY thread, same as a crash or a recycle); otherwise the
        slot stays down until the next :meth:`repair`.
        """
        proc = self._procs[slot]
        if proc is not None and proc.is_alive():
            proc.kill()
            proc.join(timeout=5)
        self._swap_in_spare(slot)

    def _swap_in_spare(self, slot: int) -> bool:
        """Retire slot's ACTIVE process (dead from a crash, or simply past
        its recycle budget) and promote the NEXT pre-forked SPARE in its
        queue, consumed FIFO (oldest-forked first — see the class
        docstring's "N SPARES PER SLOT" section).

        No ``fork()`` — only a termination signal to the retiree and a
        pointer reassignment — so this is safe to call from ANY thread, at
        ANY point in a page, unlike :meth:`_spawn`/:meth:`repair`; that is
        what lets a slot recycle (or recover from a crash) mid-page rather
        than only at the next page boundary. A spare found already dead in
        the queue (rare — an idle standby crashing before it was ever used)
        is reaped and skipped rather than treated as a failed swap, so it
        never strands a live spare queued behind it. Returns ``False``,
        leaving the slot exactly as it was, only when the queue is
        completely empty — the caller (:meth:`convert`) already handles
        both outcomes: a dead slot stays dead until :meth:`repair`, same as
        before spares existed; a merely over-budget slot just keeps running
        past its budget.
        """
        spares = self._spare_procs[slot]
        conns = self._spare_conns[slot]
        while spares:
            spare_proc = spares.pop(0)
            spare_conn = conns.pop(0)
            if not spare_proc.is_alive():
                self._close_given(spare_proc, spare_conn)
                continue
            self._close_slot(slot)
            self._procs[slot] = spare_proc
            self._conns[slot] = spare_conn
            self._doc_counts[slot] = 0
            return True
        return False

    def repair(self) -> List[int]:
        """Replace every dead ACTIVE slot with a fresh worker, and top every
        slot's SPARE queue back up to ``spares_per_slot`` (pruning any spare
        found dead in the queue first — see :meth:`_swap_in_spare`'s own
        docstring for when that happens). Call only from a point the caller
        has proven single-threaded (a delta-page boundary, after that
        page's item-concurrency thread pool has been joined — see the class
        docstring for why no MORE frequent safe point exists in that
        branch). Returns the repaired ACTIVE slot indices — used by tests.
        """
        repaired = []
        for slot, proc in enumerate(self._procs):
            if proc is None or not proc.is_alive():
                self._close_slot(slot)
                self._spawn(slot)
                repaired.append(slot)
        for slot in range(self._size):
            spares = self._spare_procs[slot]
            conns = self._spare_conns[slot]
            alive_procs: List[Any] = []
            alive_conns: List[Connection] = []
            for spare_proc, spare_conn in zip(spares, conns):
                if spare_proc.is_alive():
                    alive_procs.append(spare_proc)
                    alive_conns.append(spare_conn)
                else:
                    self._close_given(spare_proc, spare_conn)
            self._spare_procs[slot] = alive_procs
            self._spare_conns[slot] = alive_conns
            while len(self._spare_procs[slot]) < self._spares_per_slot:
                self._spawn_spare(slot)
        return repaired

    def _close_given(self, proc: Optional[Any], conn: Optional[Connection]) -> None:
        """Close one connection and retire+reap one process — the shared
        tail of :meth:`_close_slot` and every SPARE-queue cleanup path
        (:meth:`_swap_in_spare`, :meth:`repair`, :meth:`shutdown`), so all
        of them terminate a worker the same way."""
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass
        _retire_process(proc)

    def _close_slot(self, slot: int) -> None:
        self._close_given(self._procs[slot], self._conns[slot])
        self._procs[slot] = None
        self._conns[slot] = None

    def shutdown(self) -> None:
        """Signal every live worker (active AND every queued spare) to
        exit, then reap them all. Safe to call more than once and safe to
        call on a pool that never started."""
        all_spare_conns = [conn for conns in self._spare_conns for conn in conns]
        for conn in (*self._conns, *all_spare_conns):
            if conn is not None:
                try:
                    conn.send(None)
                except OSError:
                    pass
        for slot in range(self._size):
            self._close_slot(slot)
            for spare_proc, spare_conn in zip(self._spare_procs[slot], self._spare_conns[slot]):
                self._close_given(spare_proc, spare_conn)
            self._spare_procs[slot] = []
            self._spare_conns[slot] = []


@dataclass
class _PreparedDocument:
    """Outcome of the blocking half of one item: hash -> convert -> anonymize.

    A value rather than in-place counter bumps, because this runs on a worker
    thread: the caller (on the event loop) turns the outcome into the exact
    same counters the sequential pipeline recorded, so a fail-closed refusal
    is still counted once, per item, whatever thread noticed it.

    ``path``/``filename`` are the RESOLVED values the caller ingests — the
    real drive-relative path and ``<stem>.md`` for a plain scope, or their
    anonymized equivalents for an anonymize-marked one (see
    :func:`_anonymize_identity`). Never the raw ones when the scope
    anonymizes: a caller that ingested ``path``/``filename`` on ``"ok"``
    needs no branch of its own on ``anonymize`` to know which to use.
    """

    outcome: str  # "ok" | "convert_failed" | "convert_empty" | "convert_unsupported" | "anonymize_failed"
    markdown: str = ""
    source_sha256: str = ""
    path: str = ""
    filename: str = ""
    #: Why a ``"convert_failed"`` outcome failed — empty for every other
    #: outcome, and gated by scope: see :func:`_prepare_document`'s
    #: ``_convert_failure_detail`` for what this may and may not contain.
    detail: str = ""
    #: Which rung of the conversion rescue chain succeeded, on an ``"ok"``
    #: outcome — ``""`` (the ordinary case, no rescue needed),
    #: ``"libreoffice_resave"``, ``"csv_fallback"``, or ``"pdf_fallback"``.
    #: Mirrors ``src.ingest.convert.ConvertResult.rescue`` — see
    #: :func:`_process_item`'s use of it for ``CrawlStats.conversion_rescued``.
    rescue: str = ""
    #: WHY a ``"convert_failed"`` outcome failed, from the closed
    #: ``src.ingest.convert.ERROR_CLASS_*`` vocabulary — empty for
    #: every other outcome. Fed into ``_note_retry`` (2026-09-04 finding #66
    #: item 1), which is what lets a LATER run decide whether this item's
    #: failure is DETERMINISTIC enough to skip without a download (item 2).
    #: Unlike ``detail`` this is never scope-gated: it is one of a fixed,
    #: small set of class names, never document content.
    error_class: str = ""


def _anonymize_identity(path: str, name: str, *, key: bytes, detector: Any) -> Tuple[str, str]:
    """The ``(path, filename)`` an anonymize-marked scope stores instead of
    the real ones — same key, same detector as the document body.

    The source name and folder path are routinely the single most
    re-identifying string in a document (a deal name, a client name); an
    anonymize-marked scope's promise that "Agnes never holds the original at
    all" (``docs/anonymization.md``) is broken if they survive verbatim
    while the body is redacted.

    Each path SEGMENT is anonymized independently — not the path as one
    string — and the ``"/"`` separator structure is kept: two files under
    the same real folder still share the same anonymized folder prefix
    (the substitution is deterministic under one key), so prefix matching,
    the exclusion index and the corpus-map resolver keep working the same
    SHAPE against the anonymized tree they worked against the real one, even
    though no segment is readable any more.

    The returned ``path``'s leaf segment keeps the SOURCE file's extension
    (only its stem is anonymized) and ``filename`` is always ``<stem>.md`` —
    mirroring the exact relationship the un-anonymized values already have.
    Only the identity-bearing STEM changes, never the suffix.

    Routing decisions (which collection, which exclusion rule) are made
    EARLIER in the pipeline against the RAW path — those decisions come from
    admin-configured real folder names and must see the real thing. This
    function only prepares what gets PERSISTED.
    """
    stem = Path(name).stem or name
    suffix = Path(name).suffix
    anonymized_stem = str(anonymize_markdown(stem, key=key, detector=detector).text)
    filename = f"{anonymized_stem}.md"
    folder = path.rsplit("/", 1)[0] if "/" in path else ""
    segments = [
        str(anonymize_markdown(segment, key=key, detector=detector).text) for segment in folder.split("/") if segment
    ]
    anonymized_path = "/".join([*segments, f"{anonymized_stem}{suffix}"])
    return anonymized_path, filename


def _convert_failure_detail(detail_type: str, detail_message: str, *, anonymize: bool) -> str:
    """The ``detail`` a ``"convert_failed"`` outcome is allowed to carry,
    decided by THIS scope's anonymize flag — see :func:`_prepare_document`'s
    docstring for the full reasoning. ``detail_message`` may quote a
    fragment of the document a converter just failed on; ``detail_type``
    (``type(exc).__name__``) never does and is always kept."""
    if not anonymize and detail_message:
        return detail_message
    return detail_type


def _anonymize_identity(path: str, name: str, *, key: bytes, detector: Any) -> Tuple[str, str]:
    """The ``(path, filename)`` an anonymize-marked scope stores instead of
    the real ones — same key, same detector as the document body.

    The source name and folder path are routinely the single most
    re-identifying string in a document (a deal name, a client name); an
    anonymize-marked scope's promise that "Agnes never holds the original at
    all" (``docs/anonymization.md``) is broken if they survive verbatim
    while the body is redacted.

    Each path SEGMENT is anonymized independently — not the path as one
    string — and the ``"/"`` separator structure is kept: two files under
    the same real folder still share the same anonymized folder prefix
    (the substitution is deterministic under one key), so prefix matching,
    the exclusion index and the corpus-map resolver keep working the same
    SHAPE against the anonymized tree they worked against the real one, even
    though no segment is readable any more.

    The returned ``path``'s leaf segment keeps the SOURCE file's extension
    (only its stem is anonymized) and ``filename`` is always ``<stem>.md`` —
    mirroring the exact relationship the un-anonymized values already have.
    Only the identity-bearing STEM changes, never the suffix.

    Routing decisions (which collection, which exclusion rule) are made
    EARLIER in the pipeline against the RAW path — those decisions come from
    admin-configured real folder names and must see the real thing. This
    function only prepares what gets PERSISTED.
    """
    stem = Path(name).stem or name
    suffix = Path(name).suffix
    anonymized_stem = str(anonymize_markdown(stem, key=key, detector=detector).text)
    filename = f"{anonymized_stem}.md"
    folder = path.rsplit("/", 1)[0] if "/" in path else ""
    segments = [
        str(anonymize_markdown(segment, key=key, detector=detector).text) for segment in folder.split("/") if segment
    ]
    anonymized_path = "/".join([*segments, f"{anonymized_stem}{suffix}"])
    return anonymized_path, filename


#: Signals whose only realistic cause on this pool is memory pressure
#: applied from OUTSIDE the crashed worker's own accounting — the kernel's
#: OOM killer picking whichever child happens to be allocating at the
#: moment a memory cgroup hits its ceiling, per `_ConvertProcessPool`'s
#: RLIMIT_AS section. A worker's OWN ceiling turns that same failure mode
#: into a `MemoryError`, handled separately (see `_prepare_document`), so a
#: SIGKILL that still reaches here was never given the chance to attribute
#: itself. Every OTHER signal (SIGABRT, SIGSEGV, SIGBUS, ...) is a native
#: abort raised BY the conversion backend on THIS file's own content —
#: attributable, and worded as a plain crash.
_EXTERNAL_PRESSURE_SIGNALS = frozenset({"SIGKILL"})


def _convert_crash_detail(signal_name: str) -> str:
    """The operator-facing wording for a :class:`_ConvertCrashed` failure —
    deliberately different for a SIGKILL (see :data:`_EXTERNAL_PRESSURE_SIGNALS`)
    than for any other signal, so a reader is never left guessing whether
    the FILE is at fault or was collateral damage from a sibling's spike."""
    if signal_name in _EXTERNAL_PRESSURE_SIGNALS:
        return f"conversion worker killed by memory pressure outside its control ({signal_name}) — may not be this file's fault"
    return f"conversion worker crashed: {signal_name}"


def _convert_memory_guard_detail(rss_bytes: int) -> str:
    """The operator-facing wording for a :class:`_ConvertMemoryGuard`
    failure — ATTRIBUTABLE, deliberately never the
    :func:`_convert_crash_detail` "may not be this file's fault" wording a
    bare external SIGKILL gets: this SIGKILL was issued by this pool
    itself, for a reason it can name with certainty (the observed RSS this
    poll crossed the configured ceiling at), not by the kernel's OOM killer
    or a host-level watchdog acting on pressure this process never saw
    coming."""
    rss_mb = rss_bytes / (1024 * 1024)
    return f"exceeded the conversion memory guard (grew {rss_mb:.0f} MB RSS during this conversion)"


def _prepare_document(
    tmp_path: Path,
    *,
    mime: str,
    path: str,
    name: str,
    anonymize: bool,
    anonymization_key: Optional[bytes],
    detector: Any,
    convert_pool: Optional[_ConvertProcessPool] = None,
    convert_slot: int = 0,
) -> _PreparedDocument:
    """Hash, convert and (for an anonymize-marked scope) anonymize one file —
    its BODY, and (see :func:`_anonymize_identity`) its filename and path.

    Pure with respect to crawl state — it touches no counters, no cTags and
    no state file — which is what makes it safe to run on a worker thread.
    An unexpected failure of the HASH itself still propagates (as it did
    before): a file we cannot read is not a convert failure.

    Convert alone is isolated in a child process (``convert_pool``, see its
    class docstring for why); anonymize stays HERE, on the calling thread, in
    THIS process — it needs the HMAC key, and that key must never cross a
    process boundary (on argv, in an environment variable of a child we do
    not control, or otherwise) when there is no need for it to. Anonymize
    input is already-converted markdown, not attacker-shaped file bytes, and
    has no native-library dependency of the kind that motivated isolating
    convert in the first place, so it carries none of the crash risk.
    ``convert_pool=None`` (only ever a testing/unit-call default — the real
    crawl always passes one) falls back to calling the converter inline, the
    exact pre-isolation behaviour.

    A ``"convert_failed"`` outcome's ``detail`` is gated by THIS scope's
    ``anonymize`` flag (owner decision 2026-09-01, reconciling #1993's
    per-file error detail with this module's own no-content-leaves-the-file
    -reader rule): a plain scope keeps the exception's full message — an
    admin on that connection can already open the document itself, so a
    diagnosable failure beats a silent one — while an anonymize-marked
    scope keeps only the exception's TYPE, since that scope's whole premise
    is that document content never reaches storage in readable form, and a
    conversion exception can quote a fragment of the very file it read. A
    native crash's ``detail`` (the signal name) is never document content,
    so it is kept for both.

    A ``MemoryError`` (own-limit) failure and a signal crash's ``detail``
    are DELIBERATELY worded differently (owner decision 2026-09-01, live
    deployment #2): the former is a document this pool's own
    ``RLIMIT_AS`` ceiling attributes to THIS file with certainty; the
    latter, when the signal is ``SIGKILL`` specifically, is the kernel's
    OOM killer reaching in from OUTSIDE this process's own accounting and
    may have picked this file's worker only because it happened to be
    allocating at the wrong moment — an operator reading a bare
    ``SIGKILL`` cannot tell those apart, so :func:`_convert_crash_detail`
    says so explicitly.

    A ``"ConvertedTooLarge"`` outcome (live deployment #3, 2026-09-02: see
    :data:`_DEFAULT_MAX_CONVERTED_MB`) is worded the SAME way regardless of
    ``anonymize``, same as ``MemoryError`` above: its ``detail_message`` is
    built by :func:`_convert_worker_main` from byte counts alone, never
    document content, so there is nothing for an anonymize-marked scope to
    gate.

    A :class:`_ConvertMemoryGuard` failure (live deployment #4, 2026-09:
    see :data:`_DEFAULT_CONVERT_CHILD_MAX_RSS_MB`) is worded the SAME way
    regardless of ``anonymize`` too, and — like ``MemoryError`` above, not
    like a bare signal crash — ATTRIBUTABLY: :func:`_convert_memory_guard_detail`
    is built from the observed RSS byte count alone, never document
    content, and this SIGKILL was issued by THIS pool for a reason it can
    name, not by an OOM killer reaching in from outside its own accounting.
    """
    source_sha256 = _sha256_file(tmp_path)
    # Lazy, and only needed to catch a specific exception TYPE (the pool
    # path below never imports this module at all — it only compares the
    # class NAME the child sent back over the pipe) — see
    # `src.ingest.convert.UnsupportedConversionFormat`'s
    # docstring for why this is counted apart from `convert_failed`.
    from src.ingest.convert import (
        ERROR_CLASS_MEMORY_KILL,
        ERROR_CLASS_OTHER,
        ERROR_CLASS_TIMEOUT,
        ERROR_CLASS_WORKER_CRASH,
        UnsupportedConversionFormat,
        conversion_budget_seconds,
    )

    rescue = ""
    try:
        if convert_pool is not None:
            # Size-scaled per-item timeout (see `conversion_budget_seconds`'s
            # docstring for the live finding this fixes — 221 large xlsx/xlsm
            # files, average 15 MB, hit the previously-flat bound): computed
            # per FILE, not once at pool construction, so one large document
            # gets a longer budget without raising the ceiling every other
            # file in the pool converts under. `_item_timeout_seconds()` is
            # this run's own configured base (0 disables the bound entirely,
            # preserved by `conversion_budget_seconds`).
            try:
                tmp_size = tmp_path.stat().st_size
            except OSError:
                tmp_size = 0
            timeout_s = conversion_budget_seconds(tmp_size, base_seconds=_item_timeout_seconds())
            outcome = convert_pool.convert(convert_slot, tmp_path, mime, source_path=path, timeout_s=timeout_s)
            if not outcome.ok:
                if outcome.detail_type == "MemoryError":
                    detail = "exceeded its own memory limit"
                    error_class = ERROR_CLASS_MEMORY_KILL
                elif outcome.detail_type == "ConvertedTooLarge":
                    detail = outcome.detail_message
                    error_class = ERROR_CLASS_OTHER
                elif outcome.detail_type == "UnsupportedConversionFormat":
                    logger.info("sharepoint crawl: no conversion backend for %s — skipped, not an error", path)
                    return _PreparedDocument("convert_unsupported", detail=outcome.detail_message)
                else:
                    detail = _convert_failure_detail(outcome.detail_type, outcome.detail_message, anonymize=anonymize)
                    error_class = outcome.error_class or ERROR_CLASS_OTHER
                logger.warning("sharepoint crawl: conversion failed for %s: %s", path, detail)
                return _PreparedDocument("convert_failed", detail=detail, error_class=error_class)
            markdown = outcome.markdown
            rescue = outcome.rescue
        else:
            converted = convert_to_markdown(tmp_path, mime, source_path=path)
            markdown = str(getattr(converted, "markdown", "") or "")
            rescue = str(getattr(converted, "rescue", "") or "")
    except UnsupportedConversionFormat as exc:
        # Only reachable via the non-pool (inline) path above — the pool
        # path never raises here, it reports `outcome.ok=False` instead
        # (see the branch just above). Kept distinct from the broad
        # `except Exception` below for the same reason that branch is:
        # nothing was attempted, so this is a skip, not a failure.
        logger.info("sharepoint crawl: no conversion backend for %s — skipped, not an error", path)
        return _PreparedDocument("convert_unsupported", detail=str(exc))
    except _ConvertTimedOut as exc:
        # The child was still ALIVE but did not answer within the per-item
        # time bound (see `_DEFAULT_ITEM_TIMEOUT_S`) — killed and, when a
        # spare was ready, already replaced by `convert_pool.convert`
        # itself. Worded distinctly from a crash: this file did not abort,
        # it simply ran too long, which is a different, equally attributable
        # reason. The crawl continues with the next file.
        detail = f"conversion exceeded the {exc.timeout_s:.0f}s per-item time budget"
        logger.warning("sharepoint crawl: conversion timed out for %s: %s", path, detail)
        return _PreparedDocument("convert_failed", detail=detail, error_class=ERROR_CLASS_TIMEOUT)
    except _ConvertMemoryGuard as exc:
        # The child was still ALIVE but its RSS crossed the watchdog's own
        # ceiling — killed and, when a spare was ready, already replaced by
        # `convert_pool.convert` itself, exactly like a timeout. Worded
        # ATTRIBUTABLY (never the external-pressure wording a bare SIGKILL
        # crash gets): this pool killed its own child, for a reason it can
        # name. The crawl continues with the next file.
        detail = _convert_memory_guard_detail(exc.rss_bytes)
        logger.warning("sharepoint crawl: conversion failed for %s: %s", path, detail)
        return _PreparedDocument("convert_failed", detail=detail, error_class=ERROR_CLASS_MEMORY_KILL)
    except _ConvertCrashed as exc:
        # The child that was converting this file died from a signal (a
        # native abort/segfault, not a Python exception) — the one failure
        # mode a `try/except Exception` can never catch, because nothing
        # raises here: the process running it is simply gone. Counted and
        # skipped exactly like an ordinary conversion failure; the crawl
        # continues with the next file, and this process — the one running
        # the crawl loop — was never at risk.
        detail = _convert_crash_detail(exc.signal_name)
        logger.warning("sharepoint crawl: conversion failed for %s: %s", path, detail)
        return _PreparedDocument("convert_failed", detail=detail, error_class=ERROR_CLASS_WORKER_CRASH)
    except Exception as exc:  # noqa: BLE001 — one unconvertible file, not a broken run
        logger.warning("sharepoint crawl: conversion failed for %s: %s", path, type(exc).__name__)
        detail = _convert_failure_detail(type(exc).__name__, str(exc), anonymize=anonymize)
        error_class = getattr(exc, "error_class", None) or ERROR_CLASS_OTHER
        return _PreparedDocument("convert_failed", detail=detail, error_class=error_class)
    if not markdown.strip():
        logger.info("sharepoint crawl: conversion produced no text for %s", path)
        return _PreparedDocument("convert_empty")

    if anonymize:
        # FAIL CLOSED. An anonymize-marked scope promised its audience that
        # no raw identifier reaches the collection — not in the body, and not
        # in the filename or folder path either; a document any part of which
        # cannot be anonymized is therefore counted and dropped, never
        # ingested in its original form. Unchanged by concurrency: the
        # refusal is decided per document, inside this function, before any
        # caller can ingest it.
        if anonymization_key is None:
            return _PreparedDocument("anonymize_failed")
        try:
            markdown = str(anonymize_markdown(markdown, key=anonymization_key, detector=detector).text)
            out_path, out_filename = _anonymize_identity(path, name, key=anonymization_key, detector=detector)
        except Exception as exc:  # noqa: BLE001 — incl. ImportError / DetectionUnavailable
            logger.warning("sharepoint crawl: anonymization failed for %s: %s", path, type(exc).__name__)
            return _PreparedDocument("anonymize_failed")
    else:
        out_path, out_filename = path, f"{Path(name).stem or name}.md"
    return _PreparedDocument(
        "ok", markdown=markdown, source_sha256=source_sha256, path=out_path, filename=out_filename, rescue=rescue
    )


def _note_retry(
    state: Dict[str, Any],
    stats: CrawlStats,
    stable_id: str,
    *,
    target: DriveTarget,
    item: Dict[str, Any],
    path: str,
    error_class: str,
    detail: str = "",
    content_sha: str = "",
) -> None:
    """Record one failed pass over ``stable_id`` so it is retried on a
    future run regardless of what the delta feed offers next — see the
    module docstring's "a per-item failure never advances past itself".

    Stores the item dict AS SEEN at failure time (not a fresh Graph
    fetch): it already carries everything :func:`_process_item` needs to
    retry — id, name, parentReference, size, mimeType, cTag — so a retry
    costs one extra download attempt, not a second round-trip through the
    delta/metadata API. Idempotent to call again on a repeat failure: the
    attempt counter accumulates across runs (and across a same-run retry
    replay landing on the same item twice), it is never reset except by a
    success (:func:`_clear_retry`) or an operator-requested resync.

    ``error_class`` (2026-09-04 finding #66 item 1 — a closed vocabulary,
    see ``src.ingest.convert.ERROR_CLASS_*``) and ``detail`` (the
    caller's own human-readable failure text, truncated here) are this
    ATTEMPT's own classification — always overwritten, never merged with a
    prior attempt's: a document that failed one way last time and a
    different way this time should be judged on its MOST RECENT failure,
    which is what :func:`_doomed_skip_reason` reads. ``content_sha`` (only
    ever passed for an ``ingest_error`` — conversion succeeded, so a hash of
    the converted bytes exists) is kept purely for an operator to confirm
    what was actually re-hashed; the doomed-skip decision itself relies on
    the item's own cTag (compared against what THIS attempt saw), the same
    pre-download signal every other cTag-gated skip in this module already
    trusts.
    """
    with _state_lock:
        failed_items: Dict[str, Any] = state.setdefault("failed_items", {})
        entry = failed_items.get(stable_id)
        if not isinstance(entry, dict):
            entry = {"first_failed_at": _now_iso()}
        entry["state_key"] = target.state_key
        entry["item"] = item
        entry["path"] = path
        entry["last_failed_at"] = _now_iso()
        entry["error_class"] = error_class
        entry["last_error"] = (detail or "")[:500]
        if content_sha:
            entry["content_sha"] = content_sha
        attempts = int(entry.get("attempts", 0)) + 1
        entry["attempts"] = attempts
        just_exhausted = attempts >= _MAX_ITEM_RETRY_ATTEMPTS and not entry.get("given_up")
        if just_exhausted:
            entry["given_up"] = True
        failed_items[stable_id] = entry
    if just_exhausted:
        stats.add(item_retry_given_up=1)
        logger.warning(
            "sharepoint crawl: giving up on %s after %d failed attempts — it stays out of the "
            "collection until an operator intervenes (fix the file, or run a resync); the state "
            "file and the run report keep the record",
            path,
            attempts,
        )


def _clear_retry(state: Dict[str, Any], stable_id: str) -> bool:
    """Drop ``stable_id`` from the failure queue — it just ingested cleanly,
    however it got here (a normal delta row or a queued retry). Returns
    whether it was actually IN the queue, so a caller can tell "this was a
    prior failure that just recovered" from "this item never failed"."""
    with _state_lock:
        failed_items = state.get("failed_items")
        if not failed_items:
            return False
        return failed_items.pop(stable_id, None) is not None


def _note_empty(state: Dict[str, Any], stable_id: str, *, target: DriveTarget, item: Dict[str, Any], path: str) -> None:
    """Record one ``convert_empty`` outcome for ``stable_id`` — this is the
    write ``load_state``'s ``empty_items`` docstring promises, so a future
    admin-requested ``retry_empty`` run (:func:`_retry_empty_items`) knows
    which items to reconsider without a full re-enumeration.

    Idempotent, same shape as :func:`_note_retry` minus the retry-attempt
    bookkeeping (this is not a failure queue an ordinary run replays — see
    ``empty_items``'s own docstring for why). Bounded to
    :data:`_FAILED_ITEMS_CAP` total entries, FIFO: once at capacity, the
    OLDEST-inserted entry is evicted to make room for a new one — the same
    "an honestly partial list beats an unbounded one" trade the run report's
    own capped lists make, applied to persisted state. An already-recorded
    item is only ever updated in place, never counted against the cap again.
    """
    with _state_lock:
        empty_items: Dict[str, Any] = state.setdefault("empty_items", {})
        entry = empty_items.get(stable_id)
        if not isinstance(entry, dict):
            if len(empty_items) >= _FAILED_ITEMS_CAP:
                oldest = next(iter(empty_items), None)
                if oldest is not None:
                    del empty_items[oldest]
            entry = {"first_seen_at": _now_iso()}
        entry["state_key"] = target.state_key
        entry["item"] = item
        entry["path"] = path
        entry["last_seen_at"] = _now_iso()
        empty_items[stable_id] = entry


def _clear_empty(state: Dict[str, Any], stable_id: str) -> bool:
    """Drop ``stable_id`` from the empty-document backlog — it just ingested
    with real content, however it got there (an ordinary re-crawl after the
    source document changed, or an admin-requested ``retry_empty`` replay
    that finally produced text). Mirrors :func:`_clear_retry`."""
    with _state_lock:
        empty_items = state.get("empty_items")
        if not empty_items:
            return False
        return empty_items.pop(stable_id, None) is not None


def _doomed_classification(error_class: Optional[str], attempts: int) -> Optional[str]:
    """Whether ``(error_class, attempts)`` ALONE — no cTag, no
    ``force_reprocess`` — is trusted doomed, and under which rule.

    Returns a ``reason_type`` (``"doomed"`` for a
    ``src.ingest.convert.DETERMINISTIC_ERROR_CLASSES`` member at
    :data:`_DOOMED_SKIP_MIN_ATTEMPTS`, or ``"doomed_after_repeated_<class>"``
    for a :data:`_REPEATED_FAILURE_ERROR_CLASSES` member — ``timeout``,
    ``memory_kill``, ``worker_crash`` — at
    :data:`_DOOMED_SKIP_MIN_ATTEMPTS_REPEATED`, TCRD-296 gap #74), or
    ``None`` when neither threshold is met yet.

    Shared by :func:`_doomed_skip_reason` (which adds the cTag and
    ``force_reprocess`` gate before turning this into a live skip decision)
    and :func:`_retry_backlog_snapshot` (the STANDING backlog view, which is
    deliberately cTag-blind — see its own docstring): one place decides
    "is this entry's history enough to trust", so the live skip and the
    reported backlog count can never silently drift apart.
    """
    from src.ingest.convert import DETERMINISTIC_ERROR_CLASSES

    if error_class in DETERMINISTIC_ERROR_CLASSES:
        return "doomed" if attempts >= _DOOMED_SKIP_MIN_ATTEMPTS else None
    if error_class in _REPEATED_FAILURE_ERROR_CLASSES:
        return f"doomed_after_repeated_{error_class}" if attempts >= _DOOMED_SKIP_MIN_ATTEMPTS_REPEATED else None
    return None


def _doomed_skip_reason(
    state: Dict[str, Any], stable_id: str, *, item: Dict[str, Any], force_reprocess: bool
) -> Optional[Tuple[str, str]]:
    """Whether ``stable_id`` should be skipped WITHOUT a download this run —
    2026-09-04 finding #66 item 2 (see :data:`_DOOMED_SKIP_MIN_ATTEMPTS`'s
    docstring), extended by TCRD-296 gap #74 (see
    :data:`_DOOMED_SKIP_MIN_ATTEMPTS_REPEATED`'s docstring) to a document
    that keeps crashing or timing out the converter, not just one the
    converter deterministically rejects.

    Returns ``(reason_type, reason)`` (for :meth:`CrawlStats.
    note_skipped_doomed`), or ``None`` when the item should be attempted
    normally. Never skips when:

    * ``force_reprocess`` is set — the operator's explicit "re-process
      everything regardless" override, the same escape hatch every other
      cTag-based skip in this module honors;
    * :func:`_doomed_classification` says the recorded ``(error_class,
      attempts)`` does not clear either threshold yet — a
      ``DETERMINISTIC_ERROR_CLASSES`` member below
      :data:`_DOOMED_SKIP_MIN_ATTEMPTS`, a ``_REPEATED_FAILURE_ERROR_CLASSES``
      member below :data:`_DOOMED_SKIP_MIN_ATTEMPTS_REPEATED`, or an
      ``error_class`` in neither set (``download_error``/``other`` stay
      retryable forever — never a property of the document);
    * the item's cTag/eTag no longer matches what was recorded at the last
      failed attempt — new content deserves fresh attempts, and this is the
      SAME pre-download "did it change" signal :func:`_process_item`'s own
      unchanged-detection already trusts elsewhere in this function, which is
      what lets this decision be made WITHOUT a download. This also covers
      ``ingest_error``'s own "same content" requirement: Graph's cTag
      changes whenever a document's content changes, so an unchanged cTag is
      the same fact an unchanged ``content_sha`` would confirm after a
      download this function deliberately never pays for.
    """
    if force_reprocess:
        return None
    failed_items = state.get("failed_items") or {}
    entry = failed_items.get(stable_id)
    if not isinstance(entry, dict):
        return None
    error_class = entry.get("error_class")
    attempts = int(entry.get("attempts", 0))
    reason_type = _doomed_classification(error_class, attempts)
    if reason_type is None:
        return None
    recorded_item = entry.get("item") or {}
    recorded_ctag = recorded_item.get("cTag") or recorded_item.get("eTag")
    current_ctag = item.get("cTag") or item.get("eTag")
    if not recorded_ctag or recorded_ctag != current_ctag:
        return None
    last_error = (entry.get("last_error") or "")[:200]
    reason = f"{error_class}: failed {attempts} time(s), most recently: {last_error}".strip()
    return reason_type, reason


async def _process_item(
    item: Dict[str, Any],
    *,
    target: DriveTarget,
    ctx: _ScopeContext,
    transport: GraphTransport,
    ingestor: _Ingestor,
    state: Dict[str, Any],
    stats: CrawlStats,
    max_file_mb: int,
    anonymization_key: Optional[bytes],
    detector: Any = None,
    pool: Optional[ThreadPoolExecutor] = None,
    force_reprocess: bool = False,
    convert_pool: Optional[_ConvertProcessPool] = None,
    convert_slot: int = 0,
) -> None:
    """One delta row -> at most one ingested document. Never raises for a
    per-file fault: a locked, vanished, unconvertible, or un-anonymizable
    document is COUNTED and skipped, because one bad file must not cost a
    100k-file pass.

    ``force_reprocess`` (the operator "re-process everything" run option)
    bypasses ONLY the cTag-equality skip below — the item is downloaded,
    converted and re-ingested even when its cTag already matches what is on
    record. Everything else about the item (oversize cap, exclusions,
    anonymize-fail-closed, the retry queue) is unaffected.

    Safe to run concurrently with itself: every counter goes through
    :meth:`CrawlStats.add`, and the two state mutations (this item's cTag) are
    taken under :data:`_state_lock`, which is the same lock
    :func:`save_state` serializes on. ``pool`` puts the blocking
    convert/anonymize/ingest section on a worker thread; at ``None`` the
    calls are inline, i.e. the pre-parallel path exactly.
    """
    from src.ingest.convert import ERROR_CLASS_DOWNLOAD_ERROR, ERROR_CLASS_INGEST_ERROR, ERROR_CLASS_OTHER

    name = str(item.get("name") or "")
    stable_id = f"graph:{item['id']}"
    ctags: Dict[str, Any] = state["ctags"]

    if item.get("deleted"):
        if target.root_item_id is not None and stable_id not in ctags:
            # Folder-shard delete guard (2026-09-03 auto-parallel-crawl
            # design §6): an item that moved between shards mid-run can
            # surface a `deleted` row on a NEIGHBOUR shard's delta feed
            # before (or without) ever appearing as an add/change on THIS
            # shard's own state row. Applying the delete here would risk
            # removing a document a sibling shard just ingested into the
            # same collection. Only the shard whose own `ctags` carries the
            # item may act on its delete — a whole-drive target (no
            # `root_item_id`) is never split this way, so it keeps today's
            # unconditional behaviour.
            return
        for candidate in ctx.candidate_collection_ids(target.drive_id):
            if await _run_blocking(pool, ingestor.delete, candidate, stable_id):
                stats.add(deleted=1)
                break
        with _state_lock:
            ctags.pop(stable_id, None)
        return
    if "file" not in item or _should_skip_name(name):
        return

    path = _drive_relative_path(item.get("parentReference") or {}, name)
    if _excluded_file(path, stable_id, ctx.exclusions):
        stats.add(excluded_subtree_skips=1)
        return
    if ctx.exclusions.folder_prefixes and _under_prefix(path, ctx.exclusions.folder_prefixes):
        stats.add(excluded_subtree_skips=1)
        return

    if ctx.min_modified is not None:
        # `extraction.crawl.min_modified` (per-connection age filter, see
        # `resolve_min_modified`). Boundary rule: a cutoff of 2023-12-31
        # KEEPS an item modified on 2023-12-31T00:00:00Z or later — only
        # strictly-before is filtered. An item this crawl cannot date is
        # always kept (`age_unknown`), never silently dropped, and counted
        # separately from the ones actually filtered by age
        # (`filtered_by_age`) so an operator can tell the two apart.
        # Returning here — before the cTag check and `_route_collection` —
        # means a filtered item leaves no cTag behind and never touches
        # `ctags`/the delta cursor beyond the delta page itself simply
        # having been walked; a later run only re-offers it if it changes
        # again, exactly the same as any other item this crawl never saw.
        modified_at = _item_modified_at(item)
        if modified_at is None:
            stats.add(age_unknown=1)
        else:
            cutoff = datetime(ctx.min_modified.year, ctx.min_modified.month, ctx.min_modified.day, tzinfo=timezone.utc)
            if modified_at.astimezone(timezone.utc) < cutoff:
                stats.add(filtered_by_age=1)
                return

    collection_id = _route_collection(path, target.drive_id, ctx)

    ctag = item.get("cTag") or item.get("eTag")
    with _state_lock:
        # `ctx.legacy_ctags` (empty on the inline path) is a READ-ONLY seed
        # from the connection-level state, consulted only when THIS shard's
        # own row has no entry yet — see `_ScopeContext.legacy_ctags`'s
        # docstring. Never written back to; a success below still only ever
        # updates the ACTIVE `ctags` (this target's own state).
        seen_ctag = ctags.get(stable_id) or ctx.legacy_ctags.get(stable_id)
        already = bool(ctag) and seen_ctag == ctag and not force_reprocess
    if already:
        # Content is unchanged (the cTag/eTag still matches), but the item
        # may have been RENAMED or MOVED within the same drive since we last
        # saw it — Graph keeps the cTag stable across a rename, so without
        # this check the stored `corpus_files.path`/`filename` would go
        # stale forever (D.18). One indexed lookup against the already-
        # ingested row, never a download/convert/re-ingest — see
        # `_Ingestor.rename`. Compares against the SAME `(path, filename)`
        # shape `_prepare_document`'s "ok" branch would have stored: the raw
        # drive-relative path + `<stem>.md` for a plain scope, or the
        # anonymized equivalent (`_anonymize_identity` — cheap and
        # size-independent, not a re-convert) for an anonymize-marked one.
        try:
            if ctx.anonymize:
                rename_path, rename_filename = (
                    _anonymize_identity(path, name, key=anonymization_key, detector=detector)
                    if anonymization_key is not None
                    else (None, None)
                )
            else:
                rename_path, rename_filename = path, f"{Path(name).stem or name}.md"
            renamed = (
                await _run_blocking(
                    pool,
                    ingestor.rename,
                    collection_id=collection_id,
                    stable_id=stable_id,
                    path=rename_path,
                    filename=rename_filename,
                )
                if rename_path is not None
                else False
            )
        except Exception as exc:  # noqa: BLE001 — a rename-detection fault must never break an
            # otherwise-fine item: its cTag/state is untouched here, so it is
            # simply re-checked next run. Falling through to "unchanged"
            # costs nothing but one skipped rename this run.
            logger.warning("sharepoint crawl: rename check failed for %s: %s", path, exc)
            renamed = False
        if renamed:
            stats.add(renamed=1)
        else:
            stats.add(unchanged=1)
        return

    # 2026-09-04 finding #66 item 2 — a document that has already failed
    # DETERMINISTICALLY enough times is skipped right here, before the
    # download this whole function exists to pay for. See
    # `_doomed_skip_reason`'s own docstring for the exact conditions.
    doomed = _doomed_skip_reason(state, stable_id, item=item, force_reprocess=force_reprocess)
    if doomed is not None:
        doomed_reason_type, doomed_reason = doomed
        stats.note_skipped_doomed(
            path=None if ctx.anonymize else path,
            item_id=str(item.get("id") or ""),
            drive_id=target.drive_id,
            reason=doomed_reason,
            reason_type=doomed_reason_type,
            suffix=Path(name).suffix.lower(),
            error_class=str((state.get("failed_items") or {}).get(stable_id, {}).get("error_class") or ""),
        )
        logger.info("sharepoint crawl: skipping %s — doomed (%s), no download attempted", path, doomed_reason)
        return

    size = int(item.get("size") or 0)
    if max_file_mb and size > _max_file_bytes(max_file_mb):
        stats.note_oversize(path, size)
        logger.info("sharepoint crawl: skipping %s — %s over the %dMB cap", path, human_bytes(size), max_file_mb)
        return

    name_suffix = Path(name).suffix.lower()
    if name_suffix.lstrip(".") in _unsupported_extensions():
        # Known-in-advance dead end for the converter (see
        # `_DEFAULT_UNSUPPORTED_EXTENSIONS`) — classified BEFORE the download
        # that `convert_unsupported` below still pays for, so a media/BI/
        # code file never enters the download queue OR the `failed_items`
        # retry backlog (it would otherwise fail `markitdown` identically on
        # every future run, forever). Same counters `convert_unsupported`
        # uses, never `errors`/`convert_failed`.
        stats.note_skipped_unsupported(
            path=None if ctx.anonymize else path,
            item_id=str(item.get("id") or ""),
            drive_id=target.drive_id,
            reason="unsupported file type — skipped before download",
            suffix=name_suffix,
        )
        logger.info("sharepoint crawl: skipping %s — unsupported file type, no download attempted", path)
        return

    mime = str((item.get("file") or {}).get("mimeType") or "")
    # In flight from here to the end of the function — the download/convert/
    # ingest span, i.e. the part slow enough to be worth SHOWING an admin
    # watching live (owner-frustration fix, 2026-09-01). `outcome_label`
    # names how it ended for the `activity.recent` list; the `finally`
    # guarantees exactly one `exit_item_activity` per `enter_item_activity`,
    # whatever branch below returns or raises.
    activity_token = stats.enter_item_activity(path)
    outcome_label = "error"
    #: Which conversion-rescue-chain rung succeeded for THIS item, if any —
    #: see `_PreparedDocument.rescue`. Set once the "ok" outcome is known,
    #: read by the `finally` below for `activity.recent`'s per-file detail.
    rescue_label = ""
    try:
        try:
            tmp_path = await transport.download_to_temp(
                target.drive_id, str(item["id"]), name, max_bytes=_max_file_bytes(max_file_mb)
            )
        except GraphThrottled:
            # NOT a per-file fault, despite being a CrawlError: the tenant is
            # throttling this app registration as a whole, so absorbing it
            # here would turn "back off" into "keep hammering, one 429
            # budget per file". Aborts the run; the next one resumes from
            # the persisted deltaLink + cTags. Under concurrency the page
            # driver also stops feeding the pool the moment this escapes,
            # so a 429 storm costs one budget per IN-FLIGHT item, never one
            # per remaining file.
            outcome_label = "throttled"
            raise
        except (CrawlError, SharePointGraphError, httpx.HTTPError) as exc:
            # `SharePointGraphError.status_code` is the upstream HTTP status,
            # documented safe to log (Entra/Graph error bodies never carry a
            # credential); the other two exception types carry no status.
            status_code = getattr(exc, "status_code", None)
            detail = str(exc)
            stats.add(errors=1)
            stats.note_error(path, "download_failed", detail=detail, status_code=status_code)
            outcome_label = "download_failed"
            logger.warning(
                "sharepoint crawl: download failed for %s: %s status=%s detail=%s",
                path,
                type(exc).__name__,
                status_code,
                detail,
            )
            _note_retry(
                state,
                stats,
                stable_id,
                target=target,
                item=item,
                path=path,
                error_class=ERROR_CLASS_DOWNLOAD_ERROR,
                detail=detail,
            )
            return

        try:
            prepared: _PreparedDocument = await _run_blocking(
                pool,
                _prepare_document,
                tmp_path,
                mime=mime,
                path=path,
                name=name,
                anonymize=ctx.anonymize,
                anonymization_key=anonymization_key,
                detector=detector,
                convert_pool=convert_pool,
                convert_slot=convert_slot,
            )
        finally:
            # The local copy never persists — success, skip, or failure.
            tmp_path.unlink(missing_ok=True)

        # `path` for the operator-facing `failed_items`/`skipped_items` lists
        # below — never the raw drive-relative path for an anonymize-marked
        # scope, mirroring the same rule `_convert_failure_detail` already
        # applies to the `reason` text (only the exception TYPE survives for
        # that scope, never the message). `item_id`/`drive_id` are opaque
        # Graph identifiers, not document content, so they are kept
        # regardless — they are what a later `retry_failed` run needs.
        record_path = None if ctx.anonymize else path
        item_id = str(item.get("id") or "")
        suffix = Path(name).suffix.lower()
        if prepared.outcome == "convert_failed":
            stats.add(convert_failed=1, errors=1)
            stats.note_error(path, "convert_failed", detail=prepared.detail)
            stats.note_failed_item(
                path=record_path,
                item_id=item_id,
                drive_id=target.drive_id,
                reason_type="convert_failed",
                reason=prepared.detail,
                suffix=suffix,
            )
            outcome_label = "convert_failed"
            _note_retry(
                state,
                stats,
                stable_id,
                target=target,
                item=item,
                path=path,
                error_class=prepared.error_class or ERROR_CLASS_OTHER,
                detail=prepared.detail,
            )
            return
        if prepared.outcome == "convert_empty":
            # Not a failure to retry ON THE ORDINARY per-run backlog: the
            # document converted fine and, while scan OCR is off, running it
            # through the pipeline again cannot change the answer. Unlike
            # the other three outcomes here, `_note_retry` is NOT called.
            # Still recorded in `failed_items` (2026-09-02 owner decision:
            # visibility for "why is this document missing" must not depend
            # on the retry queue) — never bumps `errors` — AND in the
            # PERSISTED `empty_items` backlog (`_note_empty`), so an admin
            # who later turns scan OCR on has something to target with
            # `retry_empty` instead of a full resync.
            stats.add(convert_failed=1)
            stats.note_failed_item(
                path=record_path,
                item_id=item_id,
                drive_id=target.drive_id,
                reason_type="convert_empty",
                reason="conversion succeeded but produced no extractable text",
                suffix=suffix,
            )
            _note_empty(state, stable_id, target=target, item=item, path=path)
            outcome_label = "convert_empty"
            return
        if prepared.outcome == "convert_unsupported":
            # No backend was even attempted — never an error, never a
            # retry candidate (see `CrawlStats.skipped_unsupported`'s
            # docstring). Listed separately from `failed_items` so a reader
            # never has to inspect a reason string to tell "we didn't try"
            # from "we tried and it failed".
            stats.note_skipped_unsupported(
                path=record_path,
                item_id=item_id,
                drive_id=target.drive_id,
                reason=prepared.detail,
                suffix=suffix,
            )
            outcome_label = "convert_unsupported"
            return
        if prepared.outcome == "anonymize_failed":
            stats.add(anonymize_failed=1)
            outcome_label = "anonymize_failed"
            _note_retry(
                state,
                stats,
                stable_id,
                target=target,
                item=item,
                path=path,
                error_class=ERROR_CLASS_OTHER,
                detail="anonymization failed",
            )
            return

        # This item converted cleanly enough to reach ingest — record which
        # rescue-chain rung got it there, if any (`""` is the ordinary,
        # no-rescue case and bumps nothing). `rescue_label` also feeds the
        # `finally` below's `activity.recent` per-file detail.
        rescue_label = prepared.rescue
        if rescue_label:
            stats.note_conversion_rescue(rescue_label)

        try:
            _file_id, was_new = await _run_blocking(
                pool,
                _ingest_with_retry,
                ingestor,
                collection_id=collection_id,
                stable_id=stable_id,
                # RESOLVED values from `_prepare_document` — the real
                # path/name for a plain scope, the anonymized ones for a
                # marked scope. NOT the raw `path`/`name` locals above:
                # those are for routing and logging only, and must never
                # reach storage for a marked scope.
                path=prepared.path,
                filename=prepared.filename,
                markdown=prepared.markdown,
                source_sha256=prepared.source_sha256,
            )
        except Exception as exc:  # noqa: BLE001 — one file's ingest, not the run
            status_code = getattr(exc, "status_code", None)
            detail = str(exc)
            stats.add(errors=1)
            stats.note_error(path, "ingest_failed", detail=detail, status_code=status_code)
            outcome_label = "ingest_failed"
            logger.warning(
                "sharepoint crawl: ingest failed for %s: %s status=%s detail=%s",
                path,
                type(exc).__name__,
                status_code,
                detail,
            )
            _note_retry(
                state,
                stats,
                stable_id,
                target=target,
                item=item,
                path=path,
                error_class=ERROR_CLASS_INGEST_ERROR,
                detail=detail,
                content_sha=prepared.source_sha256,
            )
            return

        outcome_label = "new" if was_new else "changed"
        if was_new:
            stats.add(new=1)
        else:
            stats.add(changed=1)
        # Written only AFTER the document is durably ingested: a cTag
        # recorded before the ingest would make a resumed run skip a file it
        # never landed. Per ITEM, not per page — a slow neighbour in the
        # same page must not hold this one's cTag hostage — but always
        # under the state lock, so it can never land inside a `save_state`
        # serialization.
        if ctag:
            with _state_lock:
                ctags[stable_id] = ctag
        # However it got here — a normal delta row or a queued retry — it
        # just ingested cleanly, so it owes the failure queue nothing more.
        if _clear_retry(state, stable_id):
            stats.add(item_retry_recovered=1)
        # Same for the empty-document backlog: this item just produced real
        # text (a source edit, or scan OCR turning it up on a `retry_empty`
        # replay), so it is no longer a `convert_empty` candidate.
        _clear_empty(state, stable_id)
    finally:
        stats.exit_item_activity(activity_token, path, outcome_label, rescue=rescue_label)


class _ConcurrencyGovernor:
    """The in-flight target, and the AIMD that moves it when Graph pushes back.

    Multiplicative decrease, additive increase, evaluated ONCE per delta page
    — the boundary where the crawl is already quiescent, so a change of
    target never splits a page across two policies:

    * a page that met a THROTTLE BURST (more than
      :data:`_THROTTLE_BURST_429S` throttled responses, or more than
      :data:`_THROTTLE_BURST_WAIT_S` of honored ``Retry-After``) halves the
      target, floor 1;
    * a clean page adds 1 back, ceiling the configured cap.

    A downshift NEVER aborts anything: the run keeps going, more slowly. The
    hard stop stays where it was — the per-request 429 budget raising
    :class:`GraphThrottled`. Backing off and giving up are different answers
    and the tenant is telling us the first one.

    Adaptive only when the cap is > 1: an operator who pinned concurrency to
    1 asked for the sequential crawl, not for a governor that agrees with them.
    """

    def __init__(
        self,
        cap: int,
        *,
        stats: Optional[CrawlStats] = None,
        burst_429s: int = _THROTTLE_BURST_429S,
        burst_wait_s: float = _THROTTLE_BURST_WAIT_S,
    ) -> None:
        self.cap = max(1, int(cap))
        self.target = self.cap
        self.adaptive = self.cap > 1
        self.burst_429s = burst_429s
        self.burst_wait_s = burst_wait_s
        self.downshifts = 0
        self.floor_hit = False
        self._stats = stats
        # Its own lock, not the stats one: the target is read on the event
        # loop at every page start and could be written from anywhere a
        # future caller decides to observe from.
        self._lock = threading.Lock()
        if stats is not None:
            stats.note_concurrency(target=self.target)

    def current(self) -> int:
        with self._lock:
            return self.target

    def observe_page(self, throttled_429s: int, throttle_wait_s: float) -> int:
        """Fold ONE page's throttling into the target; return the new target.

        Arguments are the page's own deltas (see
        :meth:`CrawlStats.throttle_snapshot`), never running totals — a
        cumulative count would keep halving forever after a single bad page.
        """
        if not self.adaptive:
            return self.current()
        burst = throttled_429s > self.burst_429s or throttle_wait_s > self.burst_wait_s
        with self._lock:
            before = self.target
            if burst:
                self.target = max(1, self.target // 2)
                if self.target < before:
                    self.downshifts += 1
                if self.target == 1:
                    self.floor_hit = True
            else:
                self.target = min(self.cap, self.target + 1)
            target, downshifted = self.target, burst and self.target < before
        if self._stats is not None:
            self._stats.note_concurrency(target=target, downshift=downshifted)
        if downshifted:
            logger.info(
                "sharepoint crawl: %d throttled responses in one page (%.0fs waited) — "
                "halving in-flight target to %d (cap %d)",
                throttled_429s,
                throttle_wait_s,
                target,
                self.cap,
            )
        return target


#: How severely an exception escaping one item's pipeline ends the run.
#: Higher wins when several workers fail in the same page — the run reports
#: the WORST thing that happened to it, never the first one to be noticed.
#: A tenant-wide throttle outranks the clock: "we ran out of time" invites a
#: retry, "the tenant is refusing us" is what an operator has to act on. An
#: admin-requested stop outranks GraphGone (a routine 410-resync trigger,
#: never a run-ending fault on its own) for the same reason a timeout does:
#: it is a deliberate, named exit, not an incidental one.
_ABORT_SEVERITY: Tuple[type, ...] = (GraphThrottled, CrawlTimeout, CrawlStopped, GraphGone)


def _abort_rank(exc: BaseException) -> int:
    for rank, kind in enumerate(_ABORT_SEVERITY):
        if isinstance(exc, kind):
            return len(_ABORT_SEVERITY) - rank
    return 0


async def _process_page(
    items: Sequence[Dict[str, Any]],
    *,
    target: DriveTarget,
    ctx: _ScopeContext,
    transport: GraphTransport,
    ingestor: _Ingestor,
    state: Dict[str, Any],
    stats: CrawlStats,
    max_file_mb: int,
    anonymization_key: Optional[bytes],
    detector: Any = None,
    deadline: Optional[_Deadline] = None,
    concurrency: int = 1,
    stop_watcher: Optional["_StopWatcher"] = None,
    force_reprocess: bool = False,
    convert_pool: Optional[_ConvertProcessPool] = None,
    recorder: Optional["_RunRecorder"] = None,
) -> None:
    """Run ONE delta page's rows, up to ``concurrency`` items at a time.

    The page is the unit of the RESUME contract, and this function is what
    keeps that true under parallelism:

    * every item's cTag is still written by the item itself, right after its
      own durable ingest — a slow neighbour cannot delay it, and a fast
      neighbour cannot claim it;
    * this function does not return until every worker has finished, so the
      caller's ``deltaLink`` persist + the unconditional recorder checkpoint
      it makes afterward still happen after *all* of the page's rows, never
      in the middle of it;
    * the deadline is re-checked before each item is picked up, so an expired
      budget stops FEEDING the pool and then drains it, rather than starting
      work it has no time to finish;
    * an exception in one item stops further pickups and is re-raised only
      after the in-flight ones are done — their completed work is already
      recorded (counters, cTags) and is not thrown away.

    ``concurrency <= 1`` takes the sequential branch: the same loop, the same
    inline calls and the same ordering the crawl had before this existed, so
    "1 == today's behaviour" is a property of the code, not a hope.

    ``convert_pool`` (see its class docstring) supplies one dedicated
    converter PROCESS per item-concurrency slot, ``0..workers-1``. The
    sequential branch has exactly one slot (0) and no sibling to fall back
    on, so it repairs that slot before every item — always safe here, since
    this branch never creates a thread pool at all. The parallel branch
    instead repairs once, at the PAGE boundary in :func:`_crawl_drive` (after
    this function's own thread pool below has been joined): repairing a
    slot mid-page would fork while its siblings' worker threads are still
    live, which is exactly the hazard the pool's docstring warns about. A
    slot that crashes mid-page therefore stays down for the REST of that
    page — every item it draws counts as ``convert_failed`` until the next
    page's repair — while the other slots keep converting normally; those
    files are not lost, only deferred (no cTag is written for a
    ``convert_failed`` item, so the next crawl retries them).
    """
    workers = max(1, int(concurrency))
    if workers == 1:
        for item in items:
            stats.add(items_seen=1)
            # Between files. The cTag of the item just finished is already in
            # `state`; the page's deltaLink is not written until the page
            # completes, so a stop here re-reads this page next run and every
            # already-ingested item in it upserts to a no-op.
            if deadline is not None:
                deadline.check()
            # Always safe: this branch never creates a thread pool, so there
            # is never another thread alive to hand a held lock to.
            if convert_pool is not None:
                convert_pool.repair()
            # Pure bookkeeping, so the report says `max_in_flight: 1` here
            # rather than a 0 that reads as "nothing ever ran".
            stats.enter_item()
            started = time.monotonic()
            try:
                await _process_item(
                    item,
                    target=target,
                    ctx=ctx,
                    transport=transport,
                    ingestor=ingestor,
                    state=state,
                    stats=stats,
                    max_file_mb=max_file_mb,
                    anonymization_key=anonymization_key,
                    detector=detector,
                    force_reprocess=force_reprocess,
                    convert_pool=convert_pool,
                    convert_slot=0,
                )
            finally:
                stats.exit_item(time.monotonic() - started)
            stats.add(items_done=1)
            # Between files, cadence-limited (see `_StopWatcher`): a
            # completed item's cTag is already in `state`, so a stop here
            # is exactly as safe to resume from as the deadline check above.
            if stop_watcher is not None:
                stop_watcher.maybe_check_item_boundary(stats.items_done)
            if recorder is not None:
                recorder.maybe_checkpoint(stats)
        return

    if not items:
        return

    cursor = 0
    aborts: List[BaseException] = []
    # Bounded, and sized to the target the governor handed us — not the event
    # loop's default executor, whose min(32, cpu+4) ceiling would quietly cap
    # a configured concurrency above it. The `shutdown(wait=True)` below joins
    # every worker thread, a second and independent guarantee that no blocking
    # step of this page is still running when the caller persists the page's
    # deltaLink.
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sp-crawl")

    def _next_item() -> Optional[Dict[str, Any]]:
        # Single-threaded by construction: every worker below is a coroutine
        # on this run's event loop and there is no await between the read and
        # the write, so the index cannot be handed out twice.
        nonlocal cursor
        if aborts or cursor >= len(items):
            return None
        item = items[cursor]
        cursor += 1
        return item

    async def _worker(slot: int) -> None:
        while True:
            if deadline is not None:
                try:
                    deadline.check()
                except CrawlTimeout as exc:
                    aborts.append(exc)
                    return
            item = _next_item()
            if item is None:
                return
            stats.add(items_seen=1)
            stats.enter_item()
            started = time.monotonic()
            try:
                await _process_item(
                    item,
                    target=target,
                    ctx=ctx,
                    transport=transport,
                    ingestor=ingestor,
                    state=state,
                    stats=stats,
                    max_file_mb=max_file_mb,
                    anonymization_key=anonymization_key,
                    detector=detector,
                    pool=pool,
                    force_reprocess=force_reprocess,
                    convert_pool=convert_pool,
                    convert_slot=slot,
                )
            except BaseException as exc:  # noqa: BLE001 — re-raised after the drain
                # Anything escaping `_process_item` is by definition NOT a
                # per-file fault (those are counted inside it): a throttle
                # budget spent, the deadline, a dead deltaLink. Stop taking
                # new items; the drain below still lets the peers finish.
                aborts.append(exc)
                return
            finally:
                stats.exit_item(time.monotonic() - started)
            stats.add(items_done=1)
            # Same drain mechanism as the deadline check above and the
            # abort-on-exception path in the `try` block: appended to
            # `aborts` and returned, never raised past this loop, so
            # `_next_item()` stops handing out work to the OTHER workers too
            # and the in-flight ones still get to finish this iteration.
            if stop_watcher is not None:
                try:
                    stop_watcher.maybe_check_item_boundary(stats.items_done)
                except CrawlStopped as exc:
                    aborts.append(exc)
                    return
            if recorder is not None:
                recorder.maybe_checkpoint(stats)

    try:
        # `gather` without `return_exceptions` would cancel the peers on the
        # first failure — exactly the "one bad future loses the others'
        # completed work" this must not do. Every worker returns normally and
        # parks its exception in `aborts` instead. Each worker keeps the SAME
        # convert-pool slot (its position in this list) for the whole page —
        # see `convert_pool`'s docstring for why that 1:1 pairing is what
        # makes crash detection and repair unambiguous.
        await asyncio.gather(*[_worker(i) for i in range(min(workers, len(items)))])
    finally:
        pool.shutdown(wait=True)

    if aborts:
        raise max(aborts, key=_abort_rank)


def _sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


async def _retry_failed_items(
    target: DriveTarget,
    *,
    ctx: _ScopeContext,
    transport: GraphTransport,
    ingestor: _Ingestor,
    connection_id: str,
    state: Dict[str, Any],
    stats: CrawlStats,
    max_file_mb: int,
    anonymization_key: Optional[bytes],
    detector: Any,
    deadline: Optional[_Deadline],
    recorder: Optional["_RunRecorder"],
    convert_pool: Optional[_ConvertProcessPool] = None,
    include_given_up: bool = False,
    force_reprocess: bool = False,
    save_state_fn: Optional[Callable[[], None]] = None,
) -> None:
    """Replay every item THIS drive previously failed on, before asking
    Graph for what changed.

    This is the other half of the fix: Graph's delta feed only re-offers an
    item when it CHANGES, so a file that failed to download/convert/
    anonymize/ingest — and nobody has touched since — would otherwise never
    be handed to us again once its page's deltaLink moves past it. Each
    entry in ``failed_items`` carries the item dict as last seen, which is
    everything :func:`_process_item` needs to try it again; success clears
    the entry (:func:`_clear_retry`), a repeat failure bumps ``attempts``
    (:func:`_note_retry`) and re-queues it, and an item already at
    :data:`_MAX_ITEM_RETRY_ATTEMPTS` is skipped here — it stays recorded,
    just not retried every run.

    ``include_given_up`` (the admin-requested ``retry_failed`` run option,
    see :func:`run_builtin_crawl`) bypasses the ``given_up`` filter below —
    the cheap, surgical alternative to ``resync`` for a connection with a
    handful of permanently-stuck items: no full re-enumeration, just one
    more pass over exactly what this drive's own backlog already knows is
    broken, by the SAME item dict a normal run replays (never a fresh Graph
    metadata fetch — see the docstring above). A repeat failure counts
    normally (``_note_retry`` leaves ``given_up`` set; it does not re-trip
    the already-crossed bound), so an item that is still broken after an
    operator's retry still reads as given-up, honestly.

    Sequential and outside the page/concurrency machinery on purpose: the
    backlog is normally tiny (persistently-failing files, not a fresh
    page), and giving it its own governor/pool would buy nothing but risk
    for a path this rarely used.

    ``save_state_fn`` (optional) persists ``state`` in place of the module's
    own :func:`save_state` — the shard-crawl seam (2026-09-03 auto-parallel-
    crawl design §4.2): a shard child writes its OWN per-delta-unit state row
    (``kind='crawl:<state_key>'``), never the whole-connection row. ``None``
    (every caller before sharding existed) keeps today's behaviour exactly.

    ``force_reprocess`` (this run's own operator override) is threaded
    straight through to every :func:`_process_item` call below — the only
    thing it does INSIDE that function that a backlog-replay item could ever
    reach is bypass the doomed-item skip (2026-09-04 finding #66 item 2, and
    its TCRD-296 gap #74 extension to a repeated crash/timeout — see
    :func:`_doomed_skip_reason`); a failed item never had a matching
    ``ctags`` entry in the first place, so
    the cTag-equality "already ingested" skip it also bypasses was never
    reachable from here regardless.
    """
    _save = save_state_fn or (lambda: save_state(connection_id, state))
    failed_items: Dict[str, Any] = state.setdefault("failed_items", {})
    pending = [
        (stable_id, entry)
        for stable_id, entry in failed_items.items()
        if entry.get("state_key") == target.state_key
        and isinstance(entry.get("item"), dict)
        and (include_given_up or not entry.get("given_up"))
    ]
    if not pending:
        return
    for _stable_id, entry in pending:
        if deadline is not None:
            deadline.check()
        # Same repair the sequential page path does before each item, and safe
        # for the same reason: this loop never creates a thread pool, so no
        # other thread can be holding the lock a fork would copy. Without the
        # pool threaded through here at all, a retry converted INLINE — outside
        # the child-process isolation, outside its RLIMIT_AS ceiling and
        # outside recycling — so the one file that timed out could stall every
        # later crawl, which is the failure this backlog exists to end
        # (Devin Review on #2058).
        if convert_pool is not None:
            convert_pool.repair()
        stats.add(items_seen=1)
        stats.enter_item()
        started = time.monotonic()
        try:
            await _process_item(
                entry["item"],
                target=target,
                ctx=ctx,
                transport=transport,
                ingestor=ingestor,
                state=state,
                stats=stats,
                max_file_mb=max_file_mb,
                anonymization_key=anonymization_key,
                detector=detector,
                convert_pool=convert_pool,
                convert_slot=0,
                force_reprocess=force_reprocess,
            )
        finally:
            stats.exit_item(time.monotonic() - started)
        stats.add(items_done=1)
    with _state_lock:
        _save()
    if recorder is not None:
        recorder.checkpoint(stats)


async def _retry_empty_items(
    target: DriveTarget,
    *,
    ctx: _ScopeContext,
    transport: GraphTransport,
    ingestor: _Ingestor,
    connection_id: str,
    state: Dict[str, Any],
    stats: CrawlStats,
    max_file_mb: int,
    anonymization_key: Optional[bytes],
    detector: Any,
    deadline: Optional[_Deadline],
    recorder: Optional["_RunRecorder"],
    convert_pool: Optional[_ConvertProcessPool] = None,
    run: bool = False,
    save_state_fn: Optional[Callable[[], None]] = None,
) -> None:
    """Replay every item THIS drive previously converted to ``convert_empty``
    — the targeted counterpart to :func:`_retry_failed_items`, for the
    ``retry_empty`` admin action (``POST …/extraction/retry-empty``, see
    ``app/api/admin_sharepoint.py``) rather than an ordinary run.

    ``run`` defaults to ``False`` and is the whole reason this is a SEPARATE
    function rather than a branch inside :func:`_retry_failed_items`: the
    empty-document backlog is routinely thousands of items on a real corpus
    (leases, tax returns, scans — see ``src/ingest/scan_ocr.py``),
    and replaying it on every ordinary crawl would burn a full re-walk of
    that backlog for no reason while scan OCR stays off. Only an explicit
    ``retry_empty`` run (which is exactly what turning scan OCR on and
    wanting the backlog reconsidered looks like) sets it.

    Same item-dict replay contract as :func:`_retry_failed_items`: each
    entry carries the item AS SEEN when it last converted empty, so a retry
    costs one re-download/re-convert, not a fresh Graph metadata round trip.
    A document that STILL converts empty (scan OCR still off, or the scan
    still yields nothing) re-records itself via :func:`_note_empty` inside
    :func:`_process_item` exactly as an ordinary crawl would; one that
    finally produces text is cleared (:func:`_clear_empty`) and ingested.

    ``save_state_fn`` — see :func:`_retry_failed_items`'s docstring; same
    shard-crawl seam, same default.
    """
    if not run:
        return
    _save = save_state_fn or (lambda: save_state(connection_id, state))
    empty_items: Dict[str, Any] = state.setdefault("empty_items", {})
    pending = [
        (stable_id, entry)
        for stable_id, entry in empty_items.items()
        if entry.get("state_key") == target.state_key and isinstance(entry.get("item"), dict)
    ]
    if not pending:
        return
    for _stable_id, entry in pending:
        if deadline is not None:
            deadline.check()
        # Same repair as `_retry_failed_items` — see its own comment on this
        # exact line for why it is safe here too.
        if convert_pool is not None:
            convert_pool.repair()
        stats.add(items_seen=1)
        stats.enter_item()
        started = time.monotonic()
        try:
            await _process_item(
                entry["item"],
                target=target,
                ctx=ctx,
                transport=transport,
                ingestor=ingestor,
                state=state,
                stats=stats,
                max_file_mb=max_file_mb,
                anonymization_key=anonymization_key,
                detector=detector,
                convert_pool=convert_pool,
                convert_slot=0,
            )
        finally:
            stats.exit_item(time.monotonic() - started)
        stats.add(items_done=1)
    with _state_lock:
        _save()
    if recorder is not None:
        recorder.checkpoint(stats)


def _consume_replay_flag_once(
    state: Dict[str, Any], *, target: DriveTarget, job_id: Optional[str], flag: bool, marker_key: str
) -> bool:
    """Whether an admin-requested one-shot backlog-replay flag
    (``retry_failed``'s ``include_given_up``, ``retry_empty``'s ``run``)
    should still fire for THIS drive, in THIS job — the same "consume once
    per job" contract :func:`_apply_resync` gives ``resync`` (2026-09-04
    finding #66 item 4): a crash-recovery RECLAIM calls :func:`_crawl_drive`
    again with the byte-identical payload flag, and without this a
    reclaimed ``retry_failed``/``retry_empty`` run would redo the ENTIRE
    extra backlog pass on every reclaim rather than picking up past it. The
    ORDINARY pending-backlog replay (:func:`_retry_failed_items` runs on
    every crawl regardless of this flag) is UNAFFECTED — only the extra an
    explicit admin request adds is gated here.

    Keyed by ``target.state_key`` inside ``state`` (never a bare scalar):
    the inline path shares ONE ``state`` dict across every drive in the
    connection, and a scalar marker set by the first drive would wrongly
    suppress a SECOND drive's own first pass in the very same job.

    ``job_id`` is ``None`` for any caller this repo cannot identify as a
    dispatched job (a manual/test call) — always treated as fresh, the same
    conservative default :func:`_apply_resync` uses.
    """
    if not flag:
        return False
    if not job_id:
        return True
    marker: Dict[str, str] = state.setdefault(marker_key, {})
    if marker.get(target.state_key) == job_id:
        return False
    marker[target.state_key] = job_id
    return True


async def _crawl_drive(
    target: DriveTarget,
    *,
    ctx: _ScopeContext,
    transport: GraphTransport,
    ingestor: _Ingestor,
    connection_id: str,
    state: Dict[str, Any],
    stats: CrawlStats,
    max_file_mb: int,
    anonymization_key: Optional[bytes],
    recorder: Optional["_RunRecorder"] = None,
    detector: Any = None,
    deadline: Optional[_Deadline] = None,
    governor: Optional[_ConcurrencyGovernor] = None,
    stop_watcher: Optional["_StopWatcher"] = None,
    force_reprocess: bool = False,
    convert_pool: Optional[_ConvertProcessPool] = None,
    retry_failed: bool = False,
    retry_empty: bool = False,
    save_state_fn: Optional[Callable[[], None]] = None,
) -> None:
    """Delta-enumerate one drive (or one folder subtree), resuming from its
    persisted ``deltaLink``.

    ``retry_failed`` (the admin-requested run option, see
    :func:`run_builtin_crawl`) is passed straight to :func:`_retry_failed_items`
    as ``include_given_up`` — see that function's docstring. ``retry_empty``
    is the same shape for the ``convert_empty`` backlog — see
    :func:`_retry_empty_items`.

    ``recorder`` (optional, defaults to no recording) rides the checkpoint
    this function already writes — see :class:`_RunRecorder`.

    ``stop_watcher`` (optional) is checked unconditionally at every page
    boundary, same as ``deadline`` — see :class:`_StopWatcher`.

    ``governor`` supplies the WITHIN-PAGE item concurrency (see
    :func:`_process_page`) and is fed this drive's throttling at every page
    boundary, so a tenant pushing back shrinks the target for the pages that
    follow. Pages themselves remain strictly sequential: the next page's URL
    is only known once this page's response has been read, and its deltaLink
    must not be persisted before the current page's rows are on disk. Drives,
    likewise, remain sequential — see the note in :func:`_run_crawl_async`.

    ``force_reprocess`` (the operator "re-process everything" run option, see
    :func:`_run_crawl_async`) makes this drive start from the bare delta
    base regardless of a persisted ``deltaLink`` — Graph's delta feed only
    re-offers items that changed, so consulting the resume link would never
    even hand back an unchanged item for :func:`_process_page`'s own
    ``force_reprocess`` bypass to see. NEVER writes ``delta_links`` (or pops
    the entry) up front to get this behaviour — it is a bypass, not a
    reset: an interrupted forced run leaves the previous, still-valid
    resume link in place, exactly the same "don't destroy what a stop can't
    undo" contract the rest of this module keeps.

    ``convert_pool`` is repaired HERE, right after each page, not inside
    :func:`_process_page`: by the time a page returns its own
    item-concurrency thread pool (if any) has already been joined, which is
    exactly the single-threaded window a fork-based repair needs — see
    ``_ConvertProcessPool``'s docstring.

    ``save_state_fn`` (optional) persists ``state`` in place of the module's
    own :func:`save_state` — see :func:`_retry_failed_items`'s docstring for
    the shard-crawl seam this exists for. Threaded straight through to both
    retry helpers below, so this drive's backlog replays and its ordinary
    delta walk always land in the SAME row."""
    _save = save_state_fn or (lambda: save_state(connection_id, state))
    governor = governor or _ConcurrencyGovernor(1)
    delta_links: Dict[str, Any] = state["delta_links"]
    base = f"{target.delta_url}?$top={_DELTA_PAGE_SIZE}"
    url: Optional[str] = base if force_reprocess else (delta_links.get(target.state_key) or base)
    resynced = False
    stats.add(drives=1)
    # Consume `retry_failed`/`retry_empty` once per job (2026-09-04 finding
    # #66 item 4) — see `_consume_replay_flag_once`'s own docstring.
    job_id = recorder.job_id if recorder is not None else None
    effective_include_given_up = _consume_replay_flag_once(
        state, target=target, job_id=job_id, flag=retry_failed, marker_key="retry_failed_consumed_for_job"
    )
    effective_retry_empty = _consume_replay_flag_once(
        state, target=target, job_id=job_id, flag=retry_empty, marker_key="retry_empty_consumed_for_job"
    )
    # Retry this drive's OWN backlog first — see `_retry_failed_items`. It
    # runs before the first delta fetch so a resume that starts with a
    # 410 still gets the queued items a chance regardless.
    await _retry_failed_items(
        target,
        ctx=ctx,
        transport=transport,
        ingestor=ingestor,
        connection_id=connection_id,
        state=state,
        stats=stats,
        max_file_mb=max_file_mb,
        anonymization_key=anonymization_key,
        detector=detector,
        deadline=deadline,
        recorder=recorder,
        convert_pool=convert_pool,
        include_given_up=effective_include_given_up,
        force_reprocess=force_reprocess,
        save_state_fn=_save,
    )
    # The `convert_empty` counterpart — a no-op unless this run was an
    # explicit `retry_empty` request (see `_retry_empty_items`'s own
    # docstring for why it never runs on an ordinary crawl).
    await _retry_empty_items(
        target,
        ctx=ctx,
        transport=transport,
        ingestor=ingestor,
        connection_id=connection_id,
        state=state,
        stats=stats,
        max_file_mb=max_file_mb,
        anonymization_key=anonymization_key,
        detector=detector,
        deadline=deadline,
        recorder=recorder,
        convert_pool=convert_pool,
        run=effective_retry_empty,
        save_state_fn=_save,
    )
    # Where this page's throttle accounting starts. Taken BEFORE the delta
    # fetch, so a 429 storm on the page request itself counts as the tenant
    # pushing back too — and deliberately not reset by a 410 resync, so that
    # detour's throttling carries into the next observation instead of
    # vanishing.
    page_throttle_mark: Optional[Tuple[int, float]] = None

    while url:
        # Between pages: the previous page's rows are ingested and its
        # deltaLink/cTags are on disk, so stopping here costs nothing.
        if deadline is not None:
            deadline.check()
        if stop_watcher is not None:
            stop_watcher.check_page_boundary()
        if page_throttle_mark is None:
            page_throttle_mark = stats.throttle_snapshot()
        try:
            page = await transport.get_json(url)
        except GraphGone:
            # The deltaLink expired or was invalidated. Drop it WITHOUT
            # persisting it and restart this drive from a full enumeration.
            # Persisting a dead link is how change detection silently stops
            # forever; resyncing twice in one pass means something else is
            # wrong, so the second 410 propagates.
            if resynced:
                raise
            with _state_lock:
                delta_links.pop(target.state_key, None)
                _save()
            stats.add(delta_resyncs=1)
            resynced = True
            logger.info(
                "sharepoint crawl: 410 Gone — dropped dead deltaLink, full resync of %s",
                target.drive_name or target.drive_id,
            )
            url = base
            continue
        except SharePointGraphError as exc:
            if exc.status_code not in _PERMISSION_SKIP_STATUS_CODES:
                raise
            # App-only access across a real tenant is never uniform: a site
            # scope fans out to every library, and some of them legitimately
            # refuse this app registration. That is a fact to record and walk
            # past, not a reason to fail the whole connection's crawl — the
            # same posture `graph_client.search_folders` takes. Counted, never
            # silent: a drive nobody could read must be visible in the report.
            stats.add(permission_skips=1)
            logger.warning(
                "sharepoint crawl: HTTP %s on drive %s — skipping it (no access for this app registration)",
                exc.status_code,
                target.drive_name or target.drive_id,
            )
            return

        await _process_page(
            [item for item in page.get("value", []) if isinstance(item, dict) and item.get("id")],
            target=target,
            ctx=ctx,
            transport=transport,
            ingestor=ingestor,
            state=state,
            stats=stats,
            max_file_mb=max_file_mb,
            anonymization_key=anonymization_key,
            detector=detector,
            deadline=deadline,
            concurrency=governor.current(),
            stop_watcher=stop_watcher,
            force_reprocess=force_reprocess,
            convert_pool=convert_pool,
            recorder=recorder,
        )
        # `extraction.facts.stream_every` — a no-op page boundary check
        # when the knob is off (0, the default). See
        # `_maybe_stream_facts_extraction`'s docstring.
        _maybe_stream_facts_extraction(connection_id, stats)
        if convert_pool is not None:
            # Safe HERE: `_process_page` has already joined this page's own
            # thread pool (if it made one) before returning, so no other
            # thread is alive to hand a held lock to. See the class
            # docstring on `_ConvertProcessPool` and the note above.
            convert_pool.repair()
        # The page's OWN throttling, not the run's running total: the
        # governor folds a DELTA, so one bad page cannot keep halving the
        # target for the rest of the crawl.
        throttled_after, waited_after = stats.throttle_snapshot()
        governor.observe_page(throttled_after - page_throttle_mark[0], waited_after - page_throttle_mark[1])
        page_throttle_mark = None

        # Rows first, then the link: the deltaLink is persisted only after
        # everything the page produced is ingested and its cTags are on disk,
        # so a crash between the two costs re-work, never coverage.
        delta_link = page.get("@odata.deltaLink")
        if delta_link:
            # Under the state lock like every other mutation of `state`, even
            # though the pool is provably drained by here — the invariant is
            # enforced by the lock, not by an argument about who is running.
            with _state_lock:
                delta_links[target.state_key] = _require_graph_url(str(delta_link))
                _save()
            url = None
        else:
            next_link = page.get("@odata.nextLink")
            _save()
            url = _require_graph_url(str(next_link)) if next_link else None
        # The SAME state-checkpoint boundary, a second destination (design
        # §7.1) — and, unlike `_process_page`'s per-item `maybe_checkpoint`
        # calls above, UNCONDITIONAL: every page's final numbers are
        # durably recorded even if the rate limiter would otherwise have
        # withheld a write. Runs after the state file, so a recorder
        # failure can never cost the crawl its resume point.
        if recorder is not None:
            recorder.checkpoint(stats)


def _confirmed_scopes(connection: Dict[str, Any]) -> List[Dict[str, Any]]:
    """This connection's confirmed scope rows — the ones the wizard wrote a
    ``collection_id`` for. A scope with no collection has nowhere to route
    documents, so it is not crawlable."""
    scopes = (connection.get("config") or {}).get("scopes")
    if not isinstance(scopes, list):
        return []
    return [s for s in scopes if isinstance(s, dict) and s.get("source_scope_id") and s.get("collection_id")]


def _max_file_mb() -> int:
    from app.instance_config import get_value

    raw = get_value("extraction", "crawler", "max_file_mb", default=_DEFAULT_MAX_FILE_MB)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_FILE_MB


def _convert_recycle_after_docs() -> int:
    """``extraction.crawler.convert_recycle_after_docs`` — see
    :data:`_DEFAULT_CONVERT_RECYCLE_AFTER_DOCS` for why this exists. 0 (or
    negative, or unparseable) disables the document-count trigger; the RSS
    trigger, if configured, still applies."""
    from app.instance_config import get_value

    raw = get_value("extraction", "crawler", "convert_recycle_after_docs", default=_DEFAULT_CONVERT_RECYCLE_AFTER_DOCS)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_CONVERT_RECYCLE_AFTER_DOCS


def _convert_recycle_rss_bytes() -> int:
    """``extraction.crawler.convert_recycle_rss_mb``, resolved to bytes —
    see :data:`_DEFAULT_CONVERT_RECYCLE_RSS_MB`. 0 disables the RSS
    trigger; the document-count trigger, if configured, still applies."""
    from app.instance_config import get_value

    raw = get_value("extraction", "crawler", "convert_recycle_rss_mb", default=_DEFAULT_CONVERT_RECYCLE_RSS_MB)
    try:
        mb = max(0, int(raw))
    except (TypeError, ValueError):
        mb = _DEFAULT_CONVERT_RECYCLE_RSS_MB
    return mb * 1024 * 1024


def _convert_child_memory_limit_bytes() -> int:
    """``extraction.crawler.convert_child_memory_limit_mb``, resolved to
    bytes — see :data:`_DEFAULT_CONVERT_CHILD_MEMORY_LIMIT_MB`. 0 disables
    the per-child memory cap."""
    from app.instance_config import get_value

    raw = get_value(
        "extraction", "crawler", "convert_child_memory_limit_mb", default=_DEFAULT_CONVERT_CHILD_MEMORY_LIMIT_MB
    )
    try:
        mb = max(0, int(raw))
    except (TypeError, ValueError):
        mb = _DEFAULT_CONVERT_CHILD_MEMORY_LIMIT_MB
    return mb * 1024 * 1024


def _convert_child_max_rss_bytes() -> int:
    """``extraction.crawler.convert_child_max_rss_mb``, resolved to bytes —
    see :data:`_DEFAULT_CONVERT_CHILD_MAX_RSS_MB`. 0 disables the parent-side
    RSS watchdog outright."""
    from app.instance_config import get_value

    raw = get_value("extraction", "crawler", "convert_child_max_rss_mb", default=_DEFAULT_CONVERT_CHILD_MAX_RSS_MB)
    try:
        mb = max(0, int(raw))
    except (TypeError, ValueError):
        mb = _DEFAULT_CONVERT_CHILD_MAX_RSS_MB
    return mb * 1024 * 1024


def _convert_spares_per_slot() -> int:
    """``extraction.crawler.convert_spares_per_slot`` — see
    :data:`_DEFAULT_CONVERT_SPARES_PER_SLOT`. Clamped to
    ``[0, _MAX_CONVERT_SPARES_PER_SLOT]``; 0 (or negative, or unparseable)
    disables spares outright."""
    from app.instance_config import get_value

    raw = get_value("extraction", "crawler", "convert_spares_per_slot", default=_DEFAULT_CONVERT_SPARES_PER_SLOT)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_CONVERT_SPARES_PER_SLOT
    return max(0, min(_MAX_CONVERT_SPARES_PER_SLOT, value))


def _item_timeout_seconds() -> int:
    """``extraction.crawler.item_timeout_s`` — see
    :data:`_DEFAULT_ITEM_TIMEOUT_S`. 0 (or negative, or unparseable)
    disables the per-item bound — the pre-bound behaviour exactly."""
    from app.instance_config import get_value

    raw = get_value("extraction", "crawler", "item_timeout_s", default=_DEFAULT_ITEM_TIMEOUT_S)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_ITEM_TIMEOUT_S


def _max_converted_output_bytes() -> int:
    """``extraction.crawler.max_converted_mb``, resolved to bytes — see
    :data:`_DEFAULT_MAX_CONVERTED_MB`. 0 disables the cap."""
    from app.instance_config import get_value

    raw = get_value("extraction", "crawler", "max_converted_mb", default=_DEFAULT_MAX_CONVERTED_MB)
    try:
        mb = max(0, int(raw))
    except (TypeError, ValueError):
        mb = _DEFAULT_MAX_CONVERTED_MB
    return mb * 1024 * 1024


def _unsupported_extensions() -> frozenset[str]:
    """``extraction.crawler.unsupported_extensions`` — ADDITIVE to
    :data:`_DEFAULT_UNSUPPORTED_EXTENSIONS`, never a replacement: an admin
    can widen the pre-download skip list for a format this tenant's estate
    happens to be full of, but cannot narrow the base set — those are a
    dead end for markitdown regardless of instance. Each configured entry
    is lower-cased and has any leading dot stripped, so ``"mp4"`` and
    ``".mp4"`` both work; a value that is not a list, or an entry that is
    not a non-empty string, is ignored rather than raising — a bad config
    edit must not take a crawl down.
    """
    from app.instance_config import get_value

    raw = get_value("extraction", "crawler", "unsupported_extensions", default=[])
    extra: set[str] = set()
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, str) and entry.strip():
                extra.add(entry.strip().lower().lstrip("."))
    return _DEFAULT_UNSUPPORTED_EXTENSIONS | extra


def _crawl_concurrency() -> int:
    """``extraction.crawler.concurrency`` — how many items of ONE delta page
    the crawl pipelines at a time.

    Clamped to ``[1, _MAX_CONCURRENCY]``; a missing or unparseable value is
    the default. ``1`` is the pre-parallel behaviour exactly — the escape
    hatch for an operator whose tenant is throttling hard, and the setting
    the crawl's own golden test pins.

    NOT to be confused with its neighbour ``extraction.concurrency``
    (``app/worker/runtime.py::_extraction_concurrency``), which sizes the
    worker's extraction LANE — how many crawl jobs run at once. The two
    multiply: two concurrent crawls at 6 put twelve files in flight against
    the same tenant.
    """
    from app.instance_config import get_value

    raw = get_value("extraction", "crawler", "concurrency", default=_DEFAULT_CONCURRENCY)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_CONCURRENCY
    return max(1, min(_MAX_CONCURRENCY, value))


def _shard_target_docs() -> int:
    """``extraction.crawler.shard_target_docs`` (2026-09-03 auto-parallel-
    crawl design §4.8) — the ONE config knob the whole feature adds.

    Default :data:`_DEFAULT_SHARD_TARGET_DOCS` (5000); ``0`` — an explicit
    admin override, never the default — disables sharding entirely: every
    connection crawls inline, unconditionally, exactly as it did before
    this feature existed. A missing or unparseable value is the default,
    never a crash — same posture every other resolver in this module takes.
    """
    from app.instance_config import get_value

    raw = get_value("extraction", "crawler", "shard_target_docs", default=_DEFAULT_SHARD_TARGET_DOCS)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_SHARD_TARGET_DOCS


def _resolve_concurrency(override: Any) -> Tuple[int, int, str]:
    """``(effective_cap, configured, source)`` for one run.

    ``payload["concurrency"]`` overrides the configured value for THIS run
    only — the same shape ``payload["timeout_s"]`` already has — clamped to
    ``[1, _MAX_PAYLOAD_CONCURRENCY]``. An unparseable override is ignored in
    favour of config rather than guessed at: a typo in an ad-hoc payload must
    not silently re-tune the crawl.

    The resolved cap is then handed to :func:`_apply_memory_budget_cap`,
    which may lower it further (never raise it) against the container's own
    cgroup memory limit — see that function's docstring.
    """
    configured = _crawl_concurrency()
    if override is None:
        cap, source = configured, "config"
    else:
        try:
            requested = int(override)
        except (TypeError, ValueError):
            logger.warning("sharepoint crawl: ignoring unparseable payload concurrency %r — using config", override)
            cap, source = configured, "config"
        else:
            cap, source = max(1, min(_MAX_PAYLOAD_CONCURRENCY, requested)), "payload"
    cap, source = _apply_memory_budget_cap(cap, source)
    return cap, configured, source


def _timeout_seconds() -> int:
    """``extraction.timeout_s`` — the run's wall-clock bound.

    Delegates to ``app.worker.kinds._extraction_timeout_seconds``, the single
    existing reader of that key (it also derives the job's lease from it, so
    the two can never disagree about what the ceiling is). Imported lazily,
    same posture as :func:`_resolve_anonymization_key` below.
    """
    from app.worker.kinds import _extraction_timeout_seconds

    return _extraction_timeout_seconds()


#: ``extraction.anonymization.detector`` values. ``"regex"`` is the default
#: and it is a COST decision, not a legacy one: the deterministic tier is
#: free and runs over every document of every crawl, while ``"llm"`` sends
#: each document to the instance's model — order ~$5 per 1,000 documents on
#: the default Haiku-class model. An operator who wants the recall an LLM
#: reader adds opts into paying for it.
_DETECTOR_REGEX = "regex"
_DETECTOR_LLM = "llm"


def _entity_detector() -> Any:
    """The entity detector for this instance's anonymize passes, or ``None``
    for the anonymizer's own deterministic default.

    ``extraction.anonymization.detector: "llm"`` builds
    ``hybrid_detector(LLMDetector())`` from :mod:`src.anonymization_ner` —
    regex first (free, exact by construction), then the LLM adds what only a
    reader can find. That tier's ``DetectionUnavailable`` deliberately
    propagates: the caller counts the document in ``anonymize_failed`` and
    drops it, rather than ingesting a document whose redaction quietly
    degraded to regex-only. Anything else, including unset, is the regex
    tier — see :data:`_DETECTOR_REGEX` for why that is the default.

    Built ONCE per run (the detector carries its own client + running token
    accounting) and imported lazily, so an instance on the regex tier never
    imports the LLM stack at all.
    """
    from app.instance_config import get_value

    choice = str(get_value("extraction", "anonymization", "detector", default="") or "").strip().lower()
    if choice != _DETECTOR_LLM:
        return None

    from src.anonymization_ner import LLMDetector, hybrid_detector

    llm = LLMDetector()
    detect = hybrid_detector(llm)
    # Expose the LLM tier so the run recorder can read its token accounting
    # (`total_usage`) at finish time — function objects take attributes, and
    # this keeps the detector contract itself a plain callable. Best-effort:
    # a detector double that refuses attributes just loses usage reporting.
    try:
        detect.llm = llm  # type: ignore[attr-defined]
    except AttributeError:
        pass
    return detect


def _detector_usage(detector: Any) -> Dict[str, Any]:
    """The LLM tier's running token accounting for this run, or ``{}``.

    ``{}`` means "no tokens were spent" — the regex tier, or no anonymize
    scope in the run — which the UI keeps distinct from a computed $0.00.
    When tokens WERE spent, ``estimated_cost_usd`` is priced through
    ``src.llm_pricing.cost_usd`` — the one place a token count becomes USD
    (mirroring ``connectors.sharepoint.facts_extraction._Report.render``'s
    own pricing of its stage, the same field name and rounding). Never
    raises: usage is observability, not a gate."""
    llm = getattr(detector, "llm", None)
    usage = getattr(llm, "total_usage", None)
    if not isinstance(usage, dict):
        return {}
    out: Dict[str, Any] = {k: v for k, v in usage.items() if isinstance(v, (int, float)) and v}
    if not out:
        return {}
    model = getattr(llm, "model", None)
    if model:
        out["model"] = str(model)
        from src.llm_pricing import cost_usd

        out["estimated_cost_usd"] = round(
            cost_usd(
                model=str(model),
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
                cache_creation_tokens=int(usage.get("cache_creation_input_tokens") or 0),
            ),
            4,
        )
    return out


def _ocr_run_usage(scan_ocr_module: Any) -> Dict[str, Any]:
    """The scan-OCR tier's run totals, or ``{}`` — the same zero-collapse
    honesty as :func:`_detector_usage`: an all-zero block (the switch off,
    or no scanned document met) reads as "no tokens spent", never as a
    measured $0.00. Never raises: usage is observability, not a gate."""
    if scan_ocr_module is None:
        return {}
    try:
        usage = scan_ocr_module.run_usage()
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(usage, dict):
        return {}
    return {k: v for k, v in usage.items() if isinstance(v, (int, float)) and v}


def _ocr_triage_usage(scan_ocr_module: Any) -> Dict[str, Any]:
    """The scan-OCR triage DECISION counters (``previewed``/``continued``/
    ``stopped``/``pages_transcribed``/``stop_reasons``) for this run's
    report — distinct from :func:`_ocr_run_usage`'s token/call accounting.
    ``{}`` when triage never previewed a single document this run (the
    switch off, everything went through the untriaged ``full`` path, or the
    connector isn't installed) — never raises, same posture as its sibling."""
    if scan_ocr_module is None:
        return {}
    try:
        usage = scan_ocr_module.triage_run_usage()
    except Exception:  # noqa: BLE001
        return {}
    return usage if isinstance(usage, dict) and usage else {}


def _facts_stream_every() -> int:
    """``extraction.facts.stream_every`` — enqueue a standalone
    ``sharepoint-facts-extraction`` job for this connection after every N
    successfully ingested files during a live crawl, so the fact graph
    fills in WHILE a long crawl is still running rather than waiting for
    the chained tail pass below. 0 (the default) disables this entirely —
    the crawl's behaviour is then exactly what it was before this knob
    existed: only the chained tail pass runs, after the crawl finishes.
    Negative or unparseable values are treated as 0.

    Needs the worker's EXTRACTION lane (``app/worker/runtime.py::
    _extraction_concurrency`` / ``extraction.concurrency``) sized to at
    least 2 for a streamed pass to actually OVERLAP the crawl still
    running — at the default of 1, the lane has a single slot, so the
    standalone job just queues behind (or ahead of) the crawl and runs
    after it, same wall-clock ordering the chained pass would have given.
    """
    from app.instance_config import get_value

    raw = get_value("extraction", "facts", "stream_every", default=0)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


#: The job kind both the manual "run facts extraction now" trigger
#: (``app/api/admin_sharepoint.py::trigger_facts_extraction``) and this
#: module's streamed passes enqueue — one constant so a rename of either
#: side cannot drift them apart.
_FACTS_EXTRACTION_JOB_KIND = "sharepoint-facts-extraction"


def _enqueue_streamed_facts_pass(connection_id: str) -> None:
    """Enqueue one standalone ``sharepoint-facts-extraction`` job for this
    connection — the SAME job kind, enqueue call and idempotency key
    ``POST …/connections/{id}/facts-extract`` uses
    (``connectors.sharepoint.facts_extraction.facts_extraction_idempotency_key``,
    which the API's ``_facts_extraction_idempotency_key`` delegates to), so
    a pass this streams and a manual trigger (or another streamed pass
    already queued/running) can never both be in flight for the same
    connection at once — ``enqueue()``'s own idempotency dedup collapses
    onto whichever is already there.

    That collapse is the DESIRED behaviour for a fast crawl crossing
    several ``stream_every`` thresholds before a prior pass finishes — it
    is what keeps this from piling up one job per threshold — so it is
    logged at debug, never a warning. Also skips (debug) when the two
    facts-extraction switches (``extraction.facts.enabled``,
    ``facts.enabled``) are not both on — the same gate the manual trigger
    checks BEFORE enqueueing (``facts_extraction_readiness``, the API's
    ``_facts_extraction_readiness`` delegates to it too), so this never
    hands the worker a job that can only fail once claimed. Never raises:
    called from deep inside the crawl's own page loop, and a hiccup
    scheduling a bonus pass must not fail a crawl that is still ingesting
    real documents.

    ALSO skips (info, since this is the storm this check exists to stop —
    TCRD-296 synthesis F.25, gaps #25/#48) while a fleet-level
    ``provider_limit`` condition is active and still cooling down
    (``streamed_pass_suppressed_by_provider_limit`` — a workspace/usage-
    limit exhaustion, a saturated Vertex region×model quota bucket, or
    billing disabled). Before this check, a long crawl crossing many
    ``stream_every`` thresholds while the provider refuses every call kept
    enqueueing (and failing) a fresh pass every threshold — 161 failed job
    rows overnight in the incident this closes, with no single place
    saying "facts are paused because the provider refuses". The MANUAL
    trigger (``POST …/facts-extract``) is deliberately unaffected — see
    that function's own docstring.

    Both helpers are imported from the connector-level module, never from
    ``app.api.admin_sharepoint``: that API module binds
    ``source_connections_repo`` at import time, so importing it lazily
    from inside a crawl froze whatever factory was installed at that
    moment into it for the rest of the process (a test's fake, in the
    cross-test leak this fixed).
    """
    from connectors.sharepoint.facts_extraction import (
        enqueue_facts_extraction_passes,
        facts_extraction_readiness,
        streamed_pass_suppressed_by_provider_limit,
    )

    try:
        usable, _error = facts_extraction_readiness()
        if not usable:
            logger.debug(
                "sharepoint crawl: connection %s — extraction.facts.stream_every is set but facts extraction "
                "is not usable yet — not enqueueing a streamed pass",
                connection_id,
            )
            return
        condition = streamed_pass_suppressed_by_provider_limit()
        if condition is not None:
            logger.info(
                "sharepoint crawl: connection %s — a provider_limit condition (%s, provider=%s) is active — "
                "not enqueueing a streamed facts pass until it clears",
                connection_id,
                condition.get("reason"),
                condition.get("provider"),
            )
            return
        # TCRD-296 gap #67: fanned out into however many partitions the
        # current backlog justifies — a single job (today's shape) for the
        # common case, several for a backlog large enough to need them.
        jobs = enqueue_facts_extraction_passes(connection_id)
    except Exception as exc:  # noqa: BLE001 — a bonus pass must never fail the crawl
        logger.debug(
            "sharepoint crawl: connection %s — could not enqueue a streamed facts pass: %s", connection_id, exc
        )
        return
    if all(job.get("deduped") for job in jobs):
        logger.debug(
            "sharepoint crawl: connection %s — streamed facts pass already queued/running (job(s) %s), not piling up",
            connection_id,
            ", ".join(job.get("id", "") for job in jobs),
        )
    else:
        logger.info(
            "sharepoint crawl: connection %s — streamed facts-extraction job(s) %s enqueued "
            "(extraction.facts.stream_every, %d partition(s))",
            connection_id,
            ", ".join(job.get("id", "") for job in jobs),
            len(jobs),
        )


def _maybe_stream_facts_extraction(connection_id: str, stats: "CrawlStats", *, final: bool = False) -> None:
    """The crawl-side half of ``extraction.facts.stream_every``: called at
    every page boundary (``final=False``) and once more after this run's
    enumeration finishes (``final=True``, the tail flush — a remainder of
    ingested files smaller than the next full threshold must not wait for
    the chained pass below). A no-op whenever the knob is off (0, the
    default) — the crawl's behaviour is then unchanged from before this
    knob existed.
    """
    stream_every = _facts_stream_every()
    if stream_every <= 0:
        return
    if final or stats.facts_stream_due(stream_every):
        _enqueue_streamed_facts_pass(connection_id)


def _standalone_facts_pass_in_flight(connection_id: str) -> bool:
    """Whether a standalone ``sharepoint-facts-extraction`` job for this
    connection is currently ``queued`` or ``running`` — checked at the
    crawl's own CHAINED tail pass (:func:`maybe_run_facts_extraction`) so
    the two can never interleave on the same per-connection facts state
    (``connectors.sharepoint.facts_extraction``'s ``state_path``/
    ``load_state``/``save_state`` — both a streamed/manual standalone pass
    and the chained pass read-modify-write the SAME per-document
    "already up to date" ledger).

    No repository method exists for "jobs of this kind whose payload names
    this connection" (adding one would touch the frozen ``jobs``
    DuckDB↔Postgres pair for a query only this one caller needs — see
    CONTRIBUTING.md's dual-backend discipline), so this filters
    ``jobs_repo().list(kind=..., status=...)`` in Python instead — cheap,
    since one instance's total row count for this job kind is small.
    """
    from src.repositories import jobs_repo

    repo = jobs_repo()
    for status in ("queued", "running"):
        for job in repo.list(kind=_FACTS_EXTRACTION_JOB_KIND, status=status, limit=200):
            payload = job.get("payload_json") or {}
            if isinstance(payload, dict) and str(payload.get("connection_id")) == str(connection_id):
                return True
    return False


def maybe_run_facts_extraction(
    connection: Dict[str, Any],
    *,
    deadline: Optional[_Deadline] = None,
    stats: Optional["CrawlStats"] = None,
    recorder: Optional["_RunRecorder"] = None,
) -> Optional[Dict[str, Any]]:
    """Run the LLM fact-extraction stage over what this crawl just ingested,
    or return ``None`` when it is switched off.

    Delegates to ``connectors.sharepoint.facts_extraction`` — the walk, the
    prompt, the verbatim retry, the batching and the ingest all live there;
    this module owns only the seam. Imported lazily so an instance with
    ``extraction.facts.enabled`` off never imports the LLM stack, the same
    posture :func:`_entity_detector` takes for the anonymizer's own.

    Synchronous inside the async crawl, exactly like the per-file
    ``ingestor.ingest`` call: this whole coroutine owns its worker's
    EXTRACTION lane slot, and there is no concurrent work for an event loop
    to interleave.

    ``stats``/``recorder`` are this run's own — when both are given, this
    wires the pass's ``on_progress`` seam to :meth:`_RunRecorder.
    maybe_checkpoint_facts`, which is what makes a healthy multi-hour facts
    pass keep checkpointing instead of reading `stalled` the moment it
    outlasts the crawl phase's own cadence. Either left ``None`` (a caller
    with no crawl run to attach to, e.g. a standalone facts trigger) simply
    runs the pass without a liveness checkpoint of its own — never an
    error, since a caller with no run row has nowhere to write one.

    Returns ``None`` (WITHOUT even checking the enabled switches) when a
    standalone ``sharepoint-facts-extraction`` job for this connection — a
    streamed pass (``extraction.facts.stream_every``) or a manual trigger
    — is already queued or running: see
    :func:`_standalone_facts_pass_in_flight`. The two passes share the
    same per-document ledger and must never run concurrently for one
    connection.
    """
    connection_id = str(connection.get("id"))
    if _standalone_facts_pass_in_flight(connection_id):
        logger.info(
            "sharepoint crawl: connection %s — standalone pass in flight — skipping chained pass",
            connection_id,
        )
        return None

    from connectors.sharepoint.facts_extraction import maybe_run_after_crawl

    on_progress = None
    if stats is not None and recorder is not None:

        def on_progress(update: Dict[str, Any]) -> None:
            recorder.maybe_checkpoint_facts(
                stats,
                docs_done=int(update.get("docs_done") or 0),
                docs_total=int(update.get("docs_total") or 0),
                current_path=update.get("current_path"),
            )

    return maybe_run_after_crawl(connection, deadline=deadline, on_progress=on_progress)


def _resolve_anonymization_key(scopes: Sequence[Dict[str, Any]]) -> Optional[bytes]:
    """The per-instance anonymization HMAC key, or ``None`` when no scope in
    this run needs one.

    Delegates to ``app.worker.kinds._resolve_anonymization_key`` — the single
    existing owner of that resolution, including the allowlist check on the
    admin-writable env var NAME. Duplicating it here would duplicate that
    gate, which is exactly the kind of second copy a security control must
    not have. Imported lazily so this connector carries no import-time
    dependency on the worker module (which imports this one).
    """
    if not any(s.get("anonymize") for s in scopes):
        return None
    from app.worker.kinds import _resolve_anonymization_key as resolve

    return str(resolve()).encode("utf-8")


async def _run_crawl_async(
    connection: Dict[str, Any],
    *,
    only_scope_ids: Optional[Sequence[str]] = None,
    job_id: Optional[str] = None,
    timeout_s: Optional[float] = None,
    concurrency: Optional[int] = None,
    force_reprocess: bool = False,
    retry_failed: bool = False,
    retry_empty: bool = False,
    clear_stale_stop: bool = True,
) -> Dict[str, Any]:
    connection_id = str(connection["id"])
    # A stop requested for a PREVIOUS run (already finished, failed, or one
    # that never actually started) must never reach forward and kill this
    # one — clear it unconsumed, at the very start, before anything else.
    # Best-effort: a repo hiccup here must not block the run it is trying to
    # let start cleanly.
    #
    # ``clear_stale_stop=False`` (2026-09-03 auto-parallel-crawl design §4.3)
    # is a SHARD CHILD's own call: the cooperative stop flag is connection-
    # wide, and clearing it here would erase a stop an admin requested WHILE
    # this connection's parent run was busy planning/enqueuing siblings —
    # only the PARENT (planner) run owns this clear, once, per top-level
    # trigger.
    if clear_stale_stop:
        try:
            _clear_stale_stop(connection_id)
        except Exception as exc:  # noqa: BLE001 — never load-bearing
            logger.debug("sharepoint crawl: could not clear a stale stop flag for %s: %s", connection_id, exc)
    stop_watcher = _StopWatcher(connection_id)
    scopes = _confirmed_scopes(connection)
    if only_scope_ids:
        wanted = set(only_scope_ids)
        scopes = [s for s in scopes if str(s.get("source_scope_id")) in wanted]
    if not scopes:
        raise CrawlError(
            f"connection {connection_id!r} has no confirmed scope to crawl — "
            "confirm at least one scope in the SharePoint connect wizard first"
        )

    settings = resolve_sharepoint_settings(connection)
    # `min_modified` is resolved PER SCOPE, inside the loop below (TCRD-296
    # gap #80 — a scope's own filter, falling back to the connection-wide
    # default) — see `resolve_min_modified`'s own docstring.
    anonymization_key = _resolve_anonymization_key(scopes)
    # Built once per run, and only when something in this run will actually
    # anonymize — an instance on the regex tier never imports the LLM stack,
    # and an instance on the LLM tier never pays for a detector no scope uses.
    detector = _entity_detector() if anonymization_key is not None else None
    max_file_mb = _max_file_mb()
    cap, configured_concurrency, concurrency_source = _resolve_concurrency(concurrency)
    deadline = _Deadline(_timeout_seconds() if timeout_s is None else timeout_s)

    # Recorded on the stats object rather than threaded through `report()`:
    # the report is assembled in one place at the end of this function and
    # that block is deliberately left alone.
    stats = CrawlStats(
        concurrency=cap,
        concurrency_configured=configured_concurrency,
        concurrency_source=concurrency_source,
        concurrency_effective_max=cap,
        concurrency_min_target=cap,
    )
    # ONE governor for the whole run: the tenant throttles an app
    # registration, not a drive, so what one drive learns about backing off
    # must carry to the next.
    governor = _ConcurrencyGovernor(cap, stats=stats)
    # One dedicated converter process per concurrency slot, forked NOW —
    # before anything below creates a `ThreadPoolExecutor` — so `.start()`
    # runs in the single-threaded window its docstring requires. Sized to
    # the run's hard ceiling, not the governor's current (adaptive) target,
    # so a slot is always available for whatever concurrency a later page
    # actually uses. Recycled per `_ConvertProcessPool`'s own "RECYCLING"
    # section — resolved once, here, same as every other crawler.* knob.
    convert_pool = _ConvertProcessPool(
        cap,
        recycle_after_docs=_convert_recycle_after_docs(),
        recycle_rss_bytes=_convert_recycle_rss_bytes(),
        memory_limit_bytes=_convert_child_memory_limit_bytes(),
        timeout_s=_item_timeout_seconds(),
        max_output_bytes=_max_converted_output_bytes(),
        max_rss_bytes=_convert_child_max_rss_bytes(),
        spares_per_slot=_convert_spares_per_slot(),
    )
    convert_pool.start()
    auth = GraphAuth(
        acquire=lambda: graph_client.get_app_token(settings.tenant_id, settings.client_id, settings.private_key),
        stats=stats,
    )
    transport = GraphTransport(auth, stats)
    ingestor = _Ingestor()
    state = load_state(connection_id)
    scope_errors: List[Dict[str, Any]] = []
    # Opened BEFORE the first request so a run that dies in its first scope
    # is still a rendered row rather than a silence (design §4.3).
    recorder = _RunRecorder(connection_id, job_id=job_id)
    recorder.start()
    # The scan-OCR tier keeps module-level run totals (it is called from
    # deep inside the converter, which has no channel back to this run) —
    # zeroed here so a run's `ocr_usage` can never carry a predecessor's
    # spend. Import guarded: the module is import-light, but a broken
    # optional install must degrade to "no OCR accounting", not a dead crawl.
    try:
        from src.ingest import scan_ocr as _scan_ocr

        _scan_ocr.reset_run_usage()
    except Exception:  # noqa: BLE001
        _scan_ocr = None
    #: The LLM stage's own sub-report, or None when it is switched off.
    facts_report: Optional[Dict[str, Any]] = None

    try:
        try:
            for scope in scopes:
                source_scope_id = str(scope.get("source_scope_id"))
                try:
                    targets = await _drive_targets(transport, scope)
                    exclusions = await _excluded_path_prefixes(transport, scope)
                except (CrawlError, SharePointGraphError) as exc:
                    # One scope's misconfiguration (or one site's outage) must not
                    # cost the connection's other scopes their pass — the same
                    # per-unit failure isolation `acl_sync` applies per connection.
                    scope_errors.append({"scope": source_scope_id, "error": str(exc)})
                    stats.add(errors=1)
                    continue

                min_modified, _min_modified_source = resolve_min_modified(connection, scope=scope)
                ctx = _ScopeContext(
                    source_scope_id=source_scope_id,
                    collection_id=str(scope["collection_id"]),
                    anonymize=bool(scope.get("anonymize")),
                    exclusions=exclusions,
                    zone_routes_by_drive=_zone_routes_for_scope(connection, source_scope_id),
                    min_modified=min_modified,
                )
                stats.add(scopes=1)
                # Drives stay SEQUENTIAL, deliberately. Parallelising them is the
                # second axis and it is not worth its risk here: every drive
                # shares one state file whose per-drive deltaLink is the resume
                # contract, and the 410-resync path mutates that file mid-drive;
                # the 429 budget and the deadline are likewise run-wide, so a
                # second axis mostly converts into 429s against the same tenant
                # rather than into throughput. In-page concurrency already
                # saturates a 200-row page. Correctness beats the second axis.
                #
                # `_crawl_targets` is the shared body with a shard child's own
                # crawl (2026-09-03 auto-parallel-crawl design, Task 1/4) — on
                # THIS, the inline (whole-connection) path, every target
                # shares the SAME connection-wide `state` dict and the SAME
                # `save_state`, exactly today's behaviour; `state_for`/
                # `save_for` ignore the target they are handed.
                await _crawl_targets(
                    connection_id,
                    targets_by_scope=[(ctx, targets)],
                    transport=transport,
                    ingestor=ingestor,
                    stats=stats,
                    max_file_mb=max_file_mb,
                    anonymization_key=anonymization_key,
                    recorder=recorder,
                    detector=detector,
                    deadline=deadline,
                    governor=governor,
                    stop_watcher=stop_watcher,
                    convert_pool=convert_pool,
                    force_reprocess=force_reprocess,
                    retry_failed=retry_failed,
                    retry_empty=retry_empty,
                    state_for=lambda _target: state,
                    save_for=lambda _target, _state: save_state(connection_id, _state),
                )
        finally:
            # Done converting for this run either way (success, a scope
            # error that propagated, a timeout, ...) — release the worker
            # processes before the (potentially long) facts stage below runs,
            # rather than leaving them idle for its whole duration.
            convert_pool.shutdown()

        # `extraction.facts.stream_every`'s tail flush — once more, right as
        # enumeration finishes, so a remainder of ingested files smaller than
        # the next full threshold is not left to wait for the chained pass
        # below (which may run much later, or be skipped entirely by it —
        # see `_standalone_facts_pass_in_flight`). A no-op when the knob is
        # off.
        _maybe_stream_facts_extraction(connection_id, stats, final=True)

        # ---- the LLM stage (owner decision 2026-09-01) -------------------
        # Chained HERE, not in the worker handler, for three reasons: it
        # needs this run's remaining `deadline`, its numbers belong in this
        # run's report and `extraction_runs` row, and it can only run over
        # documents this crawl has already ingested and indexed. Off unless
        # `extraction.facts.enabled` — `maybe_run_after_crawl` returns None
        # and this is a no-op. A hard stop inside it (no credential, model
        # unreachable, no ontology) propagates into the handler below and
        # records the run as FAILED, exactly like any other crash: the
        # crawl's own work is already durable, and the facts pass resumes
        # from its per-document state next run.
        facts_report = maybe_run_facts_extraction(connection, deadline=deadline, stats=stats, recorder=recorder)
    except BaseException as exc:
        # A crashed — or deliberately stopped — run still owes the operator
        # its numbers and its state: the rows are already ingested, so record
        # what got done instead of losing the pass. A timeout and a throttle
        # abort are not crashes, so each is NAMED in the report rather than
        # left looking like one (see `_STOP_REASONS`).
        reason = _stop_reason(exc)
        interrupted_report = stats.report(max_file_mb=max_file_mb, interrupted=True, interrupted_reason=reason)
        interrupted_report["connection_id"] = connection_id
        interrupted_report["scope_errors"] = scope_errors
        interrupted_report["retry_backlog"] = _retry_backlog_snapshot(state)
        state["last_run"] = interrupted_report
        save_state(connection_id, state)
        if reason != "error":
            logger.warning(
                "sharepoint crawl: connection %s stopped early (%s) after %d new / %d changed — "
                "state saved, the next run resumes",
                connection_id,
                reason,
                stats.new,
                stats.changed,
            )
        # Severity-first: a timeout is the sanctioned stop (records as
        # `interrupted`, reason "timeout"); anything else records via
        # `_record_status_for` so a crash can never dress up as benign.
        ner_usage = _detector_usage(detector)
        if ner_usage:
            interrupted_report["ner_usage"] = ner_usage
        ocr_usage = _ocr_run_usage(_scan_ocr)
        if ocr_usage:
            interrupted_report["ocr_usage"] = ocr_usage
        scan_ocr_triage = _ocr_triage_usage(_scan_ocr)
        if scan_ocr_triage:
            interrupted_report["scan_ocr"] = scan_ocr_triage
        stopped_usage: Dict[str, Any] = {}
        if ner_usage:
            stopped_usage["ner"] = ner_usage
        if ocr_usage:
            stopped_usage["ocr"] = ocr_usage
        recorder.finish(
            stats,
            status="interrupted" if reason in {r for _, r in _STOP_REASONS} else _record_status_for(exc),
            report=interrupted_report,
            error=f"{type(exc).__name__}: {exc}",
            usage=stopped_usage,
        )
        raise

    report = stats.report(max_file_mb=max_file_mb)
    report["connection_id"] = connection_id
    report["scope_errors"] = scope_errors
    report["retry_backlog"] = _retry_backlog_snapshot(state)
    # Both LLM stages' numbers ride in the SAME report and the SAME
    # `extraction_runs` row: one run, one set of counters. `ner_usage` and
    # `facts_usage` are promoted to the top level (next to the crawl's own
    # totals) because they are what the cost surfaces read; the facts
    # sub-report stays nested so the crawl report's existing contract is
    # unchanged. The run row's `usage` is keyed by stage; `{}` still means
    # "no tokens spent" — a different claim from "$0.00" — whenever a
    # stage is off or the regex tier answered.
    ner_usage = _detector_usage(detector)
    if ner_usage:
        report["ner_usage"] = ner_usage
    ocr_usage = _ocr_run_usage(_scan_ocr)
    if ocr_usage:
        report["ocr_usage"] = ocr_usage
    scan_ocr_triage = _ocr_triage_usage(_scan_ocr)
    if scan_ocr_triage:
        report["scan_ocr"] = scan_ocr_triage
    facts_usage: Dict[str, Any] = {}
    if facts_report is not None:
        report["facts"] = facts_report
        facts_usage = facts_report.get("facts_usage") or {}
        report["facts_usage"] = facts_usage
    state["last_run"] = report
    save_state(connection_id, state)
    run_usage: Dict[str, Any] = {}
    if ner_usage:
        run_usage["ner"] = ner_usage
    if ocr_usage:
        run_usage["ocr"] = ocr_usage
    if facts_usage:
        run_usage["facts"] = facts_usage
    if _ingested_nothing_despite_errors(stats):
        # Same status vocabulary a crash already uses (`failed` /
        # `interrupted` / `done`) — no new word is minted here. A run that
        # errored on (almost) everything and landed nothing is at least as
        # bad as a crash, and the JOB itself still completed (no exception
        # propagates from here): `extraction_runs.status` and the job's own
        # lifecycle are allowed to disagree by design (see
        # `ExtractionRunsPgRepository`'s module docstring) — this is exactly
        # the case that split them.
        status = "failed"
        finish_error = (
            f"{stats.errors} file(s) errored and 0 documents were ingested this run — "
            "see report.errors_detail for the per-file causes"
        )
        logger.warning(
            "sharepoint crawl: connection %s — %d errors and 0 new/changed documents; "
            "recording this run as failed rather than done",
            connection_id,
            stats.errors,
        )
    else:
        status = "done"
        finish_error = None
    recorder.finish(stats, status=status, report=report, usage=run_usage, error=finish_error)
    logger.info(
        "sharepoint crawl: connection %s — %d new, %d changed, %d unchanged, %d deleted, "
        "%d errors, %d oversize skipped",
        connection_id,
        stats.new,
        stats.changed,
        stats.unchanged,
        stats.deleted,
        stats.errors,
        stats.oversize_files,
    )
    return report


# --------------------------------------------------------------------------
# Shard-crawl seam (2026-09-03 auto-parallel-crawl design, Task 1) — the body
# shared between the inline whole-connection crawl above and a shard child's
# crawl (Task 4, ``run_shard_crawl``). Appended here rather than inlined so
# that neither caller's own body changes shape; see each parameter's
# docstring below for the exact substitution each caller makes.
# --------------------------------------------------------------------------


async def _crawl_targets(
    connection_id: str,
    *,
    targets_by_scope: Sequence[Tuple["_ScopeContext", Sequence[DriveTarget]]],
    transport: GraphTransport,
    ingestor: "_Ingestor",
    stats: "CrawlStats",
    max_file_mb: int,
    anonymization_key: Optional[bytes],
    recorder: Optional["_RunRecorder"],
    detector: Any,
    deadline: Optional["_Deadline"],
    governor: "_ConcurrencyGovernor",
    stop_watcher: Optional["_StopWatcher"],
    convert_pool: Optional["_ConvertProcessPool"],
    force_reprocess: bool,
    retry_failed: bool,
    retry_empty: bool,
    state_for: Callable[[DriveTarget], Dict[str, Any]],
    save_for: Callable[[DriveTarget, Dict[str, Any]], None],
) -> None:
    """Crawl every already-resolved ``(scope-context, targets)`` pair.

    This is the ONE body :func:`_run_crawl_async` (the inline whole-
    connection path) and :func:`run_shard_crawl` (Task 4 — a shard child's
    own crawl, over a persisted plan's targets rather than a freshly-
    resolved scope) both drive :func:`_crawl_drive` through — a caller never
    duplicates the per-target state wiring below.

    ``state_for(target)`` returns the state dict THIS target crawls with;
    ``save_for(target, state)`` persists it. The inline caller passes the
    single connection-wide ``state``/`` save_state`` for every target
    (ignoring ``target`` — today's behaviour, unchanged). A shard child
    passes a closure over its OWN per-delta-unit state row
    (``kind='crawl:<target.state_key>'``, Task 2) — never the whole
    connection's row, so two children never write the same row.

    Every other argument rides straight through to :func:`_crawl_drive`
    unchanged for every target in every scope — a shard child's crawl is
    otherwise indistinguishable from the inline path's, by design.
    """
    for ctx, targets in targets_by_scope:
        for target in targets:
            state = state_for(target)

            def _save(_target: DriveTarget = target, _state: Dict[str, Any] = state) -> None:
                save_for(_target, _state)

            await _crawl_drive(
                target,
                ctx=ctx,
                transport=transport,
                ingestor=ingestor,
                connection_id=connection_id,
                state=state,
                stats=stats,
                max_file_mb=max_file_mb,
                anonymization_key=anonymization_key,
                recorder=recorder,
                detector=detector,
                deadline=deadline,
                governor=governor,
                stop_watcher=stop_watcher,
                convert_pool=convert_pool,
                force_reprocess=force_reprocess,
                retry_failed=retry_failed,
                retry_empty=retry_empty,
                save_state_fn=_save,
            )


def rehome_legacy_backlog(
    legacy: Mapping[str, Any], shard_prefixes: Sequence[Tuple[str, str]]
) -> Dict[str, Dict[str, Any]]:
    """Split a legacy (connection-level) ``failed_items``/``empty_items``
    dict into per-shard groups by path prefix — the ONE-TIME backlog
    rehoming a shard plan does the first time it shards an already-crawled
    drive (2026-09-03 auto-parallel-crawl design §4.2): each entry's own
    ``path`` (recorded by :func:`_note_retry`/:func:`_note_empty` at
    failure time) is matched against ``shard_prefixes`` — ``(shard_key,
    prefix)`` pairs, caller-ordered MOST-SPECIFIC FIRST, first match wins,
    same contract :func:`_route_collection` already uses for zone routing.

    An entry matching no prefix falls back to the LAST pair — by convention
    the remainder shard, whose own ``prefix`` is ``""`` and therefore
    matches everything — never silently dropped. An empty ``shard_prefixes``
    returns every entry ungrouped (an empty dict), since there is nowhere to
    put them.

    Pure: no I/O, no state-file reads/writes, no Graph calls — the caller
    (the planner) writes each returned group into that shard's own
    ``crawl:<shard_key>`` row via :func:`save_state`. Returns ``{shard_key:
    {stable_id: entry}}``, one key per ``shard_prefixes`` entry that
    actually received at least one item (a shard nothing rehomes to is
    simply absent, not present with an empty dict).
    """
    if not shard_prefixes:
        return {}
    fallback_key = shard_prefixes[-1][0]
    grouped: Dict[str, Dict[str, Any]] = {}
    for stable_id, entry in legacy.items():
        path = str((entry or {}).get("path") or "")
        matched = fallback_key
        for shard_key, prefix in shard_prefixes:
            if not prefix or path == prefix or path.startswith(prefix + "/"):
                matched = shard_key
                break
        grouped.setdefault(matched, {})[stable_id] = entry
    return grouped


def _apply_resync(connection_id: str, *, job_id: Optional[str] = None) -> None:
    """Force every drive of this connection to re-enumerate from scratch on
    its next crawl — the supported alternative to hand-editing the crawl
    state file on the data disk to recover a connection whose delta cursor
    ran past documents it never actually ingested.

    Drops ``delta_links`` (so the next pass starts every drive from a bare
    ``/delta``, which Graph answers with the drive's full current listing)
    and ``failed_items`` (a full re-walk offers every previously-failed item
    to the normal delta pipeline again, so the queue's own bookkeeping —
    including anything already given up on — would otherwise be stale).
    ``ctags`` are kept: a resync should make Graph tell us about everything
    again, not force re-downloading files whose content has not changed.

    Also resets every SHARD's own per-delta-unit row the same way
    (2026-09-03 auto-parallel-crawl design §4.2): a sharded site's cursors
    live in ``crawl:<state_key>`` rows the connection-level row no longer
    tracks once the FIRST fully-done sharded run has cleared its legacy
    ``ctags`` (see the crawler module's ``legacy_ctags`` docstring), so a
    resync that touched only the connection-level row would silently leave
    every shard's cursor untouched. ``ctags`` is kept per shard row too, for
    the same reason. A DuckDB-backed instance never has shard rows
    (:func:`connectors.sharepoint.state_store.list_kinds` always answers
    ``[]`` there) — a no-op extra step, not a new failure mode.

    Also drops the connection's PERSISTED shard plan (2026-09-04 finding
    #65 item 2): a resync forces the next trigger to re-plan from scratch
    rather than reuse a plan built before the cursors it now invalidates.

    Idempotent PER JOB (2026-09-04 finding #66 item 4 — live finding: a
    worker recreated mid-run reclaimed the job, which called this AGAIN with
    the identical ``resync: true`` payload, dropping the delta links the
    interrupted first attempt had already progressed past and restarting
    the whole enumeration from zero). ``state["resync_applied_for_job"]``
    records which ``job_id`` last actually applied a resync for this
    connection; a second call naming that SAME ``job_id`` is a no-op, so a
    reclaim resumes from whatever the interrupted attempt already
    persisted. ``job_id=None`` (a manual/test call this repo has no way to
    identify as a reclaim) always applies — the same conservative default
    this parameter's absence gave every caller before this marker existed.
    A FRESH trigger (a different ``job_id``) always re-applies too.
    """
    from connectors.sharepoint.state_store import list_kinds as _state_list_kinds

    with _state_lock:
        state = load_state(connection_id)
        if job_id and state.get("resync_applied_for_job") == job_id:
            logger.info(
                "sharepoint crawl: resync for connection %s already applied by job %s — reclaim, not re-applying",
                connection_id,
                job_id,
            )
            return
        state["delta_links"] = {}
        state["failed_items"] = {}
        state.pop("shard_plan", None)
        state["resync_applied_for_job"] = job_id
        state["resync_applied_at"] = _now_iso()
        save_state(connection_id, state)
        for kind in _state_list_kinds(connection_id, "crawl:"):
            shard_key = kind[len("crawl:") :]
            shard_state = load_state(connection_id, shard_key=shard_key)
            shard_state["delta_links"] = {}
            shard_state["failed_items"] = {}
            save_state(connection_id, shard_state, shard_key=shard_key)


def run_builtin_crawl(payload: dict) -> dict:
    """Entry point for the ``corpus-extraction`` job kind
    (``app/worker/kinds.py::_run_corpus_extraction``, a thin delegate to
    this).

    2026-09-03 auto-parallel-crawl design: this is now sometimes a PLANNER,
    not always a crawler. ``_plan_or_run_inline`` decides — a DuckDB-backed
    instance, ``extraction.crawler.shard_target_docs`` at 0, or a site whose
    total stays at or under that target all take the INLINE path below,
    byte-for-byte today's crawl. A large site on Postgres instead gets
    packed into shards, gets ONE parent ``extraction_runs`` row, and this
    call enqueues K ``corpus-extraction-shard`` children and returns without
    ever crawling itself — see :func:`_plan_or_run_inline` /
    :func:`run_shard_crawl` / :func:`_finalize_site_run`.

    Bounded by ``extraction.timeout_s``. On expiry the run saves its state,
    reports ``interrupted_reason="timeout"``, and fails the job; the next
    run resumes from the persisted deltaLinks and cTags.
    ``payload["timeout_s"]`` overrides the configured value for one run
    (0 = unbounded).

    An exhausted 429 budget stops a run the same way, reporting
    ``interrupted_reason="throttled"``. So does an admin-requested
    cooperative stop (``POST …/extraction/stop`` — see :func:`request_stop`),
    reporting ``interrupted_reason="stopped"`` — see :data:`_STOP_REASONS`
    for why those three, and only those three, license a caller to tell the
    operator the next run picks up where this one stopped.

    ``payload``: ``connection_id`` (required — a ``source_connections`` row
    with ``source_type='sharepoint'``) and optionally ``scopes`` (a list of
    ``source_scope_id``s to narrow the run to; every confirmed scope
    otherwise), ``concurrency`` (this run's in-page item concurrency,
    overriding ``extraction.crawler.concurrency``, clamped to
    ``[1, 16]`` — the same one-run override shape ``timeout_s`` has; the
    report's ``concurrency`` block names the effective value and its source),
    ``resync`` (truthy — drops this connection's persisted deltaLinks
    and item-failure queue before crawling, so every drive re-enumerates
    from scratch; see :func:`_apply_resync`. The supported way to recover a
    connection stuck believing it has nothing left to do), and
    ``force_reprocess`` (truthy — ignores the persisted deltaLinks AND
    cTags for this run only, so every item Graph still has is
    downloaded, converted and re-ingested regardless of whether it looks
    unchanged. The operator control for "the content on disk is the same
    but I need it re-processed anyway" — e.g. a converter or anonymizer
    setting changed. Unlike ``resync`` this never writes to the state file
    up front: an interrupted forced run leaves the connection exactly as
    resumable as before the run started, and the fresh deltaLink/cTags a
    completed forced run observes are what land in state at the end, the
    same as any other run), and ``retry_failed`` (truthy — this run's
    per-drive backlog replay (see the module docstring's "a per-item
    failure never advances past itself") ALSO includes items already
    ``given_up`` on, not just the ones still under
    :data:`_MAX_ITEM_RETRY_ATTEMPTS`; every other drive behaviour is
    unchanged, including the ordinary incremental delta walk that follows
    the backlog replay. The cheap, targeted alternative to ``resync`` for a
    connection with a handful of permanently-stuck documents — see
    :func:`_retry_failed_items`'s ``include_given_up`` for the mechanism.
    Does NOT, on its own, replay a DOOMED item (:func:`_doomed_skip_reason`
    — a deterministic reject, or TCRD-296 gap #74's repeated crash/timeout):
    that skip is gated on ``force_reprocess`` alone, so ``retry_failed``
    without it still gives a permanently-stuck-but-not-yet-doomed item one
    more chance while leaving an already-doomed one out of the replay cost —
    combine with ``force_reprocess`` to force those too), and ``retry_empty``
    (truthy — replays every item this connection last
    converted to ``convert_empty`` — see :func:`_retry_empty_items`. Unlike
    ``retry_failed`` this NEVER runs implicitly: an ordinary crawl leaves
    the empty-document backlog alone, since replaying it changes nothing
    while scan OCR stays off. The targeted admin action for "I just turned
    ``extraction.scan_ocr.enabled`` on, reconsider what it can now read" —
    see ``POST …/extraction/retry-empty`` in ``app/api/admin_sharepoint.py``),
    and ``force_replan`` (truthy — on a site large enough to auto-shard,
    discards the connection's PERSISTED shard plan and builds a fresh one
    for this trigger, without touching any cursor (unlike ``resync``, every
    drive still resumes incrementally); a no-op everywhere else. See
    :func:`_plan_or_run_inline` — 2026-09-04 finding #65 item 2).
    Credentials are resolved from the row, never from the payload.

    Returns the crawl report — the same dict persisted as ``last_run`` in
    this connection's crawl state, so the job result and the state file can
    never disagree about what a run did.

    An optional ``job_id`` in the payload is recorded on the run row
    (``extraction_runs.job_id``) so the card can join a run to its job's
    lifecycle. ``app/worker/kinds.py::dispatch_job`` merges the claimed job's
    own id in before calling this handler's caller (``_run_corpus_
    extraction``), since ``JobKind.handler`` only ever sees the payload, not
    the job row — see ``_payload_for_handler`` there. A payload built outside
    the worker (a test, a manual trigger) may simply omit it; the column is
    then honestly null rather than guessed, and the liveness check falls
    back to checkpoint age.
    """
    connection_id = payload.get("connection_id")
    if not connection_id:
        raise CrawlError("sharepoint crawl: payload missing connection_id")

    from src.repositories import source_connections_repo

    connection = source_connections_repo().get(connection_id)
    if connection is None or connection.get("source_type") != "sharepoint":
        raise CrawlError(f"sharepoint crawl: connection {connection_id!r} not found or not a sharepoint connection")

    if payload.get("resync"):
        _apply_resync(str(connection_id), job_id=payload.get("job_id"))

    try:
        return asyncio.run(_plan_or_run_inline(connection, payload))
    except SharePointSettingsError as exc:
        # Named cause, not a bare traceback — the same typed handling
        # `app/api/admin_sharepoint.py::_resolved_token` gives this error.
        raise CrawlError(f"sharepoint crawl: {exc}") from exc


# ---------------------------------------------------------------------------
# Memory-budget clamp on per-job concurrency (TCRD-296 C.10 / auto-parallel-
# crawl design §4.6): several `corpus-extraction*` jobs can share ONE
# container (the worker's EXTRACTION lane, `app/worker/runtime.py`), each
# forking its own `_ConvertProcessPool` sized to `_resolve_concurrency`'s
# cap — so a cap tuned only against Graph's own throttling (see
# `_MAX_CONCURRENCY` above) can still ask for more in-flight files than the
# container's own cgroup memory limit can hold. This never raises a
# configured/payload cap, only lowers it — it is a ceiling derived from
# where this process actually runs, not a new tuning knob.
# ---------------------------------------------------------------------------

#: cgroup v2's unified memory limit file. Absent on a v1-only host.
_CGROUP_V2_MEMORY_MAX_PATH = Path("/sys/fs/cgroup/memory.max")
#: cgroup v1's memory-controller limit file — the fallback when the v2 path
#: above does not exist.
_CGROUP_V1_MEMORY_LIMIT_PATH = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")

#: Fraction of the container's own cgroup memory limit this clamp assumes is
#: actually available for in-flight file conversion — headroom for the
#: crawl parent's own baseline VmSize and everything else sharing the
#: container, the same class of finding as the parent-OOM notes above
#: :data:`_DEFAULT_MAX_CONVERTED_MB` and :data:`_DEFAULT_CONVERT_CHILD_MAX_RSS_MB`.
_MEMORY_HEADROOM = 0.8

#: Bytes reserved per in-flight file when deriving a concurrency cap from the
#: container's own memory limit — the same "roughly 2 GB per in-flight file"
#: figure :data:`_MAX_CONCURRENCY`'s own docstring already cites for sizing
#: a large conversion box.
_MEMORY_RESERVE_PER_ITEM_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB


def _cgroup_memory_limit_bytes() -> Optional[int]:
    """The container's own memory limit in bytes, or ``None`` when it cannot
    be determined.

    Reads cgroup v2's ``memory.max`` first, falling back to cgroup v1's
    ``memory.limit_in_bytes`` when the v2 file does not exist. ``"max"``
    (v2's spelling for "unlimited"), a missing file, an empty file, or an
    unparseable value all return ``None`` — the caller then leaves
    concurrency exactly as configured rather than guessing a limit from
    nothing. Linux only: :data:`sys.platform` is checked explicitly (rather
    than relying on the reads failing) so a no-op on macOS — where this
    repo's own tests run — is by declared intent, not accident.
    """
    if sys.platform != "linux":
        return None
    for path in (_CGROUP_V2_MEMORY_MAX_PATH, _CGROUP_V1_MEMORY_LIMIT_PATH):
        try:
            raw = path.read_text().strip()
        except OSError:
            continue
        if not raw or raw == "max":
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        if value > 0:
            return value
    return None


def _memory_budget_cap(limit_bytes: int, lanes: int) -> int:
    """The concurrency cap the container's own memory limit can sustain,
    minimum 1.

    ``floor(limit_bytes * _MEMORY_HEADROOM / (lanes * _MEMORY_RESERVE_PER_ITEM_BYTES))``.
    ``lanes`` is the worker's EXTRACTION lane slot count
    (``app/worker/runtime.py::_extraction_concurrency``) — several
    `corpus-extraction*` jobs, each with its OWN convert pool, can hold a
    lane at once in the same container, so the per-job budget has to divide
    the container's whole limit by how many jobs can be converting at once,
    not assume this run is the only one.
    """
    lanes = max(1, int(lanes))
    budget = int((limit_bytes * _MEMORY_HEADROOM) // (lanes * _MEMORY_RESERVE_PER_ITEM_BYTES))
    return max(1, budget)


def _apply_memory_budget_cap(cap: int, source: str) -> Tuple[int, str]:
    """Clamp ``cap`` DOWNWARD ONLY (never raises it, minimum 1) to what the
    container's own cgroup memory limit can sustain, given the worker's own
    EXTRACTION lane count — see :func:`_cgroup_memory_limit_bytes` and
    :func:`_memory_budget_cap`.

    A missing/unreadable limit (``"max"``, no cgroup files, non-Linux) is a
    no-op: ``(cap, source)`` unchanged. When a limit IS found, this logs once
    at INFO with the limit, the lane count and the resulting cap — whether
    or not it actually reduced ``cap`` — so an operator reading the log for
    one run sees this ran, not only that it fired. ``source`` becomes
    ``"memory_budget"`` (joining ``"config"``/``"payload"``/``"adaptive"`` in
    :attr:`CrawlStats.concurrency_source`'s vocabulary) only when the clamp
    actually lowered the cap; a cap already at or below the budget keeps its
    own source label, since nothing about it is attributable to this clamp.
    """
    limit_bytes = _cgroup_memory_limit_bytes()
    if limit_bytes is None:
        return cap, source

    from app.worker.runtime import _extraction_concurrency as _worker_extraction_lanes

    lanes = _worker_extraction_lanes()
    budget_cap = _memory_budget_cap(limit_bytes, lanes)
    clamped = min(cap, budget_cap)
    logger.info(
        "sharepoint crawl: cgroup memory limit %d bytes, %d extraction lane(s) -> "
        "memory-budget cap %d (cap before clamp %d, effective %d)",
        limit_bytes,
        lanes,
        budget_cap,
        cap,
        clamped,
    )
    if clamped < cap:
        return clamped, "memory_budget"
    return cap, source


# --------------------------------------------------------------------------
# Automatic parallel site crawl (2026-09-03 design, Task 4) — the planner,
# the shard child's own crawl, and the finalizer. PG-only by construction
# (the planner never even tries to shard on a DuckDB-backed instance —
# `_plan_or_run_inline` checks `use_pg()` itself, the same fail-clean
# posture `connectors.sharepoint.state_store` already takes for a `crawl:`
# state kind).
# --------------------------------------------------------------------------


class _ShardPlanUnavailable(RuntimeError):
    """Raised internally when a shard plan was computed but the PARENT run
    row could not be opened (run recording — never load-bearing anywhere
    else in this module — IS the coordination mechanism the children roll
    up into here). The caller falls back to the inline crawl rather than
    enqueue children with nothing to join."""


def _scope_set_hash(scopes: Sequence[Dict[str, Any]]) -> str:
    """A stable fingerprint of WHICH scopes a shard plan was built from —
    the plan-reuse validity check (2026-09-04 finding #65 item 2): a scope
    added, removed, or un-confirmed since the last plan invalidates it.
    Nothing else about a scope row (``display_path``, ``anonymize``,
    ``access_mode``, ...) does, since none of those change what a shard's
    own delta targets are."""
    ids = sorted(str(s.get("source_scope_id") or "") for s in scopes)
    return hashlib.sha256("|".join(ids).encode("utf-8")).hexdigest()[:16]


def _known_folder_counts(scopes: Sequence[Dict[str, Any]]) -> Tuple[Dict[str, int], Dict[str, Dict[str, int]]]:
    """Cheap, non-Graph size signal for the shard planner (2026-09-04
    finding #65 item 1(a)): for a whole-DRIVE scope whose collection already
    holds indexed documents, ``corpus_files.top_folder_status_counts`` gives
    a per-top-level-folder document count in ONE query per distinct
    collection — the same projection ``connectors.sharepoint.completeness``
    already combines with ``list_root_children_with_url`` for its own
    "expected" column, reused rather than reinvented.

    Returns ``(known_totals, known_folder_counts)`` —
    ``known_totals``: ``drive_id -> total documents already known`` (the
    sum of a drive's own folder counts, fed to
    :func:`connectors.sharepoint.shard_plan.compute_shard_plan`'s Pass 1);
    ``known_folder_counts``: ``drive_id -> {folder_name -> documents}`` (fed
    to its Pass 2). A "folder"/"site"-kind scope (not the drive root itself)
    is skipped: its own collection's paths are relative to the SCOPE's
    root, not the DRIVE's top-level folder names the planner packs by, so
    there is no signal to attach here — childCount/Search still cover it.
    An empty/never-crawled collection yields no entry at all (nothing
    known yet), never a false ``0``.
    """
    from src.repositories import corpus_files_repo

    known_totals: Dict[str, int] = {}
    known_folder_counts: Dict[str, Dict[str, int]] = {}
    try:
        files_repo = corpus_files_repo()
    except Exception:  # noqa: BLE001 — a cheap-signal lookup failing must never block planning
        logger.debug("sharepoint shard planner: corpus_files_repo() unavailable — skipping known counts", exc_info=True)
        return known_totals, known_folder_counts

    for scope in scopes:
        source_scope_id = str(scope.get("source_scope_id") or "")
        if _scope_kind(source_scope_id) != "drive":
            continue
        collection_id = scope.get("collection_id")
        if not collection_id:
            continue
        try:
            folder_status = files_repo.top_folder_status_counts(str(collection_id))
        except Exception:  # noqa: BLE001 — a cheap-signal lookup failing must never block planning
            logger.debug(
                "sharepoint shard planner: known-count lookup failed for drive %r", source_scope_id, exc_info=True
            )
            continue
        by_folder: Dict[str, int] = {}
        for folder, status_counts in folder_status.items():
            if not folder:
                continue  # "" = loose root files, not a top-level folder unit
            by_folder[folder] = sum(status_counts.values())
        if by_folder:
            known_folder_counts[source_scope_id] = by_folder
            known_totals[source_scope_id] = sum(by_folder.values())
    return known_totals, known_folder_counts


def _predates_remainder_scope_fix(shard: Dict[str, Any]) -> bool:
    """True when a persisted ``shard_defs`` entry has the exact shape a
    PRE-2026-09-06 ``shard_plan.plan_shards`` produced for a FOLDER scope's
    remainder: ``root_item_id=None`` (the whole drive) instead of the
    scope's own root (finding: "the remainder shard must be scoped to the
    same subtree its scope covers, never to the drive root"). A genuine
    WHOLE-DRIVE scope's remainder legitimately has ``root_item_id=None``
    too — this only flags the combination that is never legitimate: a
    ``kind=="folder"`` scope's own remainder pointing at the drive root."""
    if shard.get("label") != "remainder":
        return False
    if _scope_kind(str(shard.get("scope_id") or "")) != "folder":
        return False
    return any(t.get("root_item_id") is None for t in shard.get("targets") or ())


def _reusable_shard_plan(connection_id: str, scopes: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The connection's LAST persisted shard plan, if it is still valid to
    reuse for THIS trigger (2026-09-04 finding #65 item 2) — present, built
    from the SAME scope set, and not STALE (2026-09-06 finding: a plan a
    PRE-remainder-scope-fix worker already persisted for a folder scope —
    its own remainder scoped to the drive root — must never be served back
    verbatim just because the fix shipped after it was written;
    ``scope_set_hash`` alone cannot tell the two apart, since the scope set
    itself never changed across the deploy). Returns the persisted entry
    VERBATIM (``{"shards", "scope_set_hash", "signal", "min_modified",
    ...}`` — see :func:`_enqueue_shard_plan`'s own persisted shape) so the
    caller can re-persist the SAME fingerprint after reusing it, letting a
    THIRD, FOURTH, ... trigger keep reusing it too — or ``None`` (build a
    fresh plan). The caller is responsible for the OTHER two invalidation
    triggers (``resync`` — handled by :func:`_apply_resync` dropping the
    persisted plan outright before this is ever consulted — and
    ``force_replan``, which skips calling this at all)."""
    state = load_state(connection_id)
    persisted = state.get("shard_plan") or {}
    shards = persisted.get("shards")
    if not shards:
        return None
    if persisted.get("scope_set_hash") != _scope_set_hash(scopes):
        return None
    if any(_predates_remainder_scope_fix(shard) for shard in shards):
        return None
    return dict(persisted)


async def _plan_or_run_inline(connection: Dict[str, Any], payload: dict) -> Dict[str, Any]:
    """Decide inline vs. sharded for this connection's run, once, and do
    whichever one it picks (design §4.1).

    Inline (byte-for-byte :func:`_run_crawl_async`, today's crawl) when ANY
    of: the active app-state backend is DuckDB (A3 ratchet), ``extraction.
    crawler.shard_target_docs`` is ``0`` (design §4.8), there is no
    confirmed scope to plan against, a scope-resolution Graph call fails
    (the inline path's own per-scope error isolation is a better fit for
    that than a half-built plan), :func:`connectors.sharepoint.shard_plan.
    compute_shard_plan` finds the connection's SUMMED total at or under the
    target across every confirmed scope's every drive (its own two-pass
    site-level decision), or planning exhausts its search budget with no
    usable signal at all (:class:`connectors.sharepoint.shard_plan.
    PlanningBudgetExhausted` — 2026-09-04 finding #65 item 4: a plan
    balanced on nothing but a 429 storm is worse than falling back).

    Sharded otherwise: opens ONE parent run row BEFORE any Graph call
    (2026-09-04 finding #65 item 3 — a large site's planning window used to
    run with no run row and no log line at all), reuses the connection's
    LAST persisted plan when it is still valid for this scope set (finding
    #65 item 2 — ``resync``/``force_replan``/a scope-set change force a
    fresh plan instead), and enqueues one ``corpus-extraction-shard`` child
    per packed shard (:func:`_enqueue_shard_plan`) — never crawls anything
    itself.
    """
    connection_id = str(connection["id"])

    async def _inline() -> Dict[str, Any]:
        return await _run_crawl_async(
            connection,
            only_scope_ids=payload.get("scopes"),
            job_id=payload.get("job_id"),
            timeout_s=payload.get("timeout_s"),
            concurrency=payload.get("concurrency"),
            force_reprocess=bool(payload.get("force_reprocess")),
            retry_failed=bool(payload.get("retry_failed")),
            retry_empty=bool(payload.get("retry_empty")),
        )

    from src.repositories import use_pg

    target_docs = _shard_target_docs()
    if not use_pg() or target_docs <= 0:
        return await _inline()

    scopes = _confirmed_scopes(connection)
    only_scope_ids = payload.get("scopes")
    if only_scope_ids:
        wanted = set(only_scope_ids)
        scopes = [s for s in scopes if str(s.get("source_scope_id")) in wanted]
    if not scopes:
        # No confirmed scope to plan against — let the inline path raise
        # its own named `CrawlError` for this, rather than duplicating that
        # check here.
        return await _inline()

    # Opened BEFORE any Graph call (finding #65 item 3): the fleet view and
    # source card must show SOMETHING for the whole planning window, not
    # only once a plan already exists. A row that cannot be opened at all
    # means run recording itself is unavailable — the same case that would
    # have made `_enqueue_shard_plan` refuse below; decided here, up front,
    # instead of after paying for a plan with nothing to join it to.
    recorder = _RunRecorder(connection_id, job_id=payload.get("job_id"))
    recorder.start(phase="planning")
    if recorder.run_id is None:
        return await _inline()

    force_replan = bool(payload.get("force_replan"))
    if not force_replan:
        reused = _reusable_shard_plan(connection_id, scopes)
        if reused is not None:
            try:
                return _enqueue_shard_plan(
                    connection_id,
                    list(reused["shards"]),
                    payload,
                    recorder=recorder,
                    # Re-persisted VERBATIM (same fingerprint) so a THIRD,
                    # FOURTH, ... trigger can keep reusing this plan too —
                    # not just the one right after it was built.
                    scope_set_hash=reused.get("scope_set_hash"),
                    signal=reused.get("signal"),
                    min_modified=reused.get("min_modified"),
                )
            except _ShardPlanUnavailable:
                return await _inline()

    settings = resolve_sharepoint_settings(connection)
    # Connection-wide default ONLY — this feeds `compute_shard_plan`'s doc-COUNT
    # estimate across every scope in the plan, not the actual per-item filter a
    # scope's own override enforces at crawl time (`_run_shard_crawl_async`
    # re-resolves per scope, correctly, above). A scope-level override not
    # reflected here can only skew the shard-size estimate/"expected" display,
    # never drop a document the real crawl would have kept (TCRD-296 gap #80).
    min_modified, _min_modified_source = resolve_min_modified(connection)
    plan_stats = CrawlStats()
    auth = GraphAuth(
        acquire=lambda: graph_client.get_app_token(settings.tenant_id, settings.client_id, settings.private_key),
        stats=plan_stats,
    )
    transport = GraphTransport(auth, plan_stats)

    all_targets: List[DriveTarget] = []
    scope_of_state_key: Dict[str, Dict[str, Any]] = {}
    for scope in scopes:
        try:
            targets = await _drive_targets(transport, scope)
        except (CrawlError, SharePointGraphError) as exc:
            # One scope's misconfiguration (or a Graph outage) is exactly
            # the case the inline path's own per-scope isolation already
            # handles (`scope_errors`) — a half-built plan missing one
            # scope's coverage would be worse than falling all the way back.
            recorder.finish_planning_as_inline_fallback(reason=f"scope resolution failed: {exc}")
            return await _inline()
        for target in targets:
            all_targets.append(target)
            scope_of_state_key[target.state_key] = scope

    if not all_targets:
        recorder.finish_planning_as_inline_fallback(reason="no delta targets resolved for any confirmed scope")
        return await _inline()

    known_totals, known_folder_counts = _known_folder_counts(scopes)

    try:
        plan = await compute_shard_plan(
            transport,
            auth,
            {},
            all_targets,
            min_modified=min_modified,
            target_docs=target_docs,
            max_shards=_MAX_SHARDS,
            known_totals=known_totals,
            known_folder_counts=known_folder_counts,
            on_progress=recorder.checkpoint_planning,
        )
    except (CrawlError, SharePointGraphError) as exc:
        recorder.finish_planning_as_inline_fallback(reason=f"Graph error while planning: {exc}")
        return await _inline()
    except PlanningBudgetExhausted as exc:
        # Finding #65 item 4: the search budget ran out before ANY signal
        # (known/childCount/Search) resolved for any drive or folder — a
        # plan built on nothing but a 429 storm is worse than no plan.
        logger.warning("sharepoint crawl: connection %s — %s; falling back to inline", connection_id, exc)
        recorder.finish_planning_as_inline_fallback(reason=str(exc))
        return await _inline()

    if not plan["drives"]:
        # The connection's summed total stayed at or under target — the
        # site-level "stay inline" decision (design §4.1 point 2).
        recorder.finish_planning_as_inline_fallback(reason="connection total at or under shard_target_docs")
        return await _inline()

    shard_defs: List[Dict[str, Any]] = []
    unresolved_shards = 0
    # `compute_shard_plan` returns exactly one drive-plan entry PER TARGET,
    # in `all_targets` order — its own documented contract ("duplicated
    # verbatim across every target sharing a drive"). Pairing `all_targets`
    # with `plan["drives"]` POSITIONALLY therefore gives the exact scope
    # each drive-plan belongs to, even when two confirmed scopes share one
    # physical drive (a folder scope nested inside a whole-drive scope, or
    # two folder scopes on the same drive) — never an ambiguous choice.
    # Deliberately NOT keyed off a packed folder shard's own DERIVED
    # `state_key` (`f"{drive_id}:{unit['item_id']}"` from `shard_plan.
    # plan_shards`): that key is never a key `scope_of_state_key` holds
    # (only a whole drive's or a folder scope's OWN root key are), which
    # used to silently drop every folder shard the moment a drive was big
    # enough to split.
    for target, drive_plan in zip(all_targets, plan["drives"]):
        owning_scope = scope_of_state_key.get(target.state_key)
        if owning_scope is None:
            # Structurally unreachable — `target` comes from the SAME
            # `all_targets` list `scope_of_state_key` was built from above
            # — but `scope_id` decides a shard's `min_modified` filter and
            # permission zone, so a shard this code cannot confidently
            # attribute is dropped, never guessed at.
            unresolved_shards += len(drive_plan["shards"])
            continue
        for shard in drive_plan["shards"]:
            if not shard["targets"]:
                # A `pack_folders_into_groups` group that landed empty
                # (K > the number of folders) — nothing to crawl, nothing
                # to enqueue.
                continue
            shard_defs.append(
                {
                    "scope_id": str(owning_scope.get("source_scope_id")),
                    "label": shard["label"],
                    "signal": shard.get("signal") or SIGNAL_NONE,
                    "targets": shard["targets"],
                    "exclude_prefixes": shard.get("exclude_prefixes") or [],
                    "expected": shard.get("expected") or 0,
                }
            )

    if not shard_defs:
        reason = "plan produced no non-empty shard"
        if unresolved_shards:
            reason += f" ({unresolved_shards} shard(s) had no resolvable owning scope)"
        recorder.finish_planning_as_inline_fallback(reason=reason)
        return await _inline()

    try:
        return _enqueue_shard_plan(
            connection_id,
            shard_defs,
            payload,
            recorder=recorder,
            scope_set_hash=_scope_set_hash(scopes),
            signal=plan.get("signal"),
            min_modified=str(min_modified) if min_modified else None,
        )
    except _ShardPlanUnavailable:
        return await _inline()


def _enqueue_shard_plan(
    connection_id: str,
    shard_defs: List[Dict[str, Any]],
    payload: dict,
    *,
    recorder: Optional["_RunRecorder"] = None,
    scope_set_hash: Optional[str] = None,
    signal: Optional[str] = None,
    min_modified: Optional[str] = None,
) -> Dict[str, Any]:
    """Open the PARENT run row and enqueue one ``corpus-extraction-shard``
    child per entry in ``shard_defs`` (design §4.3) — the planner's own
    tail. Never crawls: by the time this returns, every shard's work is
    queued for ANY worker to claim, and this job's own claim is free to
    finish.

    ``recorder`` (2026-09-04 finding #65 item 3): an already-started
    ``_RunRecorder`` — ``_plan_or_run_inline`` opens its parent row with
    ``phase="planning"`` BEFORE calling the planner at all, so this reuses
    that SAME row (flipping it to ``phase="plan"`` and setting
    ``shards_total``, only now known) instead of opening a second one.
    ``None`` (the default) opens a fresh row itself — the shape
    ``app.api.admin_sharepoint._trigger_shard_rerun`` still uses: re-running
    NAMED shards from an already-persisted plan skips planning (and its
    visibility concerns) entirely.

    ``scope_set_hash``/``signal``/``min_modified`` (finding #65 item 2) ride
    into the persisted plan so the NEXT trigger can validate and reuse it
    without recomputing anything — all three ``None`` for a shard RE-RUN
    (``_trigger_shard_rerun``), which does not touch the persisted plan at
    all.

    Idempotency key ``corpus-extraction-shard:{connection_id}:{index}`` —
    a re-planned connection whose Nth shard now covers different folders
    still dedups against a STILL-QUEUED-OR-RUNNING Nth shard from a PRIOR
    plan; this is intentionally cheap protection against a double-trigger
    racing this same planner, not a guarantee the two plans agree on what
    index N means (a genuine re-plan only ever runs once the PREVIOUS
    parent has finalized — see ``app.api.admin_sharepoint.trigger_
    extraction``'s 409 gate on a top-level running row).
    """
    from app.worker.registry import job_max_attempts
    from src.repositories import jobs_repo

    shards_total = len(shard_defs)
    if recorder is None:
        recorder = _RunRecorder(connection_id, job_id=payload.get("job_id"), shards_total=shards_total)
        recorder.start()
    else:
        recorder.mark_planned(shards_total)
    parent_run_id = recorder.run_id
    if parent_run_id is None:
        raise _ShardPlanUnavailable(f"could not open a parent run row for connection {connection_id!r}")

    # Persisted so the NEXT trigger can REUSE this plan instead of
    # recomputing it (finding #65 item 2 — a live 388-scope connection's
    # planning window alone took 20+ minutes; a reused plan starts children
    # within seconds). `scope_set_hash` is the reuse validity check
    # (`_reusable_shard_plan`); `resync` drops this whole entry
    # (`_apply_resync`) and `force_replan` skips consulting it, both forcing
    # a fresh plan on the NEXT trigger, not this one.
    state = load_state(connection_id)
    state["shard_plan"] = {
        "parent_run_id": parent_run_id,
        "shards_total": shards_total,
        "planned_at": _now_iso(),
        "scope_set_hash": scope_set_hash,
        "signal": signal,
        "min_modified": min_modified,
        "shards": shard_defs,
    }
    save_state(connection_id, state)

    max_attempts = job_max_attempts(_SHARD_JOB_KIND)
    for index, shard in enumerate(shard_defs, start=1):
        child_payload = {
            "connection_id": connection_id,
            "parent_run_id": parent_run_id,
            "shard_index": index,
            "shard": {**shard, "shard_index": index},
            "concurrency": payload.get("concurrency"),
            "timeout_s": payload.get("timeout_s"),
            "force_reprocess": bool(payload.get("force_reprocess")),
            "retry_failed": bool(payload.get("retry_failed")),
            "retry_empty": bool(payload.get("retry_empty")),
        }
        jobs_repo().enqueue(
            _SHARD_JOB_KIND,
            child_payload,
            priority=_SHARD_JOB_PRIORITY,
            max_attempts=max_attempts,
            idempotency_key=f"{_SHARD_JOB_KIND}:{connection_id}:{index}",
        )

    logger.info(
        "sharepoint crawl: connection %s — planned %d shard(s), parent run %s",
        connection_id,
        shards_total,
        parent_run_id,
    )
    return {
        "mode": "sharded",
        "connection_id": connection_id,
        "parent_run_id": parent_run_id,
        "shards_total": shards_total,
    }


def run_shard_crawl(payload: dict) -> dict:
    """Entry point for the ``corpus-extraction-shard`` job kind — one
    shard child's own crawl (design §4.3).

    ``payload``: ``connection_id``, ``parent_run_id``, ``shard_index``,
    ``shard`` (``{scope_id, label, targets, exclude_prefixes, expected}`` —
    ``targets`` is a list of ``{drive_id, root_item_id, state_key, path}``,
    :func:`_enqueue_shard_plan`'s own output), plus the same
    ``concurrency``/``timeout_s``/``force_reprocess``/``retry_failed``/
    ``retry_empty`` pass-through :func:`run_builtin_crawl` accepts, fanned
    out unchanged from the parent's own trigger payload.

    Credentials are resolved from the connection row, never from the
    payload — same posture as :func:`run_builtin_crawl`.
    """
    connection_id = payload.get("connection_id")
    parent_run_id = payload.get("parent_run_id")
    shard = payload.get("shard") or {}
    if not connection_id or not parent_run_id or not shard.get("targets"):
        raise CrawlError("sharepoint crawl: shard payload missing connection_id/parent_run_id/shard.targets")

    from src.repositories import source_connections_repo

    connection = source_connections_repo().get(connection_id)
    if connection is None or connection.get("source_type") != "sharepoint":
        raise CrawlError(f"sharepoint crawl: connection {connection_id!r} not found or not a sharepoint connection")

    try:
        return asyncio.run(
            _run_shard_crawl_async(connection, parent_run_id=str(parent_run_id), shard=shard, payload=payload)
        )
    except SharePointSettingsError as exc:
        raise CrawlError(f"sharepoint crawl: {exc}") from exc


async def _run_shard_crawl_async(
    connection: Dict[str, Any], *, parent_run_id: str, shard: Dict[str, Any], payload: dict
) -> Dict[str, Any]:
    """The shard child's own crawl body — the same pipeline
    :func:`_run_crawl_async` runs for the inline path, over exactly this
    shard's ``targets``, with its OWN per-delta-unit state rows (never the
    connection-level one) and its OWN ``extraction_runs`` row.

    Never clears the connection-wide cooperative-stop flag (``clear_stale_
    stop`` stays at its default in the sense that this function never even
    calls :func:`_clear_stale_stop` — only the PARENT planner run does,
    once, per top-level trigger) and never sweeps stale ``running`` rows
    for the connection (:class:`_RunRecorder`'s ``sweep_stale=False`` — a
    sibling shard's still-``running`` row is a peer, not an orphan).
    """
    connection_id = str(connection["id"])
    scope_id = str(shard.get("scope_id") or "")
    scope = next((s for s in _confirmed_scopes(connection) if str(s.get("source_scope_id")) == scope_id), None)
    if scope is None:
        raise CrawlError(
            f"sharepoint crawl: shard's scope {scope_id!r} is no longer a confirmed scope on connection "
            f"{connection_id!r} — it may have been removed since this shard was planned"
        )

    stop_watcher = _StopWatcher(connection_id)
    settings = resolve_sharepoint_settings(connection)
    # This scope's own override, falling back to the connection default
    # (TCRD-296 gap #80) — a shard child crawls exactly ONE scope, so this
    # is the same per-scope resolution the inline path's loop does.
    min_modified, _min_modified_source = resolve_min_modified(connection, scope=scope)
    anonymization_key = _resolve_anonymization_key([scope])
    detector = _entity_detector() if anonymization_key is not None else None
    max_file_mb = _max_file_mb()
    cap, configured_concurrency, concurrency_source = _resolve_concurrency(payload.get("concurrency"))
    deadline = _Deadline(_timeout_seconds() if payload.get("timeout_s") is None else payload.get("timeout_s"))

    stats = CrawlStats(
        concurrency=cap,
        concurrency_configured=configured_concurrency,
        concurrency_source=concurrency_source,
        concurrency_effective_max=cap,
        concurrency_min_target=cap,
    )
    governor = _ConcurrencyGovernor(cap, stats=stats)
    convert_pool = _ConvertProcessPool(
        cap,
        recycle_after_docs=_convert_recycle_after_docs(),
        recycle_rss_bytes=_convert_recycle_rss_bytes(),
        memory_limit_bytes=_convert_child_memory_limit_bytes(),
        timeout_s=_item_timeout_seconds(),
        max_output_bytes=_max_converted_output_bytes(),
        max_rss_bytes=_convert_child_max_rss_bytes(),
        spares_per_slot=_convert_spares_per_slot(),
    )
    convert_pool.start()
    auth = GraphAuth(
        acquire=lambda: graph_client.get_app_token(settings.tenant_id, settings.client_id, settings.private_key),
        stats=stats,
    )
    transport = GraphTransport(auth, stats)
    ingestor = _Ingestor()

    base_exclusions = await _excluded_path_prefixes(transport, scope)
    exclusions = _exclusion_index_with_extra_prefixes(base_exclusions, shard.get("exclude_prefixes") or ())

    # Read-only seed from the CONNECTION-level `crawl` row — see
    # `_ScopeContext.legacy_ctags`'s own docstring. Never the row this
    # shard writes to.
    legacy_ctags: Dict[str, str] = dict(load_state(connection_id).get("ctags") or {})

    ctx = _ScopeContext(
        source_scope_id=scope_id,
        collection_id=str(scope["collection_id"]),
        anonymize=bool(scope.get("anonymize")),
        exclusions=exclusions,
        zone_routes_by_drive=_zone_routes_for_scope(connection, scope_id),
        min_modified=min_modified,
        legacy_ctags=legacy_ctags,
    )

    targets = [
        DriveTarget(
            drive_id=str(t["drive_id"]),
            drive_name=t.get("path") or None,
            root_item_id=t.get("root_item_id"),
        )
        for t in shard["targets"]
    ]
    shard_key = ",".join(t.state_key for t in targets)

    recorder = _RunRecorder(
        connection_id,
        job_id=payload.get("job_id"),
        sweep_stale=False,
        parent_run_id=parent_run_id,
        shard_key=shard_key,
        shard_label=shard.get("label"),
    )
    recorder.start()

    scope_errors: List[Dict[str, Any]] = []
    try:
        try:
            await _crawl_targets(
                connection_id,
                targets_by_scope=[(ctx, targets)],
                transport=transport,
                ingestor=ingestor,
                stats=stats,
                max_file_mb=max_file_mb,
                anonymization_key=anonymization_key,
                recorder=recorder,
                detector=detector,
                deadline=deadline,
                governor=governor,
                stop_watcher=stop_watcher,
                convert_pool=convert_pool,
                force_reprocess=bool(payload.get("force_reprocess")),
                retry_failed=bool(payload.get("retry_failed")),
                retry_empty=bool(payload.get("retry_empty")),
                state_for=lambda t: load_state(connection_id, shard_key=t.state_key),
                save_for=lambda t, s: save_state(connection_id, s, shard_key=t.state_key),
            )
        finally:
            convert_pool.shutdown()
        # Streamed facts tail flush, same shape the inline crawl's own tail
        # uses (design §4.5): each child streams on its OWN counters; the
        # connection-keyed idempotency dedup collapses K children's
        # triggers into one queued pass.
        _maybe_stream_facts_extraction(connection_id, stats, final=True)
    except BaseException as exc:
        reason = _stop_reason(exc)
        interrupted_report = stats.report(max_file_mb=max_file_mb, interrupted=True, interrupted_reason=reason)
        interrupted_report["connection_id"] = connection_id
        interrupted_report["scope_errors"] = scope_errors
        status = "interrupted" if reason in {r for _, r in _STOP_REASONS} else _record_status_for(exc)
        recorder.finish(
            stats,
            status=status,
            report=interrupted_report,
            error=f"{type(exc).__name__}: {exc}",
        )
        _finish_shard_and_maybe_finalize(connection, parent_run_id)
        # Re-raised — same posture `_run_crawl_async` already takes: the
        # SHARD job itself fails too (no auto-retry, `retry_in_seconds=
        # None`), so an operator sees it, even though the row above already
        # says `interrupted`/`failed` honestly.
        raise

    report = stats.report(max_file_mb=max_file_mb)
    report["connection_id"] = connection_id
    report["scope_errors"] = scope_errors
    if _ingested_nothing_despite_errors(stats):
        status = "failed"
        finish_error = f"{stats.errors} file(s) errored and 0 documents were ingested in this shard"
    else:
        status = "done"
        finish_error = None
    recorder.finish(stats, status=status, report=report, error=finish_error)
    _finish_shard_and_maybe_finalize(connection, parent_run_id)
    return report


def _finish_shard_and_maybe_finalize(connection: Dict[str, Any], parent_run_id: str) -> None:
    """Bump the PARENT's ``shards_done``; the child that observes
    ``shards_done == shards_total`` wins the :meth:`claim_finalize` race and
    runs :func:`_finalize_site_run` (design §4.3 — "the LAST child to
    finish finalizes the parent").

    Never raises: called from a shard child's own finish path (success OR
    failure), and a coordination hiccup here must not turn an otherwise-
    already-recorded shard result into a crash — the same observability-
    never-load-bearing posture every other write in this module takes. A
    parent left stuck (this bookkeeping failed on what would have been the
    last child) is recovered by the next ``POST …/extract`` finalizing it
    instead of re-planning (design §4.3).
    """
    try:
        from src.repositories import extraction_runs_repo

        repo = extraction_runs_repo()
        result = repo.finish_shard(parent_run_id)
        if result is None:
            return
        shards_total = result.get("shards_total")
        if shards_total is not None and result.get("shards_done", 0) >= shards_total:
            if repo.claim_finalize(parent_run_id):
                _finalize_site_run(connection, parent_run_id)
    except Exception:  # noqa: BLE001 — coordination bookkeeping, never load-bearing for THIS shard
        logger.warning(
            "sharepoint crawl: shard finish/finalize bookkeeping failed for parent run %s (non-fatal)",
            parent_run_id,
            exc_info=True,
        )


#: ``CrawlStats.report()`` numeric fields worth SUMMING across every shard
#: — everything :meth:`CrawlStats.add` accepts, mirrored here rather than
#: introspected, so a report's own key set (which also carries strings,
#: nested dicts and lists) never accidentally gets treated as summable.
_AGGREGATE_COUNTER_FIELDS = (
    "new",
    "changed",
    "unchanged",
    "renamed",
    "deleted",
    "errors",
    "drives",
    "scopes",
    "oversize_files",
    "convert_failed",
    "anonymize_failed",
    "permission_skips",
    "delta_resyncs",
    "excluded_subtree_skips",
    "filtered_by_age",
    "age_unknown",
    "item_retry_given_up",
    "item_retry_recovered",
    "skipped_doomed",
)
#: Itemized lists worth CONCATENATING (then capping — see
#: :data:`_AGGREGATE_LIST_CAP`) across every shard.
_AGGREGATE_LIST_FIELDS = ("scope_errors", "failed_items", "skipped_items", "skipped_doomed_items")
#: Mirrors :data:`_FAILED_ITEMS_CAP` — an honestly-truncated aggregate beats
#: an unbounded one, same contract ``cap_skips`` already gives a single run.
_AGGREGATE_LIST_CAP = _FAILED_ITEMS_CAP


def _aggregate_child_reports(children: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Fold every shard child's own ``report`` (``CrawlStats.report()``'s
    shape, read off ``extraction_runs.report``) into ONE report for the
    PARENT row — the finalizer's own aggregation (design §4.3).

    Numeric counters sum (:data:`_AGGREGATE_COUNTER_FIELDS`);
    ``scope_errors``/``failed_items``/``skipped_items`` concatenate, capped
    (:data:`_AGGREGATE_LIST_CAP`) — a truncated list stays VISIBLY
    truncated, the same contract :func:`connectors.sharepoint.crawler.
    CrawlStats.report` already gives a single run.

    ``status`` (popped by the caller before the aggregated dict becomes the
    persisted ``report``): ``failed`` if ANY child failed, else
    ``interrupted`` if any child was interrupted, else ``done`` —
    severity-first, the same precedence :class:`_RunRecorder`'s own
    docstring already uses for a single run.

    Also carries ``shards``: one summary row per child (``run_id``,
    ``shard_key``, ``shard_label``, ``status``, ``files_seen``,
    ``files_done``, ``error``) — the read side's per-shard disclosure
    (Task 8) reads this rather than re-querying every child individually.
    """
    counters: Dict[str, int] = {key: 0 for key in _AGGREGATE_COUNTER_FIELDS}
    lists: Dict[str, List[Any]] = {key: [] for key in _AGGREGATE_LIST_FIELDS}
    files_seen = 0
    files_done = 0
    any_failed = False
    any_interrupted = False
    shards_out: List[Dict[str, Any]] = []

    for child in children:
        report = child.get("report") or {}
        for key in _AGGREGATE_COUNTER_FIELDS:
            value = report.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                counters[key] += int(value)
        for key in _AGGREGATE_LIST_FIELDS:
            value = report.get(key)
            if isinstance(value, list):
                lists[key].extend(value)
        files_seen += int(child.get("files_seen") or 0)
        files_done += int(child.get("files_done") or 0)
        child_status = child.get("status")
        if child_status == "failed":
            any_failed = True
        elif child_status == "interrupted":
            any_interrupted = True
        shards_out.append(
            {
                "run_id": child.get("id"),
                "shard_key": child.get("shard_key"),
                "shard_label": child.get("shard_label"),
                "status": child_status,
                "files_seen": child.get("files_seen"),
                "files_done": child.get("files_done"),
                "error": child.get("error"),
            }
        )

    for key in _AGGREGATE_LIST_FIELDS:
        lists[key] = lists[key][:_AGGREGATE_LIST_CAP]

    if any_failed:
        status = "failed"
    elif any_interrupted:
        status = "interrupted"
    else:
        status = "done"

    aggregated: Dict[str, Any] = dict(counters)
    aggregated.update(lists)
    aggregated["files_seen"] = files_seen
    aggregated["files_done"] = files_done
    aggregated["shards"] = shards_out
    aggregated["shards_total"] = len(children)
    aggregated["status"] = status
    return aggregated


def _finalize_site_run(connection: Dict[str, Any], parent_run_id: str) -> None:
    """Aggregate every child's report into the PARENT run row, run the
    chained facts pass ONCE, and finish the parent (design §4.3).

    Runs on whichever child won :meth:`ExtractionRunsPgRepository.
    claim_finalize` — by construction only ever reached once
    ``shards_done == shards_total``, so every child is terminal by the time
    this starts; there is nothing left to wait for. Best-effort around the
    final write (never raises past this function — a caller mid-shard-
    finish must not crash on the finalizer's own bookkeeping failing), but
    the facts stage's own hard stop is recorded honestly (``status``
    downgrades to ``failed`` and the parent's ``error`` names it), matching
    the inline crawl's own severity-first posture.
    """
    connection_id = str(connection["id"])
    from src.repositories import extraction_runs_repo

    repo = extraction_runs_repo()
    children = repo.children_for([parent_run_id]).get(parent_run_id, [])
    aggregated = _aggregate_child_reports(children)
    aggregated["connection_id"] = connection_id
    status = aggregated.pop("status")

    state = load_state(connection_id)
    state["last_run"] = aggregated
    # The legacy connection-level `ctags` seed (`_ScopeContext.legacy_
    # ctags`) has now served its purpose for every shard that could ever
    # consult it — cleared ONCE, on the first fully-done sharded run, so a
    # future resync (or re-plan) never re-seeds from a cursor every shard's
    # own row has long since superseded.
    if state.get("ctags"):
        state["ctags"] = {}
    save_state(connection_id, state)

    error: Optional[str] = None
    facts_report: Optional[Dict[str, Any]] = None
    if status != "failed":
        try:
            from connectors.sharepoint.facts_extraction import _standalone_timeout_seconds

            deadline = _Deadline(_standalone_timeout_seconds())
            facts_recorder = _RunRecorder(connection_id, sweep_stale=False)
            facts_recorder.run_id = parent_run_id  # already open — only ever FINISHED below
            facts_stats = CrawlStats()
            facts_report = maybe_run_facts_extraction(
                connection, deadline=deadline, stats=facts_stats, recorder=facts_recorder
            )
        except BaseException as exc:  # noqa: BLE001 — a facts hard-stop still finalizes the parent, honestly
            error = f"{type(exc).__name__}: {exc}"
            status = "failed"

    if facts_report is not None:
        aggregated["facts"] = facts_report
        aggregated["facts_usage"] = facts_report.get("facts_usage") or {}

    try:
        from src.repositories.extraction_runs_pg import cap_skips

        repo.finish(
            parent_run_id,
            status=status,
            report=aggregated,
            usage={},
            skips=cap_skips(aggregated.get("failed_items") or [], total=len(aggregated.get("failed_items") or [])),
            files_seen=aggregated.get("files_seen"),
            files_done=aggregated.get("files_done"),
            error=error,
        )
        logger.info(
            "sharepoint crawl: connection %s — parent run %s finalized (%s, %d shard(s))",
            connection_id,
            parent_run_id,
            status,
            aggregated.get("shards_total"),
        )
    except Exception:  # noqa: BLE001 — never load-bearing; see module-wide recorder posture
        logger.warning(
            "sharepoint crawl: could not finalize parent run %s for connection %s (non-fatal)",
            parent_run_id,
            connection_id,
            exc_info=True,
        )


# --------------------------------------------------------------------------
# Read-only shard-plan preview (2026-09-03 auto-parallel-crawl design §4.7,
# plan Task 9) — `GET .../connections/{id}/shard-plan`
# (`app/api/admin_sharepoint.py`). Mirrors `_plan_or_run_inline`'s own
# planning decision so a preview and what an actual trigger would build
# from can never disagree, but NEVER enqueues a job, opens a run row, or
# falls back to "inline" on a Graph hiccup — a preview that silently
# swallowed an error would just show a stale/empty plan instead of naming
# what went wrong.
# --------------------------------------------------------------------------


async def preview_shard_plan(
    connection: Dict[str, Any],
    *,
    only_scope_ids: Optional[List[str]] = None,
    min_modified_override: Optional[date] = None,
) -> Dict[str, Any]:
    """Read-only preview of the automatic parallel-crawl plan for this
    connection's site.

    Runs the IDENTICAL two decisions :func:`_plan_or_run_inline` makes
    before it ever enqueues anything (:func:`connectors.sharepoint.
    shard_plan.compute_shard_plan`, over the same resolved
    :func:`_drive_targets` AND the same :func:`_known_folder_counts` cheap
    signal), so a FRESHLY COMPUTED preview and the plan an actual trigger
    would build from never disagree. The one deliberate divergence
    (2026-09-04 finding #65 item 2): this NEVER consults or reuses the
    connection's persisted plan — a preview answering from a stale cached
    plan while claiming to show "right now" would defeat its own purpose;
    an actual trigger reuses it (see :func:`_reusable_shard_plan`) purely
    for speed, never for correctness. Unlike :func:`_plan_or_run_inline`
    this never falls back to "inline" on a Graph error — a scope-resolution
    or Graph failure propagates as :class:`CrawlError`/
    :class:`SharePointGraphError` for the caller
    (``app.api.admin_sharepoint``) to translate into its own typed ``502``,
    rather than a preview that silently looks like a small, healthy site.

    Returns ``{"mode": "inline"|"sharded", "target_docs": int, "signal":
    str, "shards": [...], "loose_root_files": [...]}``. ``mode ==
    "inline"`` — with ``shards`` empty — exactly when
    :func:`_plan_or_run_inline` would also stay inline: the active backend
    is DuckDB (A3 ratchet), ``extraction.crawler.shard_target_docs`` is
    ``0``, there is no confirmed scope to plan against, or the connection's
    summed total stays at or under the target across every scope's every
    drive. Each sharded entry: ``{"drive_id", "index", "label", "signal",
    "expected", "targets_count"}`` — ``signal`` (2026-09-04 finding #65
    item 1 — one of ``"known"``/``"child_count"``/``"search"``/``"none"``)
    is the WORST (least certain) counting signal behind this shard's own
    ``expected``, so the UI can say "≈" honestly per shard, not just for
    the plan as a whole. ``targets_count`` (never the raw ``targets`` list
    itself — no Graph item ids leave this module) is how many delta units
    this shard packs. ``loose_root_files`` concatenates every drive's own
    root-level files no folder-based shard will cover (design §4.1 point 3
    — the remainder shard still gets them at crawl time; this is preview-
    only visibility).

    ``min_modified_override`` narrows every document count to this date
    or later, for THIS preview call only — the exact "what if I backfilled
    from here" question ``GET …/split-plan?min_modified=`` already answers
    for the manual planner. ``None`` (the default) resolves the
    connection's own configured DEFAULT cutoff (:func:`resolve_min_modified`,
    no ``scope=``) uniformly across every scope in the plan — a scope with
    its OWN override (TCRD-296 gap #80) is undercounted/overcounted here the
    same bounded, display-only way :func:`_plan_or_run_inline`'s own shard
    planning is (see its comment) — the actual triggered run still applies
    each scope's correct effective filter regardless of what this preview
    estimated.
    """
    target_docs = _shard_target_docs()

    from src.repositories import use_pg

    if not use_pg() or target_docs <= 0:
        return {
            "mode": "inline",
            "target_docs": target_docs,
            "signal": SIGNAL_NONE,
            "shards": [],
            "loose_root_files": [],
        }

    scopes = _confirmed_scopes(connection)
    if only_scope_ids:
        wanted = set(only_scope_ids)
        scopes = [s for s in scopes if str(s.get("source_scope_id")) in wanted]
    if not scopes:
        return {
            "mode": "inline",
            "target_docs": target_docs,
            "signal": SIGNAL_NONE,
            "shards": [],
            "loose_root_files": [],
        }

    settings = resolve_sharepoint_settings(connection)
    if min_modified_override is not None:
        min_modified = min_modified_override
    else:
        min_modified, _min_modified_source = resolve_min_modified(connection)
    stats = CrawlStats()
    auth = GraphAuth(
        acquire=lambda: graph_client.get_app_token(settings.tenant_id, settings.client_id, settings.private_key),
        stats=stats,
    )
    transport = GraphTransport(auth, stats)

    all_targets: List[DriveTarget] = []
    for scope in scopes:
        all_targets.extend(await _drive_targets(transport, scope))

    if not all_targets:
        return {
            "mode": "inline",
            "target_docs": target_docs,
            "signal": SIGNAL_NONE,
            "shards": [],
            "loose_root_files": [],
        }

    known_totals, known_folder_counts = _known_folder_counts(scopes)
    plan = await compute_shard_plan(
        transport,
        auth,
        {},
        all_targets,
        min_modified=min_modified,
        target_docs=target_docs,
        max_shards=_MAX_SHARDS,
        known_totals=known_totals,
        known_folder_counts=known_folder_counts,
    )

    if not plan["drives"]:
        return {
            "mode": "inline",
            "target_docs": target_docs,
            "signal": plan["signal"],
            "shards": [],
            "loose_root_files": [],
        }

    shards_out: List[Dict[str, Any]] = []
    loose_root_files: List[str] = []
    for drive_plan in plan["drives"]:
        loose_root_files.extend(drive_plan.get("loose_root_files") or [])
        for shard in drive_plan["shards"]:
            if not shard["targets"]:
                continue
            shards_out.append(
                {
                    "drive_id": drive_plan["drive_id"],
                    "index": shard["index"],
                    "label": shard["label"],
                    "signal": shard.get("signal") or SIGNAL_NONE,
                    "expected": shard.get("expected") or 0,
                    "targets_count": len(shard["targets"]),
                }
            )

    return {
        "mode": "sharded",
        "target_docs": target_docs,
        "signal": plan["signal"],
        "shards": shards_out,
        "loose_root_files": loose_root_files,
    }
