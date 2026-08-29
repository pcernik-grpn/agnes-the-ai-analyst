---
name: connect-this-tool
description: Connect an outside AI tool — Claude Code, Cursor, VS Code/Copilot, or any MCP client — to this Agnes instance, over chat instead of a page of instructions. Use whenever a chat user asks to connect, hook up, or use Agnes from their editor, terminal, IDE, or another AI client, or says an existing connection stopped working.
---

# Connect this tool

The user wants their own AI tool talking to this Agnes instance. Do it as a
conversation: find out which tool, hand them the two things that tool needs,
confirm it landed.

**This should never take six steps.** If you find yourself building a long
ladder for someone, the connect path itself is wrong — say so plainly and
tell them to raise it, rather than walking them down it.

## The one rule that is not negotiable

**Never let a token into this conversation.** Do not ask for one, do not
echo one, do not accept one. If the user pastes something that looks like a
token, tell them immediately:

> That token is now in this conversation's transcript. Go to **/mcp-connect**
> and click Generate again — that revokes the one you just pasted and issues
> a fresh one. Put the new one straight into your editor config, not here.

Then carry on. The token travels from the browser page to their config file
and never through you. This is also why every snippet below puts it in an
`Authorization` header and never in a URL — a token in a query string lands
in proxy logs and browser history.

## What every MCP client needs

Exactly two things, which is why the matrix of per-tool pages was the wrong
shape:

1. **The endpoint** — this instance's origin plus `/api/mcp/sse`, over SSE.
2. **An `Authorization: Bearer <token>` header**, using a token they generate
   at **/mcp-connect** (shown once; generating again revokes the previous one).

If you know those two things you can connect anything. The named tools below
are just where each one keeps its config.

## Step 1 — ask which tool

Ask, and keep it open: "Which tool are you connecting — Claude Code, Cursor,
VS Code, something else?" Do not assume Claude. If they name something not
listed here, that is fine: go to *Any other MCP client* below.

## Step 2 — send them for a token

Point them at **/mcp-connect** in the browser and tell them to click
**Generate connector token**, then come back and say "got it". Do not ask
them to show it to you.

If /mcp-connect is not reachable for them, stop — this instance has the
connector UI switched off and there is nothing you can do from here. Tell
them to ask an admin about `mcp.connector_ui_enabled`.

## Step 3 — give them the config for their tool

Substitute this instance's own origin for `<BASE_URL>` — if you are unsure
what it is, ask them to read it out of their browser's address bar rather
than guessing. `<TOKEN>` is theirs to paste; leave it literal.

**Claude Code** — one command, then restart. The server only appears after a
restart; `/mcp` verifies it.

```
claude mcp add --transport sse agnes <BASE_URL>/api/mcp/sse \
  --header "Authorization: Bearer <TOKEN>"
```

**Cursor** — `~/.cursor/mcp.json`

```json
{
  "mcpServers": {
    "agnes": {
      "url": "<BASE_URL>/api/mcp/sse",
      "headers": { "Authorization": "Bearer <TOKEN>" }
    }
  }
}
```

**VS Code / Copilot** — `.vscode/mcp.json` in the workspace root

```json
{
  "servers": {
    "agnes": {
      "type": "sse",
      "url": "<BASE_URL>/api/mcp/sse",
      "headers": { "Authorization": "Bearer <TOKEN>" }
    }
  }
}
```

**Any other MCP client** — give them the endpoint, the transport (SSE) and
the header, and say where such clients usually keep it (a JSON config file,
or an "add MCP server" dialog). Say plainly that you do not know that tool's
exact file path rather than inventing one, and offer the shape above as the
thing to translate.

## Step 4 — confirm it actually landed

This is the step that makes it a conversation rather than a leaflet. A token
records when it was last used, so you can check instead of asking them to
trust it:

```
agnes tokens list
```

Look at `last_used_at` on their newest token. If it has a recent timestamp,
their client has authenticated — tell them, and stop. If it is still `-`,
the client has not called yet: have them restart it (Claude Code in
particular only picks the server up on restart) and check once more.

Never report success off the config alone. A config that was written and a
client that connected are different facts.

## When it does not work

Ask for the exact error and take it from there. The failures worth knowing:

- **Nothing appears in the client.** Almost always a restart — Claude Code
  and most editors read MCP config at launch.
- **401 / unauthorized.** The token is wrong, or it was revoked by a later
  Generate click. Have them generate once more and replace it, remembering
  the old one dies the moment a new one is made.
- **Connection refused or a TLS error.** Wrong `<BASE_URL>`, or the instance
  is not reachable from their network. Have them open that URL in a browser
  first — if the page does not load, the editor was never the problem.
- **Connected, but no tools.** They authenticated but have no grants. This is
  an access question, not a connection one: point them at an admin.

Never work around a TLS error by disabling verification. If someone suggests
`-k`, `NODE_TLS_REJECT_UNAUTHORIZED=0`, or similar, refuse — that hides the
real fault and ships a broken trust chain.

## After it works

Tell them what they can now do from that tool, briefly — query registered
tables, search the library, read the fact graph — and that `/mcp` (or their
client's equivalent) lists exactly which tools their grants give them. If
that list is emptier than they expected, that is grants, not connection.
