"""D1: `config.loader`'s "required fields" check no longer blocks boot.

Pre-D1, `_validate_config` raised `ValueError` when `instance.name`,
`auth.allowed_domain`, `server.host`, `server.hostname` or
`auth.webapp_secret_key` were missing/empty. That was fiction: a
provisioning-created VM ships no static `config/instance.yaml` at all (the
loader raises `FileNotFoundError` before `_validate_config` ever runs), and
even a static file that IS present but fails the check is caught by
`app.instance_config.load_instance_config`'s broad `except Exception` and
served on built-in defaults anyway. The raise only punished a direct caller
of `config.loader.load_instance_config()` that isn't going through that
wrapper. This test pins the honest behavior: warn, don't raise.
"""

import logging

from config.loader import _validate_config, load_instance_config


class TestValidateConfigWarnsInsteadOfRaising:
    def test_missing_all_recommended_fields_does_not_raise(self):
        # Must not raise even though every recommended field is absent.
        _validate_config({})

    def test_empty_string_fields_do_not_raise(self):
        _validate_config(
            {
                "instance": {"name": ""},
                "auth": {"allowed_domain": "", "webapp_secret_key": ""},
                "server": {"host": "", "hostname": ""},
            }
        )

    def test_missing_fields_log_a_warning_naming_each_path(self, caplog):
        with caplog.at_level(logging.WARNING, logger="config.loader"):
            _validate_config({})
        messages = "\n".join(r.getMessage() for r in caplog.records)
        for path in (
            "instance.name",
            "auth.allowed_domain",
            "server.host",
            "server.hostname",
            "auth.webapp_secret_key",
        ):
            assert path in messages, f"expected {path!r} named in the warning, got: {messages!r}"

    def test_fully_populated_config_logs_no_warning(self, caplog):
        complete = {
            "instance": {"name": "Acme"},
            "auth": {"allowed_domain": "acme.com", "webapp_secret_key": "secret"},
            "server": {"host": "10.0.0.1", "hostname": "acme.example.com"},
        }
        with caplog.at_level(logging.WARNING, logger="config.loader"):
            _validate_config(complete)
        assert not caplog.records


class TestLoadInstanceConfigDoesNotRaiseOnMissingFields:
    def test_load_instance_config_succeeds_with_missing_fields(self, tmp_path, monkeypatch):
        """A direct caller of ``config.loader.load_instance_config()`` (bypassing
        the ``app.instance_config`` wrapper's broad except) must get a config
        back, not a ValueError, when the static file omits the recommended
        fields — the file existing at all with SOME content is a legitimate,
        supported shape (e.g. a minimal local-dev instance.yaml)."""
        import config.loader as loader

        (tmp_path / "instance.yaml").write_text("config_version: 1\ndata_source:\n  type: local\n")
        monkeypatch.setattr(loader, "CONFIG_DIR", tmp_path)

        config = load_instance_config()

        assert config["config_version"] == 1
