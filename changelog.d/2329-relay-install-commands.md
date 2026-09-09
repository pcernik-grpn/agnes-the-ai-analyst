### Changed
- **Step 1 of the install prompt now tells the agent to relay the exact
  install commands if it isn't running them itself**, instead of only
  gesturing at "step 1" — an agent that declines to `curl | uv tool install`
  on its own (a legitimate, expected refusal) was handing the person a
  vague pointer instead of the copy-pasteable commands.
