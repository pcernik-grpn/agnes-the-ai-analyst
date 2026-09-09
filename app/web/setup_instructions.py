"""Single source of truth for the "Setup a new Claude Code" clipboard payload.

Both the JS-embedded clipboard renderer (`_claude_setup_instructions.jinja`)
and the read-only HTML preview on the dashboard and /install pages consume
these lines. Keep it in Python so there is exactly ONE place that edits.

The payload is deliberately THIN (see
`docs/superpowers/specs/2026-08-19-thin-install-prompt-design.md`): install
the CLI, run `agnes onboard`, restart Claude Code, confirm. Orchestration
that used to be an English program executed by a non-deterministic
interpreter — workspace triage, catalog smoke, git/claude preflight,
marketplace bootstrap, diagnose, connector tiles — now lives inside
`agnes onboard`, which is a deterministic state machine that reports its
own outcome. The prompt's remaining job is to get the CLI onto the machine
and to relay whatever the CLI asks for.

Placeholders `{server_url}` and `{server_host}` are substituted at render
time — `{server_host}` server-side via `resolve_lines()`, `{server_url}`
surviving into the JS template to be filled in at click time.
`{instance_brand}` and `{workspace_dir}` are likewise server-side.

`{wheel_filename}`, `plugin_install_names` and `connector_manifest` are
still accepted by `resolve_lines()` / `render_setup_instructions()` for
backward compatibility with existing callers, but are IGNORED:

* `{wheel_filename}` used to be pre-substituted into a
  `/cli/wheel/{wheel_filename}` URL, because `uv tool install` validates the
  PEP 427 filename *in the URL path* before fetching, so a stable alias like
  `agnes.whl` fails with "Must have a version". That pinned the filename
  captured at RENDER time, though, so a server upgrade between render and
  execution 404d it. Step 1 downloads via the unversioned `/cli/download`
  endpoint instead (immune to that race — see `_install_cli_lines`).
* `plugin_install_names` fed the marketplace block, which `agnes onboard`
  now owns (it runs `agnes refresh-marketplace --bootstrap` off the LIVE
  manifest, so a render-time snapshot of grants was always the weaker
  input).
* `connector_manifest` fed the connector tiles. Connectors are
  conversational and post-install now: `agnes onboard`'s summary lists what
  is available, and the user asks for one when they want it. This module no
  longer loads the manifest at all, so a broken seed cannot break the
  install prompt.

The analyst's access token is deliberately NOT a placeholder in this
template. It is written to `~/.agnes/token` out-of-band, before this
prompt is generated (see `{server_url}/how-it-works#connect`, the CLI tab's
"Set up a new Claude Code" button) — so the raw token
value never has to appear in the prompt text or a pasted chat transcript.
(Scope of that guarantee: THIS payload. Step 4's own copied shell command
does carry the token through the browser clipboard transiently — that is
its delivery mechanism — and its clipboard-blocked fallback can reveal it
on an explicit second click.) `render_setup_instructions()` still accepts a
`token` kwarg for backward compatibility with existing callers, but it is
a no-op today: nothing in the rendered body contains `{token}` to
substitute.

## Cross-platform trust strategy (when `ca_pem` is supplied)

The trust block (step 0) is the one piece of orchestration that CANNOT move
into the CLI — it runs before the CLI can be downloaded over the very
connection it is bootstrapping trust for. It stays here, unchanged, and
renders automatically for instances serving a self-signed or private-CA
cert; publicly-trusted (e.g. Let's Encrypt) instances render nothing extra.
Three things bit us in practice and the design here exists to dodge each
one:

1. **rustls rejects the Agnes leaf cert as `CaUsedAsEndEntity`.** The Agnes
   server's self-signed cert is simultaneously its own CA (basicConstraints
   `CA:TRUE`) AND the leaf served on the wire — a setup OpenSSL tolerates
   but webpki/rustls strictly refuses. So `uv tool install <https-url>`
   never works against the Agnes wheel endpoint. We download the wheel via
   curl first (curl uses OpenSSL, accepts the cert), then `uv tool install
   --native-tls --force <local-file>` lets rustls reuse the OS trust store
   for PyPI dependency resolution. No HTTPS hop through rustls touches the
   Agnes host.

2. **`SSL_CERT_FILE` REPLACES the trust store, it doesn't append.** Pointing
   it at `~/.agnes/ca.pem` alone breaks every Python tool that needs to
   reach a public host (PyPI, GitHub) — the agnes CLI works fine because it
   only talks to Agnes, but `uv run --with <pkg>` immediately fails with
   `UnknownIssuer`. We materialize a combined bundle at
   `~/.agnes/ca-bundle.pem` (system roots + Agnes CA) and point all
   `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` / `GIT_SSL_CAINFO` at it.
   `NODE_EXTRA_CA_CERTS` keeps pointing at just `ca.pem` because Node's
   semantics is *additive* (appends to bundled roots), so a single-cert
   file is correct there.

3. **Bun-compiled `claude` (Windows + macOS distributions) ignores every
   CA env var AND the OS trust store for marketplace HTTPS.** The binary
   recognizes `NODE_EXTRA_CA_CERTS`, `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`,
   `CURL_CA_BUNDLE`, but in practice the values never reach the TLS context
   — a known limitation of Bun's compiled-binary HTTPS path. That is why
   the marketplace is always reached through a system `git clone` (which
   `agnes onboard` performs via `agnes refresh-marketplace --bootstrap`):
   system git honors `GIT_SSL_CAINFO` from the combined bundle built in
   step 0(d). The OS trust-store registration in step 0(c) is still done on
   all three platforms because native tools — the system git fetch path
   itself (Schannel on Windows, Security framework on macOS) — trust via
   the OS store, not via env vars.

The combined-bundle source uses a fallback chain so the prompt still works
on machines without the system Python `certifi`: we try (a) `python3 -c
'import certifi'`, (b) the platform's curl/openssl bundle path, (c)
`uv run --with certifi` as a network last-resort. The user explicitly
permitted that fallback chain — it's not improvising-around-a-TLS-error.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _tls_trust_block(ca_pem: str) -> list[str]:
    """Step 0 — cross-platform TLS trust bootstrap for the Agnes server.

    Emitted only when the server has a non-publicly-trusted cert. Does four
    things in a single numbered block (see module docstring for the full
    rationale):

      (a) Detect platform (Windows Git Bash / macOS / Linux) and pick the
          shell rc file that the user's login shell actually reads.
          `$SHELL`-driven, NOT existence-of-rc-driven — old setups put a
          legacy `.bashrc` next to a default zsh shell on macOS, and the
          `[ -f .bashrc ]` heuristic silently writes to the wrong file.
      (b) Write the cert PEM to `~/.agnes/ca.pem` via single-quoted heredoc
          (so `$` / backtick chars in real-world certs never shell-expand).
      (c) Register the cert in the OS trust store (so native binaries that
          bypass our env vars — claude.exe, system git's Schannel backend,
          Python apps using `truststore` — still trust the host).
          Idempotent: re-running just re-affirms the entry.
      (d) Build a *combined* CA bundle (system roots + Agnes CA) at
          `~/.agnes/ca-bundle.pem`, with a fallback chain for the system
          roots source. Persist `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` /
          `GIT_SSL_CAINFO` pointing at the bundle, plus
          `NODE_EXTRA_CA_CERTS` pointing at just `ca.pem` (Node
          appends-not-replaces). Persistence is idempotent via a grep
          guard for the `AGNES_CA_PEM_TRUST` marker.
    """
    pem = ca_pem.strip()
    lines: list[str] = [
        "0) Trust the Agnes TLS certificate — cross-platform setup for a self-signed / private-CA host.",
        "",
        "   (a) Detect platform + pick the shell rc file your login shell actually reads.",
        "       Driven by $SHELL + uname (NOT by which rc files happen to exist on disk).",
        "",
        '       case "$(uname -s)" in',
        "         Darwin)               PLATFORM=macos ;;",
        "         Linux)                PLATFORM=linux ;;",
        "         MINGW*|MSYS*|CYGWIN*) PLATFORM=windows ;;",
        '         *) echo "Unsupported OS: $(uname -s)" >&2; exit 1 ;;',
        "       esac",
        '       SHELL_NAME="$(basename "${SHELL:-bash}")"',
        '       case "${SHELL_NAME}:${PLATFORM}" in',
        '         zsh:*)                   RC="$HOME/.zshrc" ;;',
        '         bash:macos)              RC="$HOME/.bash_profile" ;;',
        '         bash:windows|bash:linux) RC="$HOME/.bashrc" ;;',
        '         *)                       RC="$HOME/.profile" ;;',
        "       esac",
        '       echo "Platform: $PLATFORM, shell: $SHELL_NAME, rc: $RC"',
        "",
        "   (b) Write the cert (single-quoted heredoc so $/backticks in the body don't expand):",
        "",
        "       mkdir -p ~/.agnes",
        "       cat > ~/.agnes/ca.pem <<'AGNES_CA_PEM'",
    ]
    # PEM body is flush-left: `<<'DELIM'` heredocs preserve leading whitespace,
    # and any indent inside the cert breaks `openssl x509` / Python ssl parsers.
    lines.extend(pem.splitlines())
    lines.extend(
        [
            "AGNES_CA_PEM",
            "",
            "   (c) Register the cert in the OS trust store. Native binaries (claude.exe,",
            "       system git's Schannel/Security.framework backends) read the OS store",
            "       and ignore our env vars — without this, the later marketplace `git",
            "       clone` (when plugins are configured) and any user-side git/native",
            "       tooling against the Agnes host will fail.",
            "       No admin rights needed (user-store only). Idempotent.",
            "",
            '       case "$PLATFORM" in',
            "         windows)",
            '           WIN_CA="$(cygpath -w ~/.agnes/ca.pem)"',
            '           certutil.exe -user -addstore "Root" "$WIN_CA"',
            "           ;;",
            "         macos)",
            "           # Will prompt once for the keychain password.",
            "           security add-trusted-cert -r trustRoot \\",
            '             -k "$HOME/Library/Keychains/login.keychain-db" \\',
            "             ~/.agnes/ca.pem",
            "           ;;",
            "         linux)",
            "           if command -v update-ca-certificates >/dev/null 2>&1; then",
            "             sudo cp ~/.agnes/ca.pem /usr/local/share/ca-certificates/agnes.crt",
            "             sudo update-ca-certificates",
            "           elif command -v update-ca-trust >/dev/null 2>&1; then",
            "             sudo cp ~/.agnes/ca.pem /etc/pki/ca-trust/source/anchors/agnes.crt",
            "             sudo update-ca-trust",
            "           else",
            '             echo "WARN: install ~/.agnes/ca.pem into your distro\'s trust store manually" >&2',
            "           fi",
            "           ;;",
            "       esac",
            "",
            "   (d) Build a COMBINED CA bundle (system roots + Agnes CA) for Python tools",
            "       and curl. SSL_CERT_FILE *replaces* the trust store, so pointing it at",
            "       the Agnes CA alone would break public hosts (PyPI etc.). Source the",
            "       system roots from a fallback chain — the first source that produces",
            "       a non-empty, existing path wins. Don't abort on the first miss; that's",
            "       what the chain is for.",
            "",
            "       CERTIFI_PATH=\"$(python3 -c 'import certifi; print(certifi.where())' 2>/dev/null || true)\"",
            '       [ -z "$CERTIFI_PATH" ] && CERTIFI_PATH="$(python -c \'import certifi; print(certifi.where())\' 2>/dev/null || true)"',
            '       if [ -z "$CERTIFI_PATH" ]; then',
            "         for p in /mingw64/ssl/certs/ca-bundle.crt /usr/ssl/certs/ca-bundle.crt \\",
            "                  /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt \\",
            "                  /etc/ssl/cert.pem; do",
            '           [ -f "$p" ] && CERTIFI_PATH="$p" && break',
            "         done",
            "       fi",
            '       if [ -z "$CERTIFI_PATH" ]; then',
            "         CERTIFI_PATH=\"$(uv run --native-tls --with certifi --no-project python -c 'import certifi; print(certifi.where())' 2>/dev/null || true)\"",
            "       fi",
            '       if [ -z "$CERTIFI_PATH" ] || [ ! -f "$CERTIFI_PATH" ]; then',
            '         echo "ERROR: locate a system CA bundle. Install Python 3 + certifi and re-run." >&2',
            "         exit 1",
            "       fi",
            '       echo "Base CA bundle: $CERTIFI_PATH"',
            '       cat "$CERTIFI_PATH" ~/.agnes/ca.pem > ~/.agnes/ca-bundle.pem',
            "",
            "   (e) Persist env vars in the rc file picked in (a). Idempotent — won't",
            "       duplicate on re-run thanks to the AGNES_CA_PEM_TRUST grep guard.",
            "       Note the asymmetry: SSL_CERT_FILE (and REQUESTS_CA_BUNDLE, GIT_SSL_CAINFO)",
            "       point at the COMBINED bundle because those tools REPLACE trust.",
            "       NODE_EXTRA_CA_CERTS points at just ca.pem because Node APPENDS to its",
            "       bundled roots.",
            "",
            "       if ! grep -q 'AGNES_CA_PEM_TRUST' \"$RC\" 2>/dev/null; then",
            "         cat >> \"$RC\" <<'AGNES_RC_BLOCK'",
            "# AGNES_CA_PEM_TRUST — added by Agnes setup",
            "# Combined bundle (system roots + Agnes CA) for tools that REPLACE trust:",
            'export SSL_CERT_FILE="$HOME/.agnes/ca-bundle.pem"',
            'export REQUESTS_CA_BUNDLE="$HOME/.agnes/ca-bundle.pem"',
            'export GIT_SSL_CAINFO="$HOME/.agnes/ca-bundle.pem"',
            "# Single-cert file for Node (APPENDS to bundled roots):",
            'export NODE_EXTRA_CA_CERTS="$HOME/.agnes/ca.pem"',
            'export PATH="$HOME/.local/bin:$PATH"',
            "AGNES_RC_BLOCK",
            "       fi",
            "       # Apply for THIS shell too:",
            '       export SSL_CERT_FILE="$HOME/.agnes/ca-bundle.pem"',
            '       export REQUESTS_CA_BUNDLE="$HOME/.agnes/ca-bundle.pem"',
            '       export GIT_SSL_CAINFO="$HOME/.agnes/ca-bundle.pem"',
            '       export NODE_EXTRA_CA_CERTS="$HOME/.agnes/ca.pem"',
            '       export PATH="$HOME/.local/bin:$PATH"',
            "",
            "   Note for the Bash tool: environment variables set in one call don't",
            "   carry over to the next. Re-export the four lines above (SSL_CERT_FILE,",
            "   REQUESTS_CA_BUNDLE, GIT_SSL_CAINFO, NODE_EXTRA_CA_CERTS) plus PATH at",
            "   the top of every later step's bash block that talks to Agnes.",
            "",
        ]
    )
    return lines


def _preamble_lines(*, custom_preamble: str = "", instance_brand: str = "Agnes") -> list[str]:
    """Header that opens the prompt: what this is, which server, how sign-in
    is handled, and the idempotence promise.

    No longer asserts a token is already sitting at `~/.agnes/token` before
    step 2 runs. `agnes onboard` (`cli/commands/onboard.py::_resolve_onboard_
    credential`) already tries, in order, a bootstrap token file, a saved
    credential, and — with nothing found — falls back to the same
    interactive browser sign-in `agnes auth login` uses. That fallback is
    what the setup-code `/llms.txt` flow relies on entirely (see its own
    comment: "the old un-numbered token precheck does not apply here"), and
    it is the SAME code path this prompt's step 2 calls into — so a separate
    prompt-level check for "is the file there yet" was answering a question
    `agnes onboard` already answers better, one step later, with a real
    browser sign-in instead of a dead end. What a prompt-level check still
    cannot do is protect against a *stale, cross-deployment* saved
    credential (`~/.config/agnes/token.json` never records which server it
    belongs to) — but that surfaces as a clear auth failure inside `agnes
    init`'s own verification, covered by the existing "paste the exact error
    back and stop" guidance below, not by bash run ahead of time.

    That is stated as a plain fact: earlier wording told the agent to
    "never print the token, echo it, or paste it into this chat", which
    reads as instructions to conceal a credential rather than as a note
    that displaying a file nobody needs to see is pointless.

    Nothing here reassures the agent about trust. A previous revision told
    it that "whether that host is trusted is the user's org's call" and that
    the step-0(d) fallback chain was "documented and OK to use". Text that
    pre-empts a safety judgement reads as written to defuse one, which is
    the opposite of the intended effect — so the prompt states what each
    step does, names the server, and leaves every judgement to the reader.

    The brand line exists for the same reason: the prompt is branded with
    the operator's product name, installs a binary called `agnes`, and
    downloads from the instance's own host, so an agent seeing three
    different names has no way to know they are one system unless the
    prompt says so. On an unbranded instance there is no third name to
    reconcile — `instance_brand` is still the default `"Agnes"` — so the
    sentence drops the "own deployment of Agnes" clause rather than
    rendering the tautology "Agnes is this organization's own deployment
    of Agnes".

    The "don't lower certificate verification" advice stays unconditional —
    it's good guidance regardless of whether the server runs with a private
    CA, and it is the only error-handling instruction the thin prompt keeps
    (everything else reports its own outcome through `agnes onboard`).

    `custom_preamble` is an operator-authored block prepended at the very
    top (above `Set up the {instance_brand} CLI…`). Empty/unset emits zero
    extra lines so the default output is byte-identical. Any
    `{instance_brand}` etc. inside it is substituted by the `resolve_lines`
    loop; it must NOT contain literal `{server_url}` (that only resolves
    at click time in the JS clipboard flow, not in the preamble).
    """
    brand_lines = (
        [
            "{instance_brand} is served from {server_url}, and the command-line",
            "tool it installs is named `agnes`.",
        ]
        if instance_brand == "Agnes"
        else [
            "{instance_brand} is this organization's own deployment of Agnes, served",
            "from {server_url}, and the command-line tool it installs is named `agnes`.",
        ]
    )
    lines = [
        "Set up the {instance_brand} CLI on this machine.",
        "",
        "Server: {server_url}",
        "",
        *brand_lines,
        "",
        "Requirements: `uv` (https://docs.astral.sh/uv/) and `curl`, plus network",
        "access to the server above.",
        "",
        "If this machine already has a saved sign-in for {server_url} — a token at",
        "~/.agnes/token from the install guide, or a previous `agnes auth login` —",
        "step 2 reuses it; nothing needs to display its contents first. Otherwise",
        "`agnes onboard` opens your browser to sign in when it gets there.",
        "",
        "Every step below is idempotent and safe to re-run: a machine that already",
        'ran this setup converges instead of reinstalling, so treat "already',
        'configured" outcomes as success. If a step fails with an unfamiliar error,',
        "paste the exact error back and stop — and for a TLS error look for the cause",
        "(corporate proxy, internal CA, clock skew) rather than lowering certificate",
        "verification.",
        "",
    ]
    if custom_preamble:
        lines = [*custom_preamble.split("\n"), "", *lines]
    return lines


def _install_cli_commands(*, has_ca: bool, server_url_placeholder: str = "{server_url}") -> list[str]:
    """The shell lines that put the `agnes` wheel on a machine — nothing else.

    Shared by `_install_cli_lines` (the user-voiced /setup prompt) and the
    documentation-voiced `/llms.txt` body, so there is still exactly one
    place that knows how the CLI is downloaded and installed. Three-space
    indent matches the prompt's step layout; `/llms.txt` re-indents.
    """
    cacert = " --cacert ~/.agnes/ca.pem" if has_ca else ""
    native_tls = " --native-tls" if has_ca else ""
    return [
        "   TMPDIR_WHEEL=$(mktemp -d -t agnes_cli.XXXXXX)",
        f'   (cd "$TMPDIR_WHEEL" && curl -fsSL --max-redirs 0{cacert} -OJ {server_url_placeholder}/cli/download)',
        '   WHEEL=$(ls "$TMPDIR_WHEEL"/*.whl 2>/dev/null | head -n1)',
        '   [ -n "$WHEEL" ] || { echo "error: wheel download failed (no .whl in $TMPDIR_WHEEL)" >&2; exit 1; }',
        f'   uv tool install{native_tls} --force "$WHEEL"',
    ]


def _install_cli_lines(*, has_ca: bool, server_url_placeholder: str = "{server_url}") -> list[str]:
    """Step 1 — install the `agnes` CLI.

    Downloads via the unversioned `/cli/download` endpoint (`curl -OJ`,
    which honours `Content-Disposition` and saves the wheel under its real
    PEP-427 filename) into a fresh temp dir, then installs from that local
    file — the same pattern `app/api/cli_artifacts.py::cli_install_script`
    (`/cli/install.sh`) already uses. This is deliberately NOT a
    `/cli/wheel/{wheel_filename}` URL pinned to the filename captured when
    this prompt was *rendered*: `app/api/cli_artifacts.py::cli_wheel_versioned`
    serves only the wheel currently on disk, so if the server auto-upgrades
    between render and execution (a background version roll, or simply time
    passing before the user pastes the prompt), a pinned URL 404s.
    `/cli/download` always serves whichever wheel is current at fetch time,
    so the install survives a mid-session version roll.

    `-L --max-redirs 0` is deliberate. `-OJ` takes the saved filename
    from the response's `Content-Disposition`, so `-L` alone would follow a
    cross-host redirect and install whichever wheel the hop served, with
    nothing on screen to say the download moved. Keeping `-L` but capping
    redirects at zero turns any hop into `curl: (47) Maximum (0) redirects
    followed` and a non-zero exit — a failure the analyst can report. A
    hostname alias that 308s to the canonical host must therefore be handed
    out as the canonical URL, not as the alias. The scheme is deliberately
    NOT pinned with `--proto '=https'`: a local/dev instance legitimately
    serves this prompt over http, where that flag would refuse the download
    outright (`curl: (1) Protocol "http" not supported`).

    curl has no "follow redirects, but only within this host" mode, so the
    cap is all-or-nothing: it refuses a *benign* same-host hop too (an
    http→https upgrade at a TLS-terminating proxy, a path normalization).
    That is a deployment constraint, stated here so it is not a surprise:
    **whatever answers `{server_url}/cli/download` must serve the wheel
    directly, not redirect to it.** Two reasons it holds in practice:
    `/cli/download` is an exact route returning a `FileResponse`
    (`app/api/cli_artifacts.py`), and `{server_url}` is substituted from the
    origin the user is *already* browsing when they copy this prompt — so a
    scheme-upgrade redirect has happened before the value is captured, never
    after. A deployment that does insert a hop for this path fails loudly on
    the first install rather than silently swapping the wheel, which is the
    trade this flag is making.

    When the trust block was emitted (`has_ca=True`), we MUST additionally
    avoid `uv tool install <https-url>` against the Agnes server:
    rustls rejects the Agnes leaf cert with `CaUsedAsEndEntity`, regardless
    of `--native-tls` (the rejection is at chain validation, not at trust
    lookup — putting the cert in the OS store doesn't fix it). Solution:
    download the wheel with `curl --cacert` (curl uses OpenSSL, no rustls),
    then `uv tool install --native-tls` from the local file. PyPI deps
    still resolve over HTTPS, but `--native-tls` makes uv use the OS trust
    store for that path, which is fine because PyPI's CA chain is public.

    When `has_ca=False`, we trust the server's cert is publicly valid, so
    the simple curl-then-install pattern works without the cert flags.

    This is the ONE step that cannot be delegated to `agnes onboard` — it
    is what puts `agnes` on the machine — so it keeps its two recovery
    hints (missing `uv`, `~/.local/bin` not on PATH), the two failures
    every install session actually hits.
    """
    if has_ca:
        return [
            "1) Install the CLI.",
            "   The Agnes server's self-signed cert trips rustls' CaUsedAsEndEntity check,",
            "   so direct `uv tool install <https-url>` against the server fails (even",
            "   with --native-tls). Workaround: curl-then-local-install.",
            "",
            '   If someone else runs this, copy the commands below into the reply in full — not a reference to "step 1".',
            "",
            "   If uv is missing first, install it from the official instructions at",
            "   https://docs.astral.sh/uv/ — on Windows `winget install --id=astral-sh.uv`,",
            "   on macOS `brew install uv`. If you use the shell installer instead,",
            "   download it to a file and show it to me before running it.",
            "",
            *_install_cli_commands(has_ca=True, server_url_placeholder=server_url_placeholder),
            "",
            "   If `agnes --version` fails after install because ~/.local/bin is not on PATH:",
            '     export PATH="$HOME/.local/bin:$PATH"',
            "     # Persist for future shells. Use `grep -qF` (fixed-string,",
            "     # not regex) + `||` short-circuit so a re-run doesn't append",
            "     # a duplicate. Pick the rc file your login shell reads:",
            '     RC="$HOME/.zshrc"  # or ~/.bashrc / ~/.bash_profile',
            "     grep -qF '$HOME/.local/bin' \"$RC\" 2>/dev/null \\",
            '       || echo \'export PATH="$HOME/.local/bin:$PATH"\' >> "$RC"',
            "     # (The trust block in step 0 already does this for you on first run.)",
        ]
    return [
        "1) Install the CLI:",
        '   If someone else runs this, copy the commands below into the reply in full — not a reference to "step 1".',
        *_install_cli_commands(has_ca=False, server_url_placeholder=server_url_placeholder),
        "",
        "   If uv is not installed yet, install it from the official instructions at",
        "   https://docs.astral.sh/uv/ — on Windows `winget install --id=astral-sh.uv`,",
        "   on macOS `brew install uv`. If you use the shell installer instead,",
        "   download it to a file and show it to me before running it.",
        "",
        "   If `agnes --version` fails after install because ~/.local/bin is not on PATH:",
        '     export PATH="$HOME/.local/bin:$PATH"',
        "     # Persist for future shells. Use `grep -qF` (fixed-string, not",
        "     # regex) + `||` short-circuit so a re-run doesn't append a",
        "     # duplicate. Pick the rc file your login shell reads:",
        '     RC="$HOME/.zshrc"  # or ~/.bashrc / ~/.bash_profile',
        "     grep -qF '$HOME/.local/bin' \"$RC\" 2>/dev/null \\",
        '       || echo \'export PATH="$HOME/.local/bin:$PATH"\' >> "$RC"',
    ]


def _onboard_lines(server_url_placeholder: str = "{server_url}") -> list[str]:
    """Step 2 — `agnes onboard`, the whole of the old steps 2-6.

    `agnes onboard` is a deterministic state machine: directory check, init
    (auth off `~/.agnes/token`, a saved credential, or — with neither — its
    own browser-sign-in fallback; workspace files, Claude Code hooks, first
    `agnes pull`), catalog smoke, git/claude
    preflight, marketplace bootstrap, diagnose, summary. Each stage
    converges on a re-run and the command reports its own outcome, so the
    prompt neither enumerates the stages as instructions nor triages their
    errors.

    Three things still need the agent, and they are all this step says:

      * The directory decision is the user's. The CLI declines home and
        system directories and asks before adopting a directory that
        already holds unrelated files; the agent relays that and re-runs
        with `--accept-dir` only after an explicit yes. It must not pick or
        create a folder on its own — the authoritative unsafe-directory
        list lives in the CLI guard, not in prompt prose a model can talk
        itself out of.
      * `{workspace_dir}` is named only as the folder the install guide
        suggested, so the agent can echo the same suggestion the user
        already saw.
      * Same relay note as step 1 (`_install_cli_lines`): a real session hit
        this — declined to run step 1 itself (reasonably: downloading and
        executing a wheel), relayed those commands, but then reached this
        step, tried a handful of read-only CLI probes to answer an
        unrelated question instead of running THIS command, had every one
        of those probes blocked by its own tool-permission classifier, and
        left the person with no command to run themselves at all. Step 1
        already prints its commands regardless of who runs them; step 2
        did not, because it names the placeholder folder `.` rather than
        the substituted decision — nothing here forced the agent to surface
        the finished command line.
    """
    return [
        "2) Set up the {instance_brand} workspace in the current directory:",
        "",
        "   If someone else runs this, copy the exact command (with the agreed folder) into",
        '   the reply in full — not a reference to "step 2".',
        "",
        f'   agnes onboard --server-url "{server_url_placeholder}" --workspace .',
        "",
        "   The CLI checks the directory first: it declines home and system directories,",
        "   and asks before adopting a directory that already holds unrelated files.",
        "   Relay its instructions to the user and follow them — re-run with",
        "   `--accept-dir` only once the user has explicitly agreed to this directory",
        "   (the install guide suggested ~/Desktop/{workspace_dir}).",
        "   Don't pick or create a folder on your own.",
        "",
        "   From there `agnes onboard` converges the rest in one run — workspace init,",
        "   the first data pull, the marketplace plugins, and diagnostics — and is",
        "   safe to re-run. Read its summary; step 4 asks you to recap it.",
    ]


def _restart_lines() -> list[str]:
    """Step 3 — restart Claude Code.

    Marketplace plugins, MCP server registrations, and the SessionStart
    hooks installed by `agnes onboard` only load on the NEXT Claude Code
    session — without this step the user sits inside the setup session with
    stale state and re-discovers the requirement later.
    """
    return [
        "",
        "3) Restart Claude Code so every plugin, MCP server, and hook installed above actually loads:",
        "   Tell me to type `/exit` (or close the Claude Code session entirely), then run `claude` again from this same directory.",
    ]


def _confirm_lines() -> list[str]:
    """Step 4 — Confirm.

    Asks for the plain-language outcome the user cares about plus a recap
    of what `agnes onboard` itself reported. The bullets deliberately name
    only things the CLI prints, so the agent summarizes an observed
    transcript instead of re-deriving state (or hallucinating it).

    The closing "good first question" line mirrors `_default_llms_txt_body`'s
    "Verify the connection and try your first question" section — that newer,
    setup-code-based prompt already nudges the person toward `agnes catalog`
    and a data-oriented opener; this older, saved-token prompt was missing
    the same nudge, which meant the reply landed on installation ceremony
    (diagnose status, connectors) with nothing pointing the person at their
    actual data.
    """
    return [
        "",
        "4) Confirm:",
        '   Tell me "{instance_brand} workspace is ready" and recap what `agnes onboard`',
        "   reported:",
        "   - what it installed versus what was already present",
        "   - the diagnose status it finished on",
        "   - which connectors are available to set up later — nothing to do now,",
        '     just ask when you want one (e.g. "set up Jira")',
        "",
        '   A good first question from here: "What data can I access through',
        "   {instance_brand}? Give me a brief overview and suggest three useful",
        '   questions you can answer."',
    ]


def resolve_lines(
    wheel_filename: str,
    *,
    plugin_install_names: list[str] | None = None,
    server_host: str = "",
    ca_pem: str | None = None,
    connector_manifest: list[Any] | None = None,
    instance_brand: str = "Agnes",
    workspace_dir: str = "Agnes",
    custom_preamble: str = "",
) -> list[str]:
    """Return the template lines with server-side placeholders substituted.

    Pre-substitutes `{server_host}`, `{workspace_dir}` and
    `{instance_brand}`. Leaves `{server_url}` as a placeholder for
    click-time JS substitution (or for `render_setup_instructions()`
    below). The access token is never a placeholder here — see the module
    docstring.

    `ca_pem` (PEM-encoded fullchain of the Agnes server's TLS cert) gates
    the cross-platform step-0 trust-bootstrap block AND switches step 1 to
    the curl-then-local-install pattern. Caller decides whether the cert
    needs the bootstrap (typically: skip for publicly-trusted certs like
    Let's Encrypt, emit for self-signed or private corp CA).

    `plugin_install_names` and `connector_manifest` are accepted for caller
    compatibility and IGNORED — the marketplace and connector work moved
    into `agnes onboard`. See the module docstring. In particular this
    function never loads the connector manifest, so it stays DB-free,
    seed-free and cheap enough to call on every page render.

    `wheel_filename` is likewise unused by the built-in body (step 1
    downloads via the unversioned `/cli/download` endpoint), but its
    substitution is still applied to every line so an operator-authored
    `custom_preamble` that references `{wheel_filename}` keeps resolving.
    """
    del plugin_install_names, connector_manifest  # accepted, ignored

    has_ca = bool(ca_pem and ca_pem.strip())

    lines: list[str] = []
    if has_ca:
        lines.extend(_tls_trust_block(ca_pem))  # type: ignore[arg-type]
    lines.extend(_preamble_lines(custom_preamble=custom_preamble, instance_brand=instance_brand))
    lines.extend(_install_cli_lines(has_ca=has_ca))  # 1
    lines.append("")
    lines.extend(_onboard_lines())  # 2
    lines.extend(_restart_lines())  # 3
    lines.extend(_confirm_lines())  # 4

    return [
        line.replace("{wheel_filename}", wheel_filename)
        .replace("{server_host}", server_host)
        .replace("{workspace_dir}", workspace_dir)
        .replace("{instance_brand}", instance_brand)
        for line in lines
    ]


def render_setup_instructions(
    server_url: str,
    token: str,
    wheel_filename: str = "agnes.whl",
    *,
    plugin_install_names: list[str] | None = None,
    server_host: str = "",
    ca_pem: str | None = None,
    connector_manifest: list[Any] | None = None,
    instance_brand: str = "Agnes",
    workspace_dir: str = "Agnes",
    custom_preamble: str = "",
) -> str:
    """Render the setup instructions as a single string.

    Used server-side for tests and any non-JS rendering path. The browser
    clipboard flow uses the JS renderer embedded in the Jinja partial; both
    must produce byte-identical output for a given (server_url, host,
    ca_pem, brand, workspace_dir, custom_preamble) tuple.

    `token` is accepted for backward compatibility with existing callers
    but is otherwise unused: the rendered body deliberately contains no
    `{token}` placeholder (the access token is delivered out-of-band, see
    the module docstring), so the trailing `.replace("{token}", token)`
    below is a no-op today.
    """
    lines = resolve_lines(
        wheel_filename,
        plugin_install_names=plugin_install_names,
        server_host=server_host,
        ca_pem=ca_pem,
        connector_manifest=connector_manifest,
        instance_brand=instance_brand,
        workspace_dir=workspace_dir,
        custom_preamble=custom_preamble,
    )
    text = "\n".join(lines)
    return text.replace("{server_url}", server_url).replace("{token}", token)
