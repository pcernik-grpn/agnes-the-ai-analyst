### Changed
- **BREAKING (for a misconfigured deployment only): an instance configured for
  `chat.docker_egress_mode: allowlist` no longer starts chat without a running
  egress proxy.** In allowlist mode the sandbox network is `internal` — no route
  off the host — and the `services/egress_proxy` sidecar is the only way out, so
  a stack brought up without the `chat-docker-egress` compose profile ran every
  session with *zero* egress and nothing said why (a proxy container once sat
  `Exited` for five days unnoticed, #1250). Two things change. The compose
  profile is now **derived from the configured mode** rather than being a step
  an operator has to remember: `scripts/ops/agnes-compose-file.sh` gained
  `agnes_chat_egress_allowlist_active`, and both VM boot and the 5-minute
  auto-upgrade tick read `chat.docker_egress_mode` out of the same
  `instance.yaml` the app does, activate `--profile chat-docker-egress`, and
  bring the proxy back up on any tick it is found down. And the app now refuses
  to spawn the ChatManager (every `/chat` route 503s, with the exact
  `docker compose --profile chat-docker-egress up -d egress-proxy` command in
  the log) when the mode says allowlist and the proxy does not answer on
  `chat.docker_egress_proxy_url` — previously it booted and only the sessions
  broke. `chat.docker_egress_mode: open` and the `none` default are untouched:
  no profile, no probe, no behavior change. See `docs/cloud-chat.md` → *Egress
  policy*.
