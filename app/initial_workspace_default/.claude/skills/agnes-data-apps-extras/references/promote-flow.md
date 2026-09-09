# Promote flow — draft to prod

Run this exact sequence once the user picks "Publish" at the preview
`AskUserQuestion` (SKILL.md section 3). Never run any step of it before that
explicit choice.

1. **Close the preview pane first** — `agnes_data_app_close(<draft_slug>)`.
   Do this before anything else so the pane never ends up pointing at an
   app that's mid-teardown or already deleted.
2. **Merge the draft branch into `main`**, in the app's managed repo:

   ```bash
   git checkout main
   git merge <draft_branch>
   git push
   ```

3. **Redeploy prod** — `data_app_deploy(<prod_slug>)` (no `mode` argument;
   this fast-forwards the live ref from `main` and redeploys the prod app).
4. **Delete the draft** — `data_app_delete_draft(<prod_slug>, <draft_slug>)`.
   This removes the draft's registry row, its container, and its branch.
5. **Ask who should see it** — SKILL.md §3b: `data_app_share_get(<prod_slug>)`,
   then `data_app_share(...)` once the user answers. Never skip this step
   silently and never decide it yourself.
6. **Data identity, only if asked** — if the user wants each viewer to see
   only their own data rather than the owner's, `data_app_set_data_identity(
   <prod_slug>, "viewer")`. This redeploys the app and requires a
   Postgres-backed Agnes instance (`501 requires_postgres_backend`
   otherwise) — tell the user plainly if it's unavailable rather than
   retrying.

Do not reorder steps 1 and 4: closing the preview before touching anything
else, and deleting the draft only after prod is confirmed redeployed from
`main`, avoids a window where the user-visible pane or the draft registry
row point at nothing. Steps 5 and 6 come after, once prod is stable.

After promote, update the managed repo's root `CLAUDE.md` `# App context
(maintained by Agnes)` section (SKILL.md section 6) with anything a future
conversation should know before editing this app again — new data sources
wired in, key decisions made this session, anything fragile to be careful
with on the next iteration.
