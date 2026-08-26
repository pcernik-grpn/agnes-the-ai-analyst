# Agent Workspace Prompt

The agent workspace prompt is the `CLAUDE.md` file written to each analyst's
workspace by `agnes init`. It gives Claude Code context about the
connected instance: available tables (RBAC-filtered), business metrics, installed
plugins, and operational rules for the analyst.

## When is CLAUDE.md written?

`agnes init` fetches `GET /api/welcome` and writes the rendered markdown
to `<workspace>/CLAUDE.md` on every run (including `--force` re-initialisation).

On **default** workspaces (no Initial Workspace Template), `agnes update` also
refreshes `CLAUDE.md` from `/api/welcome`: if the file already exists and the
server content changed, Agnes writes a `CLAUDE.md.bak.<timestamp>` backup
before overwriting (an unchanged template is a no-op). Because the SessionStart
hook runs a detached `agnes update --quiet`, an admin's prompt edit now
propagates **automatically** on the analyst's next Claude Code session — not
only on the next `agnes init`. (Initial Workspace Template workspaces get
`CLAUDE.md` from the template repo instead — see
[`initial-workspace-override.md`](initial-workspace-override.md).)

To skip writing CLAUDE.md on init:

```bash
agnes init --server-url https://agnes.example.com --no-claude-md
```

**Analysts who ran setup while CLAUDE.md generation was temporarily absent** will
have their file written on the next `agnes init` or `agnes update` run. Any
existing `CLAUDE.md` is backed up (by `agnes update`) and replaced with the
current server template.

The companion `CLAUDE.local.md` (at `.claude/CLAUDE.local.md`) is **never**
overwritten — it is the analyst's personal customisation space.

## Editing the template

Admins configure the template via:

- **Admin UI:** `/admin/workspace-prompt` — Jinja2 markdown editor with a
  placeholder cheatsheet, live preview (rendered against the calling admin's
  RBAC context), and save/reset actions.
- **REST API:**
  - `GET /api/admin/workspace-prompt-template` — returns
    `{content, default, updated_at, updated_by}`. `content` is `null` when no
    override is set; `default` is always the live rendered default.
  - `PUT /api/admin/workspace-prompt-template` with body `{"content": "..."}` —
    validates Jinja2 syntax against two stubs (authenticated user, minimal user)
    before persisting. Returns `400` on syntax errors or unknown placeholders.
  - `DELETE /api/admin/workspace-prompt-template` — clears the override; reverts
    to the rich default template from `config/claude_md_template.txt`.
  - `POST /api/admin/workspace-prompt-template/preview` with
    body `{"content": "..."}` — renders arbitrary content against the calling
    admin's live RBAC context without persisting. Used by the editor's Preview
    button.

The override lives in `system.duckdb` (table `claude_md_template`, singleton
row id=1). `DELETE` NULLs `content`; audit trail (`updated_at`, `updated_by`)
is preserved.

## Default template

The default template is `config/claude_md_template.txt` (Jinja2 markdown).
When no admin override is set, this file is rendered for every `GET /api/welcome`
request. Operators can customise it per-instance via the UI — or ship a modified
default by editing the file before deployment.

## Template language

[Jinja2](https://jinja.palletsprojects.com/) with `autoescape=False` and
`StrictUndefined`. Autoescape is off because the rendered output is markdown, not
HTML. `StrictUndefined` means any typo in a placeholder name raises an error at
PUT validation time, so the admin is notified immediately.

## Available placeholders

| Placeholder | Type | Notes |
|---|---|---|
| `instance.name` | string | `instance.name` from `instance.yaml` |
| `instance.subtitle` | string | `instance.subtitle` from `instance.yaml` |
| `server.url` | string | Full server URL at render time |
| `server.hostname` | string | Host part only |
| `sync_interval` | string | e.g. `"1h"` from `instance.yaml` |
| `data_source.type` | string | `keboola`, `bigquery`, or `local` |
| `data_source.source_types` | list[str] | Sorted distinct `source_type` values of the tables visible to the calling user. Use this (together with `data_source.type`) to gate engine-specific guidance — a multi-source instance can register e.g. BigQuery or Databricks rows while `type` stays `local` |
| `tables` | list[dict] | RBAC-filtered list of `{name, description, query_mode, source_type}` |
| `metrics.count` | int | Total metric definitions in DB |
| `metrics.categories` | list[str] | Sorted unique category names |
| `semantic_layer.has_models` | bool | `True` iff the calling user can read at least one valid semantic model |
| `marketplaces` | list[dict] | RBAC-filtered `{slug, name, plugins:[{name}]}` |
| `user.id` | string | Analyst user ID |
| `user.email` | string | Analyst email |
| `user.name` | string | Analyst display name |
| `user.is_admin` | bool | Whether the user is in the Admin group |
| `user.groups` | list[str] | User's group names |
| `now` | datetime (UTC, tz-aware) | Server time at render |
| `today` | string (`YYYY-MM-DD`) | Server date |
| `is_sandbox` | bool | `True` when rendering for the ephemeral chat sandbox, `False` for a laptop workspace (`GET /api/welcome`, the default). Use this to branch wording that differs by surface — e.g. whether a saved file path is reachable by the user. |

## Example: iterating tables

```jinja2
## Available Datasets
{% for t in tables -%}
- `{{ t.name }}`{% if t.description %} — {{ t.description }}{% endif %}
{% else -%}
- _No tables registered yet._
{% endfor %}
```

## Example: conditional marketplace section

```jinja2
{% if marketplaces %}
## Plugins
{% for mp in marketplaces %}
- **{{ mp.name }}**: {{ mp.plugins | map(attribute="name") | join(", ") }}
{% endfor %}
{% endif %}
```

## Resetting to the built-in default

Click **Reset to default** in the admin UI, or call
`DELETE /api/admin/workspace-prompt-template`. The next analyst who runs
`agnes init` will receive the rich default template from
`config/claude_md_template.txt`.
