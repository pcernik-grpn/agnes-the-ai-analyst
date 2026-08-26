"""Tests for the THIN setup-instructions template + resolver.

The install prompt is a stub now: preamble + token pre-check, step 1
install the CLI, step 2 `agnes onboard`, step 3 restart Claude Code,
step 4 confirm. Everything that used to be an English program executed by
the agent (workspace triage, catalog smoke, git/claude preflight,
marketplace bootstrap, diagnose, connector tiles) lives inside
`agnes onboard` — see
`docs/superpowers/specs/2026-08-19-thin-install-prompt-design.md`.

Two pieces survive from the fat prompt and are still pinned here:

  * `_install_cli_lines` — both variants (curl-then-local-install when a
    private CA is bootstrapped, plain `uv tool install` otherwise) plus the
    missing-uv and PATH recovery hints.
  * `_tls_trust_block` — the cross-platform trust bootstrap. It cannot move
    into the CLI (it runs *before* the CLI can be downloaded), so the
    self-signed scenario stays automatic: a PEM in, step 0 out.

`{wheel_filename}`, `plugin_install_names` and `connector_manifest` are
still accepted by `resolve_lines()` / `render_setup_instructions()` for
caller compatibility, but are ignored — the tests below pin that too.
"""


# ---------------------------------------------------------------------------
# Accepted-but-ignored kwargs / placeholder plumbing
# ---------------------------------------------------------------------------


def test_resolve_lines_substitutes_wheel_filename():
    """`wheel_filename` is accepted for backward compatibility, but step 1
    downloads via the unversioned `/cli/download` endpoint instead of
    pinning the filename into a `/cli/wheel/<name>` URL."""
    from app.web.setup_instructions import resolve_lines

    lines = resolve_lines("agnes_the_ai_analyst-2.0.0-py3-none-any.whl")
    joined = "\n".join(lines)
    assert "{wheel_filename}" not in joined
    assert "/cli/wheel/" not in joined
    assert "/cli/download" in joined


def test_wheel_filename_substitution_still_applies_to_operator_copy():
    """The built-in body never emits `{wheel_filename}`, but the
    substitution still runs over every line so an operator-authored
    preamble that references it keeps resolving."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(
        resolve_lines(
            "agnes_the_ai_analyst-9.9.9-py3-none-any.whl",
            custom_preamble="Mirror {wheel_filename} to the internal artifact store first.",
        )
    )
    assert "Mirror agnes_the_ai_analyst-9.9.9-py3-none-any.whl to the internal" in joined
    assert "{wheel_filename}" not in joined


def test_resolve_lines_fallback_filename_is_honoured():
    """Callers pass `'agnes.whl'` when no wheel is on disk; resolve_lines
    still renders cleanly (the value is accepted but unused)."""
    from app.web.setup_instructions import resolve_lines

    lines = resolve_lines("agnes.whl")
    assert "{wheel_filename}" not in "\n".join(lines)
    assert any("/cli/download" in line for line in lines)


def test_ignored_kwargs_do_not_change_the_render():
    """`plugin_install_names` and `connector_manifest` are accepted for
    caller compatibility and ignored — no marketplace block, no connector
    tiles. Every combination renders byte-identically."""
    from app.web.setup_instructions import resolve_lines

    baseline = "\n".join(resolve_lines("agnes.whl"))
    assert "\n".join(resolve_lines("agnes.whl", plugin_install_names=["foo", "bar"])) == baseline
    assert "\n".join(resolve_lines("agnes.whl", connector_manifest=[])) == baseline

    from src.connectors_manifest import ConnectorEntry

    manifest = [
        ConnectorEntry(
            slug="connector-xtool",
            display_name="XTool",
            short_summary="XTool summary.",
            estimated_minutes=1,
            required=True,
        )
    ]
    assert "\n".join(resolve_lines("agnes.whl", connector_manifest=manifest)) == baseline


def test_resolve_lines_never_loads_the_connector_manifest(monkeypatch):
    """The renderer must not touch the seed at all: a manifest loader that
    blows up cannot break the install prompt."""

    def _boom():  # pragma: no cover - must never be called
        raise AssertionError("load_manifest() must not be called by the thin prompt")

    monkeypatch.setattr("src.connectors_manifest.load_manifest", _boom)

    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl"))
    assert "agnes onboard" in joined


def test_render_setup_instructions_wires_all_placeholders():
    from app.web.setup_instructions import render_setup_instructions

    out = render_setup_instructions(
        server_url="https://agnes.example.com",
        token="T-123",
        wheel_filename="agnes_the_ai_analyst-2.0.0-py3-none-any.whl",
    )
    assert "{server_url}" not in out
    assert "{token}" not in out
    assert "{wheel_filename}" not in out
    assert "https://agnes.example.com/cli/download" in out
    # The token is delivered out-of-band (written to ~/.agnes/token before
    # this prompt is generated) — its raw value must NEVER appear in the
    # rendered text, even though the `token` kwarg is still accepted for
    # backward compatibility.
    assert "T-123" not in out


# ---------------------------------------------------------------------------
# Thin layout
# ---------------------------------------------------------------------------


def test_thin_layout_has_four_steps():
    """1 install, 2 onboard, 3 restart, 4 confirm — in that order, with no
    fifth step and no stray Confirm at another position."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl"))
    assert "1) Install the CLI" in joined
    assert "2) Set up the Agnes workspace in the current directory" in joined
    assert "3) Restart Claude Code" in joined
    assert "4) Confirm:" in joined
    assert "5)" not in joined
    for stray in ("1) Confirm:", "2) Confirm:", "3) Confirm:", "5) Confirm:"):
        assert stray not in joined
    install_idx = joined.index("1) Install the CLI")
    onboard_idx = joined.index("2) Set up the Agnes workspace")
    restart_idx = joined.index("3) Restart Claude Code")
    confirm_idx = joined.index("4) Confirm:")
    assert install_idx < onboard_idx < restart_idx < confirm_idx


def test_orchestration_steps_are_gone():
    """Everything the CLI now owns must be absent from the prompt: the
    catalog smoke step, the git/claude preflight, the marketplace
    bootstrap, diagnose, and the connector tiles."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl"))
    assert "Verify the data is queryable" not in joined
    assert "agnes catalog" not in joined
    assert "agnes refresh-marketplace" not in joined
    assert "agnes my-stack show" not in joined
    assert "Register the Agnes Claude Code marketplace" not in joined
    assert "Run diagnostics:" not in joined
    assert "Connect the user's tools" not in joined
    assert "Install required tools" not in joined
    assert "agnes connectors show" not in joined
    assert "claude plugin install" not in joined
    assert "agnes init" not in joined


def test_retired_helpers_are_deleted():
    """The step-number machinery and the block builders that produced the
    deleted sections must not survive as dead code."""
    from app.web import setup_instructions as si

    for name in (
        "_step_numbers",
        "_connectors_block",
        "_required_connectors_block",
        "_marketplace_block",
        "_diagnose_lines",
        "_init_lines",
        "_finale_lines",
        "_restart_claude_lines",
        "_load_connector_body",
    ):
        assert not hasattr(si, name), f"{name} should have been deleted with the fat prompt"


def test_prompt_stays_short():
    """The whole point of the rewrite: the default render is a stub, not a
    program. Generous ceiling so wording tweaks don't churn the test, tight
    enough to catch a deleted section creeping back in (the fat prompt was
    163 lines; ~30 of what is left is step 1's install + recovery hints,
    which cannot move into a CLI that isn't installed yet)."""
    from app.web.setup_instructions import resolve_lines

    assert len(resolve_lines("agnes.whl")) < 75


# ---------------------------------------------------------------------------
# Preamble + token pre-check
# ---------------------------------------------------------------------------


def test_preamble_opens_with_brand_server_and_token_guard():
    from app.web.setup_instructions import resolve_lines

    lines = resolve_lines("agnes.whl")
    joined = "\n".join(lines)
    assert lines[0] == "Set up the Agnes CLI on this machine."
    assert "Server: {server_url}" in joined
    # Brand/host/binary coherence: the prompt carries the operator's product
    # name, downloads from this instance's own host, and installs a binary
    # called `agnes`. Say once that the three name one system — an agent
    # given three unfamiliar names and no relation between them has to treat
    # the mismatch as a red flag.
    #
    # Unbranded instance: brand IS "Agnes", so there is no third name and the
    # "own deployment of Agnes" clause would render as the tautology "Agnes is
    # this organization's own deployment of Agnes". It is dropped; the server
    # and the binary name are still stated.
    assert "own deployment of Agnes" not in joined
    assert "Agnes is served from {server_url}" in joined
    assert "installs is named `agnes`" in joined
    # Token handling stated as a fact, not as an instruction to conceal:
    # the steps use the file path, so nothing needs to display its contents.
    assert "Your login token is already saved on this machine at ~/.agnes/token" in joined
    assert "no need to display its contents" in joined
    assert "never print the token" not in joined
    # Provenance fact: the token came from the install guide's previous step.
    assert "step 4 of the install guide at {server_url}" in joined
    # Idempotence promise (one line, not a paragraph).
    assert "idempotent" in joined


def test_preamble_names_brand_host_and_binary_as_one_system_when_branded():
    """A rebranded instance is the case the coherence sentence exists for.

    The prompt then carries three names an agent cannot relate on its own —
    the operator's product name, the instance's own hostname, and a binary
    called `agnes` — and an unexplained mismatch between them is the
    look-alike-domain signal that stalled a real install. Assert the
    sentence is present and names all three.
    """
    from app.web.setup_instructions import render_setup_instructions

    rendered = render_setup_instructions(
        server_url="https://analyst-acme.example.net",
        token="",
        instance_brand="Foundry AI",
    )
    assert "Foundry AI is this organization's own deployment of Agnes, served" in rendered
    assert "https://analyst-acme.example.net" in rendered
    assert "installs is named `agnes`" in rendered


def test_preamble_asserts_no_consent_on_the_assistants_behalf():
    """The prompt states verifiable facts and leaves the ask/no-ask
    judgment to the assistant — it must never pre-declare the user's
    go-ahead or argue the host is trusted."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl"))
    for phrase in (
        "go-ahead",
        "not unknown",
        "already approved",
        "you have permission",
        "no need to ask",
    ):
        assert phrase not in joined


def test_token_precheck_block():
    """The pre-check keeps both branches of "the file isn't there": a fresh
    install (stop, send the user back to the guide) and a reconcile (the
    saved credential already exists — continue)."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl"))
    assert "test -s ~/.agnes/token" in joined
    assert "{server_url}/home" in joined
    assert "step 4" in joined
    assert "~/.config/agnes/token.json" in joined
    # The pre-check is prose, not a numbered step — step 0 belongs to the
    # TLS trust block, which must be free to claim that number.
    assert "0) Check" not in joined


def test_token_precheck_requires_both_a_credential_and_a_matching_server():
    """A missing token file must not be waved through by either signal alone.

    Neither one discriminates by itself, in opposite directions:

    `token.json` holds `{access_token, email}` and never the server, and there
    is one per machine — so on a laptop signed in to a different Agnes
    deployment it exists and proves nothing about this one. That was the
    original false positive.

    And `config.yaml`'s `server:` key is written by `/cli/install.sh` at
    install time (`app/api/cli_artifacts.py`), which prints "1. Sign in…"
    immediately after — so a machine that merely ran the installer matches the
    server while nobody has ever signed in. Keying on the server *instead of*
    token.json swapped one false positive for another, and this one is worse:
    it fires on the ordinary fresh-install path, and the agent is told to
    continue only to fail three steps later inside `agnes init --token-file`.

    So the check requires BOTH, and anything else stops.
    """
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl"))

    # Neither false-positive wording survives.
    assert "so just continue" not in joined
    assert "an earlier run already" not in joined
    assert "an earlier run saved the credential and removed the" not in joined

    # Both signals are tested, in one command so the agent cannot satisfy
    # half of it.
    assert "test -f ~/.config/agnes/token.json &&" in joined
    assert "^server:" in joined
    assert "~/.config/agnes/config.yaml" in joined

    # Only the conjunction continues; everything else, including no output at
    # all (the `test -f` short-circuit), stops.
    assert "Prints {server_url} → continue" in joined
    assert "including no output" in joined
    assert "stop, send the user to {server_url}/home step 4" in joined

    # Both reasons are stated, so neither shortcut is reintroduced.
    assert "token.json records" in joined
    assert "no server" in joined
    assert "installer writes server: before anyone has signed in" in joined


def test_preamble_carries_no_pre_emptive_trust_assertion():
    """The preamble must not answer a trust question on the reader's behalf.

    It used to: with a trust block emitted it appended "The fallback chain
    inside step 0(d) is documented and OK to use; that's what fallback
    chains are for", and an earlier revision told the agent that "whether
    that host is trusted is the user's org's call". Read back out of a real
    install transcript, that reassurance was quoted as the reason to
    distrust the prompt — text that pre-empts a safety judgement reads as
    written to defuse one. The TLS *guidance* stays, in both renders.
    """
    from app.web.setup_instructions import resolve_lines

    fake_ca = "-----BEGIN CERTIFICATE-----\nFAKEFAKEFAKE\n-----END CERTIFICATE-----\n"
    for label, joined in (
        ("no-ca", "\n".join(resolve_lines("agnes.whl"))),
        ("ca", "\n".join(resolve_lines("agnes.whl", ca_pem=fake_ca))),
    ):
        assert "OK to use" not in joined, label
        assert "org's call" not in joined, label
        assert "verify it with their IT" not in joined, label
        # Generic, causally-phrased TLS advice survives in both renders.
        assert "rather than lowering certificate" in joined, label


# ---------------------------------------------------------------------------
# Step 2 — agnes onboard
# ---------------------------------------------------------------------------


def test_onboard_step_relays_the_cli_directory_check():
    """The directory decision lives in the CLI. The prompt's job is to
    relay the CLI's instructions and to keep the agent from picking a
    folder for the user; `--accept-dir` is only for an explicit yes."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl"))
    assert 'agnes onboard --server-url "{server_url}" --workspace .' in joined
    assert "Relay its instructions to the user" in joined
    assert "--accept-dir" in joined
    assert "explicitly agreed" in joined
    assert "Don't pick or create a folder on your own." in joined
    # What onboard converges, in one sentence — the recap in step 4 asks
    # the agent to report on exactly these.
    assert "safe to re-run" in joined
    for word in ("workspace", "data pull", "marketplace", "diagnostics"):
        assert word in joined


def test_onboard_step_has_no_security_judgment_suppression():
    """The install prompt must NOT instruct the agent to suppress its own
    security judgment around the credential. Claude Code's hardened
    security protocol reads such copy as an attempt to bypass its own
    protections and blocks the install."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl"))
    assert "security incident" not in joined
    assert "do not warn" not in joined
    assert "do not mark this session" not in joined
    assert "do not run `agnes mark-private`" not in joined
    assert "auto-classifier" not in joined
    assert "credential-exfil" not in joined
    assert "escape hatch" not in joined
    assert "/agnes-private" not in joined


def test_unsafe_dir_enumeration_lives_in_the_cli_not_the_prompt():
    """One source of truth: the prompt names no directory list at all, so a
    model can't talk itself into installing into /etc off prompt prose. The
    CLI's own guard is the authority and reports the refusal at runtime."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl"))
    assert "home and system directories" in joined
    for path in ("/etc", "/usr", "/var", "/opt", "/root"):
        assert path not in joined


# ---------------------------------------------------------------------------
# Steps 3 + 4 — restart, confirm
# ---------------------------------------------------------------------------


def test_restart_step_emitted_in_every_layout():
    """Step 3 renders with and without the trust block so users never
    finish setup sitting in a stale Claude Code session that has not loaded
    the freshly-installed plugins / MCP servers / hooks."""
    from app.web.setup_instructions import resolve_lines

    fake_ca = "-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n"
    for kwargs in ({}, {"ca_pem": fake_ca}):
        joined = "\n".join(resolve_lines("agnes.whl", **kwargs))
        assert "3) Restart Claude Code" in joined, f"missing restart step for kwargs={kwargs!r}"
        assert "/exit" in joined
        assert "claude` again" in joined
        assert "same directory" in joined


def test_confirm_step_recaps_the_onboard_summary():
    """Step 4 asks for the brand-ready sentence plus a recap of what
    `agnes onboard` itself reported — installed vs already present, the
    diagnose status, and the connectors that can be set up later just by
    asking."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl"))
    assert '"Agnes workspace is ready"' in joined
    assert "already present" in joined
    assert "diagnose" in joined
    assert "connectors" in joined
    assert "just ask" in joined


# ---------------------------------------------------------------------------
# Brand / workspace_dir threading
# ---------------------------------------------------------------------------


def test_brand_and_workspace_dir_substitution():
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(
        resolve_lines(
            "agnes.whl",
            instance_brand="Foundry AI",
            workspace_dir="FoundryAI",
        )
    )
    assert "Set up the Foundry AI CLI on this machine." in joined
    assert "2) Set up the Foundry AI workspace in the current directory" in joined
    assert '"Foundry AI workspace is ready"' in joined
    assert "~/Desktop/FoundryAI" in joined
    assert "{instance_brand}" not in joined
    assert "{workspace_dir}" not in joined


def test_server_host_is_substituted_server_side():
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl", server_host="agnes.example.com"))
    assert "{server_host}" not in joined
    # {server_url} stays a click-time placeholder for the JS renderer, and
    # the access token is never a placeholder at all.
    assert "{server_url}" in joined
    assert "{token}" not in joined
    assert "eyJ" not in joined


# ---------------------------------------------------------------------------
# Step 1 — CLI install (both variants)
# ---------------------------------------------------------------------------


_FAKE_CA_PEM = (
    "-----BEGIN CERTIFICATE-----\n"
    "MIIBkTCB+wIJAKf9$x`cNotARealCert\n"  # `$` and backtick: smoke test for shell-quote safety
    "thisIsNotARealCertificateBodyJustAnInlinePlaceholder==\n"
    "-----END CERTIFICATE-----\n"
)


def test_resolve_lines_with_ca_pem_switches_step_one_to_curl_then_local_install():
    """Step 1 always downloads via /cli/download into a local file first;
    has_ca only changes whether curl carries --cacert and whether uv gets
    --native-tls (avoids rustls CaUsedAsEndEntity):
    - has_ca=True  → curl --cacert ... then uv tool install --native-tls
    - has_ca=False → curl ... then uv tool install (no cert flags)

    Both forms cap redirects at zero (`-L --max-redirs 0`, curl exit 47):
    `-OJ` names the saved file from the response, so a cross-host redirect
    would otherwise install whichever wheel the hop served, silently.
    """
    from app.web.setup_instructions import resolve_lines

    joined_ca = "\n".join(resolve_lines("agnes-1.0-py3-none-any.whl", ca_pem=_FAKE_CA_PEM))
    assert ("curl -fsSL --max-redirs 0 --cacert ~/.agnes/ca.pem -OJ {server_url}/cli/download") in joined_ca
    assert "TMPDIR_WHEEL=$(mktemp -d -t agnes_cli.XXXXXX)" in joined_ca
    assert 'uv tool install --native-tls --force "$WHEEL"' in joined_ca
    assert "/cli/wheel/" not in joined_ca

    joined_plain = "\n".join(resolve_lines("agnes-1.0-py3-none-any.whl"))
    assert "curl -fsSL --max-redirs 0 -OJ {server_url}/cli/download" in joined_plain
    assert 'uv tool install --force "$WHEEL"' in joined_plain
    assert "curl -fsSL --cacert" not in joined_plain
    assert "/cli/wheel/" not in joined_plain
    assert "uv tool install --native-tls" not in joined_plain


def test_install_step_keeps_missing_uv_and_path_recovery_hints():
    """Both variants keep the recovery hints — a missing `uv` and a
    `~/.local/bin` that isn't on PATH are the two failures every install
    session hits."""
    from app.web.setup_instructions import resolve_lines

    for kwargs in ({}, {"ca_pem": _FAKE_CA_PEM}):
        joined = "\n".join(resolve_lines("agnes.whl", **kwargs))
        assert "https://docs.astral.sh/uv/" in joined
        assert "winget install --id=astral-sh.uv" in joined
        assert "brew install uv" in joined
        assert "download it to a file and show it to me before running it" in joined
        assert 'export PATH="$HOME/.local/bin:$PATH"' in joined
        assert "grep -qF '$HOME/.local/bin'" in joined


# ---------------------------------------------------------------------------
# Step 0 — TLS trust block (unchanged from the fat prompt)
# ---------------------------------------------------------------------------


def test_resolve_lines_with_ca_pem_emits_step_zero_trust_block():
    from app.web.setup_instructions import resolve_lines

    lines = resolve_lines("agnes.whl", ca_pem=_FAKE_CA_PEM)
    joined = "\n".join(lines)

    assert "0) Trust the Agnes TLS certificate" in joined
    assert joined.index("0) Trust the Agnes TLS certificate") < joined.index("1) Install the CLI")

    # PEM body inlined verbatim, flush-left (heredoc would corrupt indented content).
    assert "-----BEGIN CERTIFICATE-----" in joined
    assert "-----END CERTIFICATE-----" in joined
    # The PEM is passed inside a single-quoted heredoc so `$` / backtick
    # in real-world cert bodies are NOT shell-expanded — preserve verbatim.
    assert "MIIBkTCB+wIJAKf9$x`cNotARealCert" in joined
    assert "<<'AGNES_CA_PEM'" in joined


def test_resolve_lines_with_ca_pem_emits_cross_platform_substeps():
    """Step 0 must contain the cross-platform sub-blocks: platform detection,
    OS-trust-store registration, combined CA bundle build, env persistence."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl", ca_pem=_FAKE_CA_PEM))

    # (a) Platform detection — uname-driven, with all three families covered.
    assert 'case "$(uname -s)" in' in joined
    assert "Darwin" in joined and "PLATFORM=macos" in joined
    assert "Linux" in joined and "PLATFORM=linux" in joined
    assert "MINGW*|MSYS*|CYGWIN*" in joined and "PLATFORM=windows" in joined
    assert 'SHELL_NAME="$(basename "${SHELL:-bash}")"' in joined
    assert "bash:macos)" in joined and ".bash_profile" in joined

    # (c) OS trust store registration — one command per platform.
    assert "certutil.exe -user -addstore" in joined  # Windows
    assert "security add-trusted-cert -r trustRoot" in joined  # macOS
    assert "update-ca-certificates" in joined  # Linux Debian
    assert "update-ca-trust" in joined  # Linux RHEL

    # (d) Combined CA bundle — multi-source fallback chain.
    assert "ca-bundle.pem" in joined
    assert "import certifi; print(certifi.where())" in joined
    assert "/mingw64/ssl/certs/ca-bundle.crt" in joined
    assert "/etc/ssl/certs/ca-certificates.crt" in joined
    assert "/etc/ssl/cert.pem" in joined
    assert "uv run --native-tls --with certifi --no-project" in joined


def test_resolve_lines_with_ca_pem_uses_combined_bundle_for_replace_envs():
    """SSL_CERT_FILE/REQUESTS_CA_BUNDLE/GIT_SSL_CAINFO must point at the
    COMBINED bundle (~/.agnes/ca-bundle.pem), not at the single Agnes cert.
    NODE_EXTRA_CA_CERTS keeps pointing at just ca.pem because Node's
    semantics is additive (appends to bundled roots)."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl", ca_pem=_FAKE_CA_PEM))

    assert 'export SSL_CERT_FILE="$HOME/.agnes/ca-bundle.pem"' in joined
    assert 'export REQUESTS_CA_BUNDLE="$HOME/.agnes/ca-bundle.pem"' in joined
    assert 'export GIT_SSL_CAINFO="$HOME/.agnes/ca-bundle.pem"' in joined
    assert 'export NODE_EXTRA_CA_CERTS="$HOME/.agnes/ca.pem"' in joined

    assert "AGNES_CA_PEM_TRUST" in joined  # marker grep-checks for
    assert "AGNES_RC_BLOCK" in joined  # the rc-append heredoc delimiter


def test_trust_block_rc_heredoc_writes_exactly_8_lines():
    """The trust block emits a heredoc that appends to the user's shell rc.
    The companion `agnes-client-reset.sh` strips the block via awk that
    `skip = 8` from the AGNES_CA_PEM_TRUST marker, so the heredoc MUST
    write exactly 8 lines (marker + 7 export/comment lines)."""
    from app.web.setup_instructions import _tls_trust_block

    fake_ca = "-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n"
    lines = _tls_trust_block(fake_ca)
    joined = "\n".join(lines)

    start = joined.index("<<'AGNES_RC_BLOCK'")
    end = joined.index("\nAGNES_RC_BLOCK\n", start)
    after_open = joined.index("\n", start) + 1
    body = joined[after_open:end]
    body_lines = body.split("\n")

    assert len(body_lines) == 8, (
        f"Heredoc body has {len(body_lines)} lines; reset script awk "
        f"skips 8 lines, so any drift leaves stray lines in the rc file. "
        f"Body was:\n" + "\n".join(f"  {i + 1:2d} {ln!r}" for i, ln in enumerate(body_lines))
    )
    assert body_lines[0] == "# AGNES_CA_PEM_TRUST — added by Agnes setup"


def test_trust_block_rc_heredoc_count_matches_reset_script_skip():
    """Stronger version of the previous test: read the actual `skip = N`
    integer literal out of `scripts/dev/agnes-client-reset.sh` and assert
    it matches the heredoc body line count."""
    import re
    from pathlib import Path

    from app.web.setup_instructions import _tls_trust_block

    fake_ca = "-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n"
    joined = "\n".join(_tls_trust_block(fake_ca))
    start = joined.index("<<'AGNES_RC_BLOCK'")
    end = joined.index("\nAGNES_RC_BLOCK\n", start)
    after_open = joined.index("\n", start) + 1
    body_line_count = len(joined[after_open:end].split("\n"))

    repo_root = Path(__file__).resolve().parents[1]
    reset_sh = (repo_root / "scripts" / "dev" / "agnes-client-reset.sh").read_text()
    match = re.search(r"AGNES_CA_PEM_TRUST.*?skip\s*=\s*(\d+)", reset_sh, re.DOTALL)
    assert match, "Could not locate `skip = N` near AGNES_CA_PEM_TRUST in reset script"
    reset_skip = int(match.group(1))

    assert body_line_count == reset_skip, (
        f"Heredoc body has {body_line_count} lines but reset script skips "
        f"{reset_skip}. Update one side to match — either trim the heredoc "
        f"or bump the awk skip count."
    )


def test_trust_block_references_no_step_number():
    """Step 0(c) must justify the OS-trust-store registration by naming the
    consequence (the marketplace git clone the CLI runs), never a step
    number — the numbered steps around it have been renumbered twice."""
    from app.web.setup_instructions import resolve_lines

    fake_ca = "-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n"
    joined = "\n".join(resolve_lines("agnes.whl", ca_pem=fake_ca))
    assert "step 7's marketplace add" not in joined
    assert "marketplace `git" in joined and "clone`" in joined


def test_resolve_lines_ca_pem_empty_string_is_treated_as_absent():
    """`ca_pem=''` (or whitespace-only) must NOT emit the trust block —
    same as None. Guards against `Path.read_text()` returning empty for
    a touched-but-unwritten cert file."""
    from app.web.setup_instructions import resolve_lines

    for empty in ("", "   ", "\n\n"):
        joined = "\n".join(resolve_lines("agnes.whl", ca_pem=empty))
        assert "0) Trust the Agnes TLS certificate" not in joined
        assert "curl -fsSL --cacert" not in joined


def test_render_setup_instructions_propagates_ca_pem():
    from app.web.setup_instructions import render_setup_instructions

    out = render_setup_instructions(
        server_url="https://agnes.example.com",
        token="T-CA",
        wheel_filename="agnes-1.0-py3-none-any.whl",
        server_host="agnes.example.com",
        ca_pem=_FAKE_CA_PEM,
    )
    assert "0) Trust the Agnes TLS certificate" in out
    assert "-----BEGIN CERTIFICATE-----" in out
    assert "git config --global" not in out
    assert "{server_url}" not in out
    assert "{token}" not in out
    assert "T-CA" not in out
    assert "https://agnes.example.com/cli/download" in out
    assert "/cli/wheel/" not in out
    assert 'uv tool install --native-tls --force "$WHEEL"' in out


# ---------------------------------------------------------------------------
# Global invariants
# ---------------------------------------------------------------------------


def test_resolve_lines_no_sslverify_downgrade_anywhere():
    """The legacy `git config sslVerify=false` downgrade is gone in every
    rendering combination. Self-signed and private-CA servers must place
    the fullchain at AGNES_TLS_FULLCHAIN_PATH (default
    /data/state/certs/fullchain.pem) so step 0 picks it up via
    _read_agnes_ca_pem; publicly-trusted certs need no trust block at
    all. There is no third path."""
    from app.web.setup_instructions import resolve_lines

    for kwargs in (
        {"server_host": "agnes.example.com"},
        {"server_host": "agnes.example.com", "ca_pem": _FAKE_CA_PEM},
        {},
    ):
        joined = "\n".join(resolve_lines("agnes.whl", **kwargs))
        assert "git config --global" not in joined, f"sslVerify downgrade leaked through with kwargs={kwargs!r}"
        assert "sslVerify" not in joined, f"sslVerify downgrade leaked through with kwargs={kwargs!r}"


def test_unified_flow_uses_only_agnes_verbs():
    """No-legacy-`da`-verbs invariant: every line emitted by
    `resolve_lines()` uses the `agnes` CLI verb. Match `"da "` (with the
    trailing space) so we don't false-positive on `Darwin`, `adapter`,
    `database`, etc."""
    from app.web.setup_instructions import resolve_lines

    fake_ca = "-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n"

    for kwargs in ({}, {"ca_pem": fake_ca}):
        joined = "\n".join(resolve_lines("agnes.whl", **kwargs))
        assert "da " not in joined, (
            f"Legacy `da ` verb leaked into resolve_lines output (kwargs={kwargs!r}).\n"
            f"Search the rendered prompt for the offending line."
        )
        assert "agnes onboard --server-url" in joined


def test_install_page_uses_versioned_wheel_url(monkeypatch, tmp_path):
    """End-to-end: the /setup preview must render the version-resilient
    /cli/download install path (immune to a mid-session server version
    roll), not a wheel_filename-pinned /cli/wheel/<name> URL."""
    wheel = tmp_path / "agnes_the_ai_analyst-2.0.0-py3-none-any.whl"
    wheel.write_bytes(b"PK\x03\x04")
    monkeypatch.setenv("AGNES_CLI_DIST_DIR", str(tmp_path))

    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    resp = client.get("/setup", headers={"host": "agnes.test", "Accept": "text/html"})
    assert resp.status_code == 200
    assert "/cli/download" in resp.text
    assert "/cli/wheel/" not in resp.text
    assert "/cli/agnes.whl" not in resp.text


# ---------------------------------------------------------------------------
# Operator-authored custom_preamble injected at the TOP.
# ---------------------------------------------------------------------------


def test_custom_preamble_appears_first_above_cli_line():
    """A non-empty `custom_preamble` is prepended above the
    `Set up the … CLI` opening line (before the numbered steps)."""
    from app.web.setup_instructions import resolve_lines

    lines = resolve_lines("agnes.whl", custom_preamble="TRUST LINE ONE\nTRUST LINE TWO")
    joined = "\n".join(lines)
    assert "TRUST LINE ONE" in joined
    assert "TRUST LINE TWO" in joined
    assert joined.index("TRUST LINE ONE") < joined.index("Set up the Agnes CLI")
    assert lines[0] == "TRUST LINE ONE"


def test_custom_preamble_substitutes_instance_brand():
    """`{instance_brand}` inside the preamble is substituted by the
    resolve_lines placeholder loop (just like the rest of the prompt)."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(
        resolve_lines(
            "agnes.whl",
            instance_brand="Foundry AI",
            custom_preamble="TRUST LINE {instance_brand}",
        )
    )
    assert "TRUST LINE Foundry AI" in joined
    assert "{instance_brand}" not in joined


def test_empty_custom_preamble_is_byte_identical_to_no_arg():
    """Empty `custom_preamble` (the default) must emit ZERO extra lines —
    the rendered prompt is byte-identical to the no-arg call."""
    from app.web.setup_instructions import resolve_lines

    baseline = "\n".join(resolve_lines("agnes.whl"))
    assert "\n".join(resolve_lines("agnes.whl", custom_preamble="")) == baseline


def test_render_setup_instructions_forwards_custom_preamble():
    """The string-rendering entry point threads `custom_preamble` through
    to the resolver."""
    from app.web.setup_instructions import render_setup_instructions

    out = render_setup_instructions(
        server_url="https://agnes.example.com",
        token="T-CP",
        wheel_filename="agnes-1.0-py3-none-any.whl",
        custom_preamble="OPERATOR TRUST NOTE",
    )
    assert out.startswith("OPERATOR TRUST NOTE")
