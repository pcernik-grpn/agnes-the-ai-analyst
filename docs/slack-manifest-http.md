# Slack App manifest — HTTP transport (default)

Paste this at api.slack.com/apps → "Create New App" → "From a manifest".
Replace `<your-host>` with the public hostname of your Agnes instance
(e.g. `agnes.example.com`). This is the default transport — Slack delivers
events over an HTTPS webhook to your public endpoint.

Do **not** hand-pick scopes instead of using the manifest. The set below is
the minimum the bot code actually calls, it is bot-token only (no user
scopes), and each scope carries its justification — which is exactly what a
workspace-admin approval or a customer security review wants to see. The
canonical copy lives at `services/slack_bot/manifest.yaml`;
`tests/test_slack_manifest_sync.py` keeps this file in sync with it.

Slack↔Agnes identity needs no `users:*` scopes: the bot never reads Slack
profiles. A first DM issues a 6-digit code the user redeems at `/setup`
while logged in, which persists the binding server-side.

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
    url: "https://<your-host>/api/slack/commands"
    description: Ask Agnes a data question
    usage_hint: "<your question> | help"
    should_escape: false
  - command: /agnes-new
    url: "https://<your-host>/api/slack/commands"
    description: Archive your Agnes session and start fresh
    should_escape: false
  - command: /agnes-status
    url: "https://<your-host>/api/slack/commands"
    description: Show your active Agnes session count and cap
    should_escape: false
settings:
  event_subscriptions:
    request_url: "https://<your-host>/api/slack/events"
    bot_events:
      - app_mention
      - message.im
  interactivity:
    is_enabled: true
    request_url: "https://<your-host>/api/slack/interactivity"
  org_deploy_enabled: false
  socket_mode_enabled: false
  token_rotation_enabled: false
```

Slack adds the `commands` scope automatically because the manifest declares
slash commands — you don't list it yourself.

## Required environment

- `SLACK_BOT_TOKEN` (`xoxb-…`)
- `SLACK_SIGNING_SECRET`
- `chat.slack.transport: http` in `instance.yaml` (or `SLACK_TRANSPORT=http`,
  or leave unset — `http` is the default).
- These tokens may instead be set from the admin UI (`/admin/server-config` → Slack bot secrets), stored encrypted in the vault (`AGNES_VAULT_KEY` required). Environment variables, if present, take precedence.
