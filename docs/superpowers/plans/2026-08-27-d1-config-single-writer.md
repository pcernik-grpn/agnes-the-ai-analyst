# D1 Residual: Config Single Writer (`data_source.type`) — Design Note

**Status:** implemented in this slice (not a forward plan — a short record of
the reframe + the residual work, for anyone tracing D1 later).

**Goal (D1's original framing):** "provisioning stops rewriting UI-owned
settings; the overlay is the single writer." A 2026-08 audit against
`main@b62bf91eb` found this only ~30% done. D1 slice 1
(`3cf9fd628`, `docs/superpowers/plans/2026-08-24-agnes-remediation-program.md`
§Track D/D1) closed the rest of the always-wins `.env` knobs
(`theme`/`experience`/`home_route`/`studio_enabled`) and deleted dead config
surfaces — leaving D1 at ~70% shipped. This note is the residual: the one
live dual-config bug that slice left behind, plus a re-audit of the
dead-section deletion the same slice claimed.

## Reframe: what was actually left

The prior investigation's brief for this slice assumed two open items. Only
one was still open on current `main` (re-verified before touching anything,
per this repo's issue-economy convention — a stale premise gets closed as
moot, not re-executed):

1. **`data_source.type` was still bootstrap-env.** `variables.tf`'s
   `data_source` var fed an always-wins `DATA_SOURCE=$DATA_SOURCE` line in
   `startup-script.sh.tpl`'s `.env` heredoc, rewritten on every boot/apply/
   auto-upgrade tick — silently reverting a UI change to
   `data_source.type`, the exact bug class D1 slice 1 fixed for the other
   four knobs. **Open — fixed in this slice.**
2. **`get_data_source_type()` read env before overlay.** Even after (1), a
   VM whose `.env` still carried a stale `DATA_SOURCE` line from before this
   change (or a running container with that value already baked into its
   process environment) would keep shadowing a UI edit until the container
   was recreated. **Open — fixed in this slice.**
3. **The dead `jira:` editable-config section.** The brief cited
   `app/api/admin.py:~515`/`~581` as still carrying `"jira"` in
   `_STATIC_EDITABLE_SECTIONS` / `_SECTION_BASELINE_EFFECT`. Re-grepping
   current `main` found neither dict contains `"jira"` — D1 slice 1
   (`3cf9fd628`) already deleted it, along with `email.from_name`,
   `admins:`, and `server.ssh_*` (confirmed zero residue for all three via a
   fresh grep). **Already shipped — audited and closed as moot, not
   re-done.** A regression test (`test_post_jira_section_rejected` in
   `tests/test_admin_server_config.py`) was added anyway, since no test
   previously pinned this deletion — the assertion is cheap insurance
   against the dict silently regaining the key.

## The two real changes

**Infra (`infra/modules/customer-instance/`):** the `.env` heredoc drops the
`DATA_SOURCE=$DATA_SOURCE` line. `var.data_source` still flows into the
startup script template (it gates the one boot-time decision of whether to
fetch the `keboola-storage-token` Secret Manager secret — an operational
concern, not a presentation choice) but no longer writes an env line. A new
`instance_data_source_map` local folds `{ type = var.data_source }` into the
same `instance_branding_b64` first-boot-only seed blob that
`theme`/`experience`/`home_route`/`studio_enabled` already ride — exact same
mechanism, no new plumbing. `variables.tf`'s `data_source` description now
says "first-boot seed" instead of implying an always-wins env line.

**App (`app/instance_config.py::get_data_source_type()`):** resolution order
flips from `DATA_SOURCE env > data_source.type overlay > "local"` to
`data_source.type overlay > DATA_SOURCE env > "local"` — the one deliberate
exception to this module's usual env-wins-everything rule. Without this
flip, fixing only the infra side would still leave a gap: an existing VM's
`.env` (written before this change shipped, or a container that hasn't been
recreated yet) keeps a `DATA_SOURCE` line the new startup script no longer
writes but also doesn't remove from an already-materialized file, and the
env-wins rule would let that stale value keep shadowing a UI edit
indefinitely. Flipping precedence closes that gap without needing every
customer VM to be recreated. `DATA_SOURCE` remains a fallback when the
overlay has no value at all, so local-dev (`DATA_SOURCE=keboola` in a laptop
`.env`, no `instance.yaml` overlay) is unaffected.

## BREAKING note (infra pins)

Any downstream root module or fork of `infra/modules/customer-instance` that
pins an `infra-vX.Y.Z` tag predating this change and relies on
`var.data_source` re-asserting `DATA_SOURCE` into `.env` on every apply must
move that expectation to `/admin/server-config` → *Data source* (or a
first-boot re-seed of `instance.yaml`) after upgrading. This mirrors D1 slice
1's `theme`/`experience`/`home_route`/`studio_enabled` BREAKING note exactly
— same shape of change, same operator remediation.

## Deferred (explicitly out of scope here)

A real DB-backed config table — replacing the `instance.yaml` overlay file
entirely — is **not** part of this slice. It only becomes necessary for
cross-process live-reload under a role-split deployment (api/gateway/worker
as separate processes), where the in-process `reset_cache()` this repo
already has cannot reach the other processes. `data_source` is currently
classified `restart` for exactly this reason
(`app/api/admin.py::_SECTION_BASELINE_EFFECT["data_source"]`), and that
classification is unchanged by this slice — the precedence flip here fixes
which value eventually wins, not how fast every process learns about it.
That's D2/D3 territory (`docs/superpowers/plans/2026-08-24-agnes-remediation-program.md`),
not D1.

## Non-goals (scope discipline)

- No change to `data_source.{keboola,bigquery,snowflake,databricks}.*`
  connection settings (credentials, stack URL) — that consolidation is D2
  (`docs/superpowers/plans/2026-08-26-derived-connection-model.md`).
- No change to the DuckDB/BigQuery router union logic that consumes
  `get_data_source_type()`'s return value.
- No removal of the `data_source.type` scalar itself.
