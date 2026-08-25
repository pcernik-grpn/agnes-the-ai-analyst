# Slack App manifest — Socket Mode transport (optional)

Use this when your Agnes instance has no publicly reachable webhook URL.
Slack delivers events over an outbound WebSocket instead of an HTTPS
webhook, so there is **no `request_url`** — that's the whole point of the
two-stanza split (a stale `request_url` is a common foot-gun).

The scope set is identical to the HTTP manifest (see
`docs/slack-manifest-http.md` for the per-scope rationale — bot-token only,
no user scopes, no hand-picking). The canonical copy lives at
`services/slack_bot/manifest.yaml`; `tests/test_slack_manifest_sync.py`
keeps this file in sync with it.

```yaml
display_information:
  name: Agnes
  description: Ask Agnes data questions from Slack
  background_color: "#1a1a1a"
features:
  bot_user:
    display_name: Agnes
    always_online: false
  app_home:
    home_tab_enabled: false
    messages_tab_enabled: true
    messages_tab_read_only_enabled: false
oauth_config:
  scopes:
    bot:
      - app_mentions:read # event: app_mention
      - chat:write # chat.postMessage / chat.postEphemeral / chat.update
      - im:history # event: message.im (DMs with the bot)
      - im:write # conversations.open (opening the DM channel)
      - reactions:write # reactions.add (ack emoji; degrades to a log warning)
slash_commands:
  - command: /agnes
    description: Ask Agnes a data question
    usage_hint: "<your question> | help"
    should_escape: false
  - command: /agnes-new
    description: Archive your Agnes session and start fresh
    should_escape: false
  - command: /agnes-status
    description: Show your active Agnes session count and cap
    should_escape: false
settings:
  event_subscriptions:
    bot_events:
      - app_mention
      - message.im
  interactivity:
    is_enabled: true
  org_deploy_enabled: false
  socket_mode_enabled: true
  token_rotation_enabled: false
```

After creating the app, generate an **app-level token** (`xapp-…`) with the
`connections:write` scope under "Basic Information → App-Level Tokens".

## Required environment

- `SLACK_BOT_TOKEN` (`xoxb-…`)
- `SLACK_APP_TOKEN` (`xapp-…`, with `connections:write`)
- `SLACK_SIGNING_SECRET`
- `chat.slack.transport: socket` in `instance.yaml` (or `SLACK_TRANSPORT=socket`)
- Install the optional dependency: `pip install '.[slack-socket]'`
- These tokens may instead be set from the admin UI (`/admin/server-config` → Slack bot secrets), stored encrypted in the vault (`AGNES_VAULT_KEY` required). Environment variables, if present, take precedence.

## Constraints

- **Single worker only.** Socket Mode requires `UVICORN_WORKERS=1` — multiple
  workers each open a WS and fracture event dedup. Agnes refuses to start the
  WS otherwise (logs the reason, disables Slack, never crashes).
- All gates are fail-closed: a missing/mis-prefixed token pair or a missing
  `slack-socket` extra logs the reason and leaves Slack HTTP-only.
