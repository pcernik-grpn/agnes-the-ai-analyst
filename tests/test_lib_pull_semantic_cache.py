"""agnes pull — semantic-layer physical cache (Fáze 1, "distribuce jako
fyzická cache s TTL"). Codes against the `/api/semantic-models/bundle`
contract (`app/api/semantic_models.py::semantic_models_bundle`): a top-level
``{generated_at, ttl_seconds, models: [{slug, name, description, source,
source_ref, content_hash, document_json}, ...]}`` payload rendered by
`src/semantic/cache_render.py` into `<workspace>/semantic/<slug>/…`.

Reuses the stub-server idiom of `tests/test_lib_pull_digests.py`.
"""

from __future__ import annotations

import os
import stat

from unittest.mock import MagicMock

import pytest

from cli.lib.pull import run_pull


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path, monkeypatch):
    cfg = tmp_path / "_agnes_cfg"
    cfg.mkdir()
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(cfg))


def _model_row(
    slug="retail",
    name="retail",
    description="Retail semantic model.",
    content_hash="h1",
    source="manual",
    source_ref=None,
):
    return {
        "id": f"manual/_/{slug}",
        "slug": slug,
        "name": name,
        "description": description,
        "source": source,
        "source_ref": source_ref,
        "content_hash": content_hash,
        "document_json": {
            "semantic_model": [
                {
                    "name": name,
                    "description": description,
                    "datasets": [{"name": "orders", "source": "db.public.orders"}],
                    "metrics": [{"name": "revenue", "description": "Total revenue."}],
                }
            ]
        },
    }


def _bundle(models, ttl_seconds=86400, generated_at="2026-08-26T00:00:00Z"):
    return {"generated_at": generated_at, "ttl_seconds": ttl_seconds, "models": models}


def _server(monkeypatch, manifest=None, bundle=None, bundle_status=200):
    manifest = manifest if manifest is not None else {"tables": {}}

    def _api_get(path, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = lambda: None
        if path == "/api/sync/manifest":
            resp.json.return_value = manifest
        elif path == "/api/memory/bundle":
            resp.json.return_value = {"mandatory": [], "approved": []}
        elif path == "/api/semantic-models/bundle":
            resp.status_code = bundle_status
            if bundle_status == 404:

                def _raise():
                    raise RuntimeError("404")

                # run_pull's caller checks status_code == 404 BEFORE calling
                # raise_for_status, so this branch is never actually invoked
                # — kept only so a future refactor that reorders the checks
                # fails loudly instead of silently swallowing a 404.
                resp.raise_for_status = _raise
            resp.json.return_value = bundle or _bundle([])
        else:
            resp.json.return_value = {}
        return resp

    monkeypatch.setattr("cli.lib.pull.api_get", _api_get, raising=False)


def test_writes_brief_tables_metrics_for_an_accessible_model(tmp_path, monkeypatch):
    _server(monkeypatch, bundle=_bundle([_model_row()]))

    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

    slug_dir = tmp_path / "semantic" / "retail"
    assert (slug_dir / "_brief.md").exists()
    assert (slug_dir / "tables" / "orders.yml").exists()
    assert (slug_dir / "metrics" / "revenue.yml").exists()
    assert result.semantic_models_updated == 3
    assert result.semantic_models_removed == 0


def test_header_carries_generated_at_content_hash_and_ttl(tmp_path, monkeypatch):
    _server(
        monkeypatch,
        bundle=_bundle([_model_row(content_hash="deadbeef")], ttl_seconds=3600, generated_at="2026-01-01T00:00:00Z"),
    )
    run_pull(server_url="http://x", token="t", workspace=tmp_path)

    text = (tmp_path / "semantic" / "retail" / "tables" / "orders.yml").read_text()
    assert "generated_at: 2026-01-01T00:00:00Z" in text
    assert "content_hash: deadbeef" in text
    assert "source_slug: retail" in text
    assert "ttl_seconds: 3600" in text


def test_written_files_are_read_only(tmp_path, monkeypatch):
    _server(monkeypatch, bundle=_bundle([_model_row()]))
    run_pull(server_url="http://x", token="t", workspace=tmp_path)

    target = tmp_path / "semantic" / "retail" / "_brief.md"
    mode = stat.S_IMODE(os.stat(target).st_mode)
    assert mode == 0o444


def test_no_accessible_models_writes_nothing(tmp_path, monkeypatch):
    _server(monkeypatch, bundle=_bundle([]))
    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

    assert not (tmp_path / "semantic").exists()
    assert result.semantic_models_updated == 0
    assert result.semantic_models_removed == 0


def test_prunes_a_model_directory_no_longer_accessible(tmp_path, monkeypatch):
    _server(monkeypatch, bundle=_bundle([_model_row()]))
    run_pull(server_url="http://x", token="t", workspace=tmp_path)
    assert (tmp_path / "semantic" / "retail").exists()

    _server(monkeypatch, bundle=_bundle([]))
    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

    assert not (tmp_path / "semantic" / "retail").exists()
    assert result.semantic_models_removed == 1


def test_prunes_a_metric_dropped_from_an_otherwise_still_live_model(tmp_path, monkeypatch):
    _server(monkeypatch, bundle=_bundle([_model_row()]))
    run_pull(server_url="http://x", token="t", workspace=tmp_path)
    assert (tmp_path / "semantic" / "retail" / "metrics" / "revenue.yml").exists()

    row = _model_row()
    row["document_json"]["semantic_model"][0]["metrics"] = []
    _server(monkeypatch, bundle=_bundle([row]))
    run_pull(server_url="http://x", token="t", workspace=tmp_path)

    assert not (tmp_path / "semantic" / "retail" / "metrics" / "revenue.yml").exists()
    assert (tmp_path / "semantic" / "retail" / "_brief.md").exists()


def test_a_content_hash_change_re_renders_the_file(tmp_path, monkeypatch):
    _server(monkeypatch, bundle=_bundle([_model_row(content_hash="h1")]))
    run_pull(server_url="http://x", token="t", workspace=tmp_path)

    _server(monkeypatch, bundle=_bundle([_model_row(content_hash="h2")]))
    run_pull(server_url="http://x", token="t", workspace=tmp_path)

    text = (tmp_path / "semantic" / "retail" / "tables" / "orders.yml").read_text()
    assert "content_hash: h2" in text


def test_pre_this_feature_server_404_leaves_existing_cache_untouched(tmp_path, monkeypatch):
    _server(monkeypatch, bundle=_bundle([_model_row()]))
    run_pull(server_url="http://x", token="t", workspace=tmp_path)
    assert (tmp_path / "semantic" / "retail" / "_brief.md").exists()

    _server(monkeypatch, bundle_status=404)
    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

    assert (tmp_path / "semantic" / "retail" / "_brief.md").exists()
    assert result.semantic_models_updated == 0
    assert result.semantic_models_removed == 0
    assert result.errors == []


def test_bundle_fetch_error_is_recorded_and_does_not_abort_the_pull(tmp_path, monkeypatch):
    def _api_get(path, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = lambda: None
        if path == "/api/sync/manifest":
            resp.json.return_value = {"tables": {}}
        elif path == "/api/memory/bundle":
            resp.json.return_value = {"mandatory": [], "approved": []}
        elif path == "/api/semantic-models/bundle":
            raise RuntimeError("boom: semantic bundle fetch failed")
        else:
            resp.json.return_value = {}
        return resp

    monkeypatch.setattr("cli.lib.pull.api_get", _api_get, raising=False)
    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

    assert any(e.get("stage") == "semantic_cache" for e in result.errors)
    # A best-effort failure must not prevent the rest of the pull from
    # completing — the DuckDB file is still the load-bearing artifact.
    assert (tmp_path / "user" / "duckdb" / "analytics.duckdb").exists()


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    _server(monkeypatch, bundle=_bundle([_model_row()]))
    result = run_pull(server_url="http://x", token="t", workspace=tmp_path, dry_run=True)

    assert not (tmp_path / "semantic").exists()
    assert result.semantic_models_updated == 0
