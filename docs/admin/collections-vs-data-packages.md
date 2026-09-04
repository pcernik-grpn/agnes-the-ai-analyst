# Collections or a data package? — choosing how knowledge enters Agnes

Both surface under **Library**, which is why they get confused. Library is not a thing you choose *instead of* a data package — it is the shelf that holds every artifact type: data packages, Collections (files), memory domains, skills, agents, plugins, and recipes, each as a `?section=` band.

The real choice is between two ways of getting content in:

| | **Collection** (files) | **Data package** (tables) |
|---|---|---|
| What it holds | Uploaded documents — PDF, Markdown, text | Registered tables from a live source |
| How content arrives | A person uploads a file | The connector syncs on a schedule |
| How it stays current | It doesn't. Someone re-uploads. | Next scheduled sync |
| How it's read | Hybrid search over chunks, with citations | SQL, via `agnes query` or an agent |
| Good for | Policies, contracts, handbooks, specs, decks | Anything a system of record already owns |

## The question that settles it

**"When this content changes, who updates it here?"**

If the answer is a person, a Collection is honest about that — it is a file corpus, and a stale file is visibly a stale file. If the answer is "a system already knows", uploading an export is the wrong shape: you have made a copy that starts drifting the moment it lands, and nothing will tell you it drifted. Register the source and let a data package carry it.

This is why an export of a live report is the classic mistake. The spreadsheet is accurate on upload day and quietly wrong a month later, and search results cite it with the same confidence either way.

## Two things worth knowing before you choose

**Ingestion is per-file and manual to repair.** A file that extracts to nothing lands in `needs_review`; a rejected one stays rejected. Fixing it means re-running ingestion for that file (`POST /api/collections/{id}/files/{file_id}/reingest`, `collections_reingest` over MCP). Nobody is watching that queue for you — at scale, this is the maintenance cost people underestimate.

**A data package is a bundle, not a permission.** Adding tables to a package doesn't grant anything; the grant is `(group, data_package, <id>)`, and the package then reaches an analyst's stack — automatically if the grant is marked required, otherwise once they subscribe. Access to the underlying tables still resolves through [RBAC](../RBAC.md).

## When both are right

They compose, and the combination is usually better than either alone: the package carries the numbers, a Collection carries the document that says what the numbers mean. An agent asked "why did churn spike in March?" can query the table *and* cite the incident write-up. Keep the boundary clean — the table is the source of truth for values, the document for intent.

## See also

- [`query-modes.md`](query-modes.md) — `local` / `remote` / `materialized`, and `server_only` for tables that stay on the server
- [`../RBAC.md`](../RBAC.md) — granting a package or a collection to a group
- [`../api-reference.md`](../api-reference.md) §4 — data package endpoints and editable fields
