    # -------------------------------------------------------------------------
    # L3 — role: gateway
    # -------------------------------------------------------------------------
    # One turn, end to end: claim the session, spawn the sandbox, pump the
    # turn, broker every call out. Two chokepoints stand in front of the model
    # and both fail closed.
    # -------------------------------------------------------------------------

    gwChatManager -> gwRouting "Claims chat:{id} on spawn; renews on the idle-reaper heartbeat"
    gwChatManager -> gwReplay "Appends every outbound frame with a monotonic seq"
    gwChatManager -> gwProvider "Spawns and destroys the session's sandbox"
    gwChatManager -> gwPrincipal "Resolves the effective authority for this turn"
    gwChatManager -> gwArtifacts "Harvests files the turn produced"
    gwChatManager -> appState "Persists sessions, messages and per-turn usage" "SQL"
    gwRouting -> coordination "Lease keyed on the session" "Redis"
    gwReplay -> coordination "Bounded replay and inbound streams" "Redis"
    gwNotifications -> coordination "Subscribes to notify:{user}" "Redis"

    gwProvider -> appsRunner "Requests the container; never touches the Docker socket itself" "HTTP"
    gwProvider -> sandbox "Stages the caller's stack into the workspace before spawn" "File"

    gwPrincipal -> appState "Owner grants intersected with the agent's declared scope" "SQL"
    gwBroker -> gwPrincipal "Binds the restricted principal to every brokered request"
    gwBroker -> appState "Reads the pinned model and the monthly token budget" "SQL"
    gwBroker -> anthropic "Forwards the prompt; the key never enters the sandbox" "HTTPS"
    gwBroker -> vertex "Forwards the prompt, keyless via workload identity" "HTTPS"
    gwBroker -> openaiCompat "Forwards the prompt" "HTTPS"

    gwDelegation -> gwChatManager "Spawns the delegate as a child session under the ORIGINAL CALLER"
    gwArtifacts -> appState "Stores artifacts scoped to the session that made them" "SQL"

    sandbox -> gwBroker "Every model call, ticket-gated" "HTTPS"
    sandbox -> egressProxy "Everything else, refused unless allowlisted" "CONNECT"
