# Maintaining a semantic layer over time

A layer that was correct at import time drifts: a table gets dropped or
renamed in `table_registry`, a metric's SQL stops binding, an object nobody
references anymore just sits there. This is the audit loop — what to check,
with which command, and what a finding means.

## Coverage: does the layer actually reach Agnes?

For instances that sync a semantic layer from an external metastore
(currently Keboola projects), the layer that was *published upstream* and
the layer that *reaches Agnes* can diverge — a dataset's source table may
never have been registered here.

```
agnes admin semantic keboola-import [--json]
```

Per connection, this reports how many published metrics bind to a
registered table, which metrics are blocked by their own definition, and
which datasets have no registered table. Two `warnings[]` entries are worth
acting on immediately:

- `token_project_mismatch` — the connection's storage and master tokens
  resolve to different projects, so tables sync from one project while the
  semantic layer reads from another. No metric can bind while this is true.
- `no_metrics_bound` — the project publishes metrics but none bind to a
  registered table (usually every dataset's source table is unregistered).

`unregistered_tables` in the report is *not* itself a finding — a semantic
layer routinely describes more of a project than an instance chooses to
register. Only chase it down when a specific metric a user expects is
missing.

## Drift: does a document still validate?

A document that validated at import time can be invalidated by an upstream
schema-version bump this instance hasn't picked up, or by a hand-edit that
skipped the CLI's validate step. Re-check any document you're about to rely
on or extend:

```
agnes semantic-model export <slug> -o /tmp/model.yaml
agnes semantic-model validate /tmp/model.yaml
```

Neither needs an admin: `export` reads the public, resource-gated endpoint and
`validate` never contacts a server at all.

`agnes admin semantic list` surfaces `status` per model (an admin read — it is
the only listing that also shows invalid and draft documents; `agnes
semantic-model search <term>` is the any-user one) —
`status='invalid'` on a synced (non-`manual`) model means the last sync
wrote something that failed schema validation; fix it at the source and
resync, never by hand-patching the stored document (an import-owned model
refuses direct edits with `409 source_owned` for exactly this reason — the
next sync would silently revert it).

## Dead objects

An object with no reference anywhere else in the model, and no query ever
observed to use it, is a candidate for removal — but "no reference" needs
checking, not assuming:

1. **Referential check** — does anything point at this object? A dataset
   used only by a metric/relationship that itself looks dead is still live
   until you remove the referrer too; removing the wrong end first breaks
   validation. Use `agnes semantic-model context <type>` to enumerate
   compactly, then `agnes semantic-model context relationship --id <name>`
   / `... metric --id <name>` to check what a candidate dataset feeds.
2. **Usage check** — `validate-query` records `used_datasets`/
   `used_metrics`/`matched_relationships` per call; if your team logs or
   reviews the queries analysts actually run against this layer, cross-
   reference before deleting. There is no built-in usage-tracking store in
   Agnes today — this is a process check, not a command.

Don't delete an object solely because it looks unused in a small sample of
recent queries — confirm with whoever owns the upstream source (for a
synced model) or the team that authored it (for a `manual` model) before
removing something that might be used seasonally or by a report outside
Agnes's visibility.

## Two neighbouring reports that answer different questions

`keboola-import` above predicts what a *live Keboola project* would import.
Two others read what is already stored, and are worth running alongside it:

```
agnes admin semantic coverage [--source <id>] [--json]   # per SOURCE × six domains
agnes admin semantic coverage tables [--limit N]         # per TABLE: no model at all
agnes admin semantic health [--json]                     # sync failures, orphans, invalid docs
```

## Cadence

There's no scheduled audit job for any of the above (unlike sync itself,
which the scheduler runs on `sync_schedule`). Treat coverage + drift checks
as something to run: after any Keboola project semantic-layer change lands
upstream, after a `spec_version` bump in this repo, and periodically (e.g.
whenever `/admin/semantic-layer` shows a sync error) rather than never.
