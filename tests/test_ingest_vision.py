"""Tests for the gated vision fallback + runner image routing."""

from __future__ import annotations


def test_media_type_for():
    from src.ingest.vision import media_type_for

    assert media_type_for("PNG") == "image/png"
    assert media_type_for("jpg") == "image/jpeg"
    assert media_type_for(".jpeg") == "image/jpeg"
    assert media_type_for("dwg") is None


def test_vision_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    from src.ingest.vision import extract_image_text, vision_available

    assert vision_available() is False
    assert extract_image_text("/nope.png", ext="png") is None


def _img_file(corpus_slug, tmp_path):
    from src.repositories import corpus_files_repo, file_corpora_repo

    cid = file_corpora_repo().create(name=corpus_slug, slug=corpus_slug, description=None, created_by="u")
    img = tmp_path / "p.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n fake")
    fid = corpus_files_repo().add(
        corpus_id=cid,
        filename="p.png",
        sha256="s",
        file_type="png",
        size_bytes=1,
        storage_path=str(img),
    )
    return cid, fid


def test_runner_indexes_image_when_vision_returns_text(e2e_env, tmp_path, monkeypatch):
    from src.ingest import vision

    monkeypatch.setattr(vision, "extract_image_text", lambda path, *, ext: "transcribed text from the scan")
    from src.ingest.runner import ingest_file
    from src.repositories import corpus_chunks_repo, corpus_files_repo

    _cid, fid = _img_file("v-on", tmp_path)
    assert ingest_file(fid) == "indexed"
    detail = corpus_files_repo().get(fid)["processing_detail"]
    assert detail["vision_used"] is True
    assert detail["tier"] == 2
    assert len(corpus_chunks_repo().list_for_file(fid)) >= 1


def test_runner_leaves_image_pending_without_vision(e2e_env, tmp_path, monkeypatch):
    from src.ingest import vision

    monkeypatch.setattr(vision, "extract_image_text", lambda path, *, ext: None)
    from src.ingest.runner import ingest_file
    from src.repositories import corpus_files_repo

    _cid, fid = _img_file("v-off", tmp_path)
    assert ingest_file(fid) == "pending"
    assert corpus_files_repo().get(fid)["processing_status"] == "pending"


def test_vision_builds_correct_request_and_parses(monkeypatch, tmp_path):
    """With a key + SDK present, extract_image_text sends a proper multimodal
    request (base64 image block + media_type + model) and parses the reply.

    Real Claude-vision output quality needs a live key (not exercised here);
    this locks the request/response WIRING."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from PIL import Image

    img = tmp_path / "scan.png"
    Image.new("RGB", (80, 40), (255, 255, 255)).save(img)

    captured = {}

    class _Block:
        type = "text"
        text = "Revenue grew strongly in the EU region."

    class _Resp:
        content = [_Block()]

    class _Messages:
        def create(self, **kwargs):
            captured.update(kwargs)
            return _Resp()

    class _Client:
        def __init__(self, api_key=None):
            self.messages = _Messages()

    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key=None: _Client())

    from src.ingest.vision import extract_image_text

    text = extract_image_text(str(img), ext="png")
    assert text == "Revenue grew strongly in the EU region."
    assert captured.get("model")
    content = captured["messages"][0]["content"]
    img_blocks = [b for b in content if b.get("type") == "image"]
    assert img_blocks, "no image block in the request"
    src = img_blocks[0]["source"]
    assert src["type"] == "base64"
    assert src["media_type"] == "image/png"
    assert src["data"]  # non-empty base64
    assert any(b.get("type") == "text" for b in content)


def test_vision_available_with_vertex_only(monkeypatch):
    """No static key, but the instance runs on Vertex → vision is available."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setattr("connectors.llm.factory.vertex_config_or_none", lambda: ("p", "global"))
    from src.ingest.vision import vision_available

    assert vision_available() is True


def test_extract_image_text_vertex_path(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setattr("connectors.llm.factory.vertex_config_or_none", lambda: ("proj-1", "global"))
    captured = {}

    class _Msgs:
        def create(self, **kw):
            captured.update(kw)
            return type("R", (), {"content": [type("B", (), {"type": "text", "text": "hello table"})()]})()

    class _FakeVertex:
        def __init__(self, **kw):
            captured["ctor"] = kw
            self.messages = _Msgs()

    import anthropic

    monkeypatch.setattr(anthropic, "AnthropicVertex", _FakeVertex, raising=False)
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG fake")
    from src.ingest.vision import extract_image_text

    out = extract_image_text(str(img), ext="png")
    assert out == "hello table"
    assert captured["ctor"]["project_id"] == "proj-1"
    assert "@" in captured["model"]  # vertex id form
