---
description: Scope-gated PR/diff review. Detects which reviewers the diff touches, runs them as a Team in parallel, consolidates into one advisory report (file:line + severity, ≤15 findings). Read-only working tree; optionally posts ONE comment-only review when an open PR exists. Never approves, requests changes, merges, or edits.
allowed-tools: Task, Bash, Read, Grep, TeamCreate, TeamDelete, TaskCreate, TaskUpdate, TaskList, TaskGet, SendMessage
argument-hint: "[optional base ref] [--adversarial]"
---

# /agnes-review — scope-gated review team

## 1. Resolve scope

```bash
ADVERSARIAL=false
BASE_ARG="$ARGUMENTS"
if [[ "$BASE_ARG" == *--adversarial* ]]; then
  ADVERSARIAL=true
  BASE_ARG="$(echo "$BASE_ARG" | sed 's/--adversarial//' | xargs)"
fi
BASE="${BASE_ARG:-$(git merge-base origin/main HEAD)}"
git diff --name-only "$BASE"...HEAD
```

Map changed paths → in-scope reviewers:

| Reviewer | Fires when a changed path matches |
|---|---|
| `agnes-reviewer-rules` | always |
| `agnes-reviewer-adversarial` | only when `--adversarial` was passed |
| `agnes-reviewer-architecture` | `src/orchestrator.py`, `src/db.py`, `connectors/*/extractor.py`, `connectors/*/extract_init.py`, new `connectors/**` |
| `agnes-reviewer-rbac` | `app/api/`, `app/auth/`, `app/resource_types.py` |
| `agnes-reviewer-parity` | `src/repositories/`, `src/db.py`, `migrations/`, `tests/db_pg/` |

`agnes-reviewer-rules` always runs. `agnes-reviewer-adversarial` runs its
deep checks (real test runs, whole-repo greps, CI history) only when the
caller opts in with `--adversarial` — it is slow, so it's not part of the
default fast pass. The remaining reviewers run only if their paths matched.

## 2. Run the team

```
TeamCreate(team_name="agnes-review", description="Scope-gated review")
# one TaskCreate per in-scope reviewer (no deps)
# TaskCreate(consolidator) with addBlockedBy = [<reviewer task ids>]
```

Spawn each in-scope reviewer via the Task tool: `subagent_type` = the reviewer
name, `team_name="agnes-review"`, `run_in_background=true`, prompt = "Review
`$BASE`...HEAD per your playbook. Return findings in your own output format. Read-only." Pass `$BASE`.

Wait for all reviewer tasks to complete, then spawn `agnes-review-consolidator`
(`subagent_type="agnes-review-consolidator"`) with all reviewer findings.

## 3. Output

Print the consolidator's report to the terminal. Then check for an open PR:

```bash
gh pr view --json number,state 2>/dev/null
```

If an OPEN PR exists, ask the user whether to post; on yes, post ONE comment-only
review:

```bash
gh pr review <N> --comment --body-file <report.md>
```

NEVER `--approve`, `--request-changes`, `gh pr merge`, `git push`, or edit files.

## 4. Clean up

`TeamDelete(team_name="agnes-review")`. The working tree is untouched — fixes are
a separate follow-up step (main agent or `agnes-builder`).
