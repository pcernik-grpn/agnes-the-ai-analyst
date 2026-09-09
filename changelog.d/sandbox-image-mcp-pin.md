### Fixed
- **Docker-provider chat sandbox: the agent gets its Agnes tools back.** The
  sandbox image installed `claude-agent-sdk` without pinning `mcp`, and the
  SDK's floor-only requirement resolved `mcp` 2.x, which removed the module
  the `agnes mcp` stdio server is built on. Claude Code dropped the failed
  server silently, so the sandbox agent ran with no Agnes foundation tools
  and no Universal-MCP passthrough tools — only delegation. The image now
  pins `mcp` (and `sqlglot`) to pyproject's range, its contract label is `4`
  (rebuild it: `docs/cloud-chat.md` → "Keep the sandbox image fresh"), a
  guard keeps every pyproject-capped pin in the image identical to
  pyproject's, and the runner logs `mcp server 'agnes' did not connect` to
  the sandbox log whenever an MCP server fails to start instead of nothing.
