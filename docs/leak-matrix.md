# Leak matrix — persona × resource sweep

`scripts/leak_matrix.py` presents each persona's own credential to the real
read surfaces and records what actually comes back. Run it after every grant
change.

## Why not read the grant graph

`/admin/access` and the `effective-access` diagnostic behind it describe
**intent**. The read surfaces **enforce** it, through different code paths.
A review of the graph can therefore come back clean while a persona still
reads something they should not — and every incident of this shape looks the
same afterwards: someone widened a grant (or a default) for one surface, and
nobody re-checked what the other surfaces then answered.

So this sweep does not inspect configuration. It makes real requests as real
personas and compares the answers against what each persona declared it
should reach. It also asks the server, with the persona's own credential,
what it *claims* that persona can read (`GET /api/me/effective-access`) and
diffs the claim against the actual reads — a mismatch there means the audit
view an operator reads during an incident is not describing the enforcement
path, which is worth knowing on its own.

## What it probes

| Surface | Question |
|---|---|
| `GET /api/v2/catalog` | which tables the persona is **offered** |
| `POST /api/query` | whether an undeclared table is **refused** |
| `GET /api/collections` | which collections are listed |
| `GET /api/collections/search` | whether canary text surfaces in content |
| `GET /api/knowledge/search` | the same question on the other index |
| `GET /api/v1/agents` | reachability for an agent-PAT persona |

The query probe is the important one. The catalog is a projection; a table
absent from the catalog but still queryable is exactly the gap a
catalog-only check reports as clean. Listing a collection and reading its
contents are likewise separate gates, which is why canary text is searched
rather than inferred from the listing.

Every request is a `GET`, or a `POST /api/query` whose body is a
`SELECT … LIMIT 1`. The sweep never writes anything. Table names come from a
catalog *response*, so they are quoted through the repo's `quote_ident` like
any other untrusted identifier — run the script from the checkout (it needs
no venv, only `src/sql_ident.py`).

## Findings

| Severity | Meaning |
|---|---|
| `LEAK` | the persona reached something it did not declare |
| `DISAGREEMENT` | the self-audit view and the enforcement path disagree |
| `WRONGLY-DENIED` | a declared resource was refused — a broken grant, not safety |
| `GAP` | part of the matrix did not run (usually a missing credential) |

`GAP` exists so a missing credential can never render as an empty row that
reads like "nothing leaked". A run with gaps is **not** a clean result, and
the report says so. The same severity covers a narrower hole: the query
probe only exercises the tables the catalog offered plus the ones the
persona declared, so a table the self-audit view claims *outside* that set
is untested rather than contradicted — it is reported as a gap, and
declaring it in `expect_tables` turns the claim into a tested one.

## Usage

```bash
python scripts/leak_matrix.py --config leak-matrix.yaml
```

```bash
python scripts/leak_matrix.py --config leak-matrix.yaml --json sweep.json --fail-on-leak
```

`--fail-on-leak` exits 1 on any `LEAK` or `DISAGREEMENT`, for a scheduled
run. Exit 2 means the sweep itself could not run (bad config, unreachable
host).

## Config

See [`config/leak-matrix.example.yaml`](../config/leak-matrix.example.yaml).
Tokens are written as `env:NAME` and read from the environment, so no secret
lands in the file or in the process's argv.

Each persona declares what it *should* reach; anything else that answers is
a finding. Personas worth keeping in the matrix permanently:

- a user from outside the allowed domain (expects nothing)
- a user in no group / with no data package (expects nothing)
- an analyst with exactly one package (expects that package's tables)
- an agent PAT (expects its declared scope — agent PATs only authenticate
  `/api/v1/*`, so the analyst probes are skipped by design and reported as
  not probed)
- a persona for each collection that is meant to be restricted, with a
  canary string from inside a document that must not surface

## Adding a probe

Add it to `sweep_persona` and, in the same change, extend
`tests/test_leak_matrix.py` — it drives the sweep against a real app in
three directions: a correctly-scoped persona (no findings), a persona
reaching more than declared (must be reported), and a declared-but-
unreachable resource (must be reported). A probe that only ever runs against
a correctly-configured instance proves nothing.
