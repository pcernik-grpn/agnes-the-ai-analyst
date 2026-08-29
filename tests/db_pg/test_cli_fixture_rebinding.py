"""Regression: `cli_client_both` must rebind v2 helpers in command modules
imported BEFORE the fixture ran.

Under xdist, `cli.commands.*` are routinely imported by an earlier test in
the same worker. The fixture's per-module rebinding loop used to compare a
module's local binding against the ALREADY-patched `cli.v2_client` attr — a
condition that can never hold — so early-imported modules kept their real
httpx bindings and dialed the configured server (CI: `localhost:8000` →
ECONNREFUSED). This file pins the fix: the first test forces the early
import; the second poisons the server URL so any unpatched path fails
loudly, then drives the exact commands that failed in CI.
"""


def test_a_preimport_command_modules():
    import cli.commands.my_stack  # noqa: F401
    import cli.commands.catalog  # noqa: F401
    import cli.commands.schema  # noqa: F401


def test_b_early_imported_modules_are_rebound(cli_client_both, monkeypatch):
    # Any unpatched path would try this unroutable address and error loudly
    # instead of silently reaching a stray local server or a dev's real config.
    monkeypatch.setenv("AGNES_SERVER", "http://127.0.0.1:9")

    import cli.commands.my_stack as ms
    import cli.v2_client as v2

    # The module binding must be the fixture's replacement, not v2's original.
    assert ms.api_get_json is v2.api_get_json, (
        "cli.commands.my_stack.api_get_json was not rebound — the fixture's "
        "identity check regressed to comparing against the patched attr"
    )

    for args in (["my-stack", "show"], ["catalog"]):
        result = cli_client_both["invoke"](args)
        assert result.exit_code == 0, f"{args}: {result.output}"
