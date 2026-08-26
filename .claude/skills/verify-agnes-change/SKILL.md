---
name: verify-agnes-change
description: Verification loop for a change in this repo — run the deterministic sync-map + guard checks, fix what fails, re-run until clean, and only then spend an LLM review. Use before claiming a change is done, before committing, and before opening or updating a PR.
allowed-tools: Bash, Read, Edit, Grep
---

# verify-agnes-change

A verification loop: check your own work, fix what fails, check again. Stop only
when a gate passes — never because you ran out of patience with it.

The gates are ordered by **cost, cheapest first**. That ordering is the whole
point: every finding a script can produce is a finding an LLM reviewer should
never have to spend tokens on. `/agnes-review` is the last gate, and by the time
it runs it should only be looking at things that need judgment — architecture,
coupling, whether an abstraction earns its keep.

## The loop

For each gate in order:

1. Run the command.
2. If it fails → fix the cause, then **re-run that same gate**. Do not advance.
3. When it passes, move to the next gate.

Fixing a later gate can break an earlier one. If you edit code after gate 2,
restart from gate 1 — it costs seconds.

Never report a gate as passing from memory or inference. Paste the command's
actual output. If you did not run it, say so.

## Gate 1 — sync-map (instant, no venv)

```bash
python3 scripts/verify_syncmap.py
```

Covers the `CONTRIBUTING.md` sync-map rows that have **no CI guard**: an
unregistered `ResourceType`, a missing `## [Unreleased]` CHANGELOG bullet, a new
boolean scope flag, `query_mode='remote'` without `_remote_attach`, and (as a
WARN) a new entity-scoped endpoint with no authz dependency.

- Exit 1 = at least one BLOCKING finding. Fix it; these are all mechanical.
- WARN findings need a decision, not obedience. An entity-scoped route that
  authorizes inside the handler body is fine — confirm that it does, then move
  on. Say in your summary which WARNs you cleared and why.
- Exit 2 = the verifier could not run (bad base ref, not a git repo). That is a
  broken gate, not a pass.

Default base is the merge-base with `origin/main`. Use `--base HEAD` to check
only uncommitted work mid-task, and `--json` when you want to process findings
programmatically.

## Gate 2 — the guards your diff touches

These are static (AST/inspection) but not free — pick by what you changed
instead of running all of them:

| You touched | Run |
|---|---|
| `src/repositories/`, `src/db.py`, `migrations/` | `tests/db_pg/test_repo_method_parity.py tests/test_repository_registry.py tests/test_backend_split_guard.py tests/test_db_schema_version.py` |
| `app/api/`, `app/auth/` | `tests/test_route_auth_guard.py tests/test_documentation_api_triple_surface.py tests/test_api_docs_coverage.py` |
| `cli/`, MCP tools | `tests/test_cli_api_parity.py tests/test_mcp_tool_parity.py` |
| `app/web/`, templates | `tests/test_design_system_contract.py` |
| `connectors/` | `tests/test_verify_syncmap.py` plus the connector's own tests |

```bash
.venv/bin/pytest <selected files> --tb=short -q
```

These guards are ratchets. When one fails on a *pre-existing* entry rather than
your change, do not widen its allow-list to make it quiet — that converts a
guard into decoration. Confirm on a clean tree (`git stash`) and note it.

## Gate 3 — the tests for the behavior you changed

Run the tests covering the code you touched, then the full suite — the same
command CI runs:

```bash
.venv/bin/pytest tests/ connectors/ --tb=short -n auto -q
```

Failures in code you touched: fix before pushing. Failures unrelated to your
diff: confirm with `git stash` that they reproduce on a clean branch, note them
in the PR body, do not block on them.

## Gate 4 — review (judgment only)

```
/agnes-review
```

Only now. Gates 1–3 have already removed everything mechanical, so treat any
finding here that a script could have caught as a gap in gate 1 — and say so, so
the check gets added rather than re-litigated by hand next time.

Then apply the repo's standing review rules: fix findings, resolve the
corresponding review threads, and re-run this loop from gate 1 before merge.

## When a gate has no check

If you find yourself hand-verifying the same invariant twice, it belongs in
`scripts/verify_syncmap.py`, not in your working memory. The bar for adding one:
it must be decidable from the diff without judgment, and it must fail loudly
with `file:line` and the mirror surface that is missing. Add the detector plus
its tests in `tests/test_verify_syncmap.py` — both positive and negative cases,
so the next person can tell a real rule from a lucky regex.

Rules that genuinely need judgment stay with the reviewers. Do not turn a
reviewer into a linter, or a linter into a reviewer.
