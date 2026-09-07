### Internal
- Docs and the agent kit now describe the merge queue as the way a PR lands on `main`, replacing the hand-driven merge trains: queue with `gh pr merge <N> --merge --auto --delete-branch`, the queue tests the merged result on the `merge_group` event, and `main` no longer requires an up-to-date branch (#2295).
