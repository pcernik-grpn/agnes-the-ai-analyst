---
description: Build a plan in parallel. Decomposes a writing-plans plan into independent tasks (respecting sync-map coupling), implements each in its own git worktree via agnes-builder, integrates the results (migration serialized last), then runs /agnes-review on the unified diff. For genuinely entangled work it falls back to fewer/coarser tasks.
allowed-tools: Task, Bash, Read, Grep, Glob, TeamCreate, TeamDelete, TaskCreate, TaskUpdate, TaskList, TaskGet, SendMessage
argument-hint: "<path to a writing-plans plan, or a task description>"
---

# /agnes-build — parallel build team

Orchestrate a parallel implementation. `$ARGUMENTS` is a plan path or a task
description.

## 1. Decompose

Spawn `agnes-decomposer` (`subagent_type=agnes-decomposer`) with the plan/description
+ a pointer to `CONTRIBUTING.md`. It returns a JSON task graph (independent tasks,
`migration_task_id`, merge-magnet bullets handled out-of-band).

If the decomposer returns a single task (work didn't parallelize), just run it
inline via `agnes-builder` and skip the team — don't pay orchestration overhead.

## 2. Build in parallel worktrees

```
TeamCreate(team_name="agnes-build", description="Parallel build")
```

For each task, spawn an `agnes-builder` via the Task tool with
`subagent_type=agnes-builder`, `team_name="agnes-build"`, `isolation="worktree"`,
`run_in_background=true`. Prompt = the task's title + files + the rule "commit your
work in this worktree; do NOT edit CHANGELOG.md or CLAUDE.md — emit your bullet in
your report instead." Honor `depends_on` (don't start a task until its deps finish).

**Worktree isolation is what makes this safe** — each builder edits its own copy,
so parallel file writes can't collide. The cost is that the diffs are in separate
worktrees and must be integrated (next step).

## 3. Integrate

When all builders finish, spawn `agnes-integrator`
(`subagent_type=agnes-integrator`) with the task graph + each worktree's
path/branch/commit. It applies tasks in dependency order, lands the migration task
LAST, writes the `changelog.d/` fragment + CLAUDE.md bullets, and STOPS on any non-merge-magnet
conflict (reporting the colliding tasks). If it stops, surface that to the user —
the decomposer under-coupled; do not force-merge.

## 4. Review

Run `/agnes-review` on the unified diff (the integrated result on the target
branch). Relay its advisory report.

## 5. Clean up

`TeamDelete(team_name="agnes-build")`. Worktrees that were integrated can be
removed; leave any that the integrator flagged unresolved for the user.

## Honest limitations

- Cross-worktree integration is the hard part; if tasks share non-magnet files the
  integrator will stop rather than guess. Prefer coarser tasks over racy ones.
- At most ONE migration task per run (the `SCHEMA_VERSION` ladder is serial).
- This orchestrates `agnes-builder`, which is itself disciplined (TDD, parity,
  CHANGELOG) — the build team does not relax those rules.
