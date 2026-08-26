"""Unit tests for the marketplace-metadata.json parser.

Covers the lenient-parse contract (missing file / malformed JSON / wrong-type
top level all degrade to empty) plus the per-plugin / per-skill resolution
that produces dicts ready for the DB write layer.
"""

from __future__ import annotations

import json

from src.marketplace_asset_validation import DocLinkRef, parse_doc_link
from src.marketplace_metadata import (
    collect_all_external_urls,
    collect_external_urls,
    get_inner_section,
    get_plugin_section,
    read_marketplace_metadata,
    resolve_inner_metadata,
    resolve_plugin_metadata,
)


def _write_metadata(repo_root, payload):
    """Write payload as `.claude-plugin/marketplace-metadata.json` under repo_root."""
    target = repo_root / ".claude-plugin" / "marketplace-metadata.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


# --- read_marketplace_metadata --------------------------------------------------


def test_read_marketplace_metadata_missing_file_returns_empty(tmp_path):
    """No metadata file → empty dict, no warning crash."""
    assert read_marketplace_metadata(tmp_path) == {}


def test_read_marketplace_metadata_malformed_json(tmp_path):
    """Malformed JSON degrades to empty dict (sync should not abort)."""
    target = tmp_path / ".claude-plugin" / "marketplace-metadata.json"
    target.parent.mkdir(parents=True)
    target.write_text("{not json at all", encoding="utf-8")
    assert read_marketplace_metadata(tmp_path) == {}


def test_read_marketplace_metadata_top_level_array_rejected(tmp_path):
    """Top-level must be a JSON object — array is logged + ignored."""
    target = tmp_path / ".claude-plugin" / "marketplace-metadata.json"
    target.parent.mkdir(parents=True)
    target.write_text("[1, 2, 3]", encoding="utf-8")
    assert read_marketplace_metadata(tmp_path) == {}


def test_read_marketplace_metadata_happy_path(tmp_path):
    payload = {"version": 1, "plugins": {"x": {"category": "Tools"}}}
    _write_metadata(tmp_path, payload)
    assert read_marketplace_metadata(tmp_path) == payload


def test_read_marketplace_metadata_oversized_file_returns_empty(tmp_path):
    """Curator-controlled file > 1 MB cap is refused without reading the body.

    Defends against a misbehaving (or hostile) curator committing a multi-GB
    JSON that would OOM the sync worker (PR #234 review #9).
    """
    from src.marketplace_metadata import MARKETPLACE_METADATA_MAX_BYTES

    target = tmp_path / ".claude-plugin" / "marketplace-metadata.json"
    target.parent.mkdir(parents=True)
    # Write valid JSON padded with whitespace to exceed the cap. The test
    # doesn't have to allocate a real GB — anything > MARKETPLACE_METADATA_MAX_BYTES
    # demonstrates the size gate fires before json.loads.
    padding = " " * (MARKETPLACE_METADATA_MAX_BYTES + 1024)
    target.write_text("{" + padding + "}", encoding="utf-8")

    assert read_marketplace_metadata(tmp_path) == {}


def test_read_marketplace_metadata_deeply_nested_does_not_crash(tmp_path):
    """A deeply-nested JSON that fits under the size cap must not crash sync.

    Even if json.loads raises ``RecursionError`` instead of ``ValueError``
    (cpython's recursive object/array parser hits this at ~1000 levels of
    depth), the sync stays alive and the marketplace just gets empty
    metadata. The previous code only caught ``ValueError`` so this would
    have aborted the whole sync.
    """
    target = tmp_path / ".claude-plugin" / "marketplace-metadata.json"
    target.parent.mkdir(parents=True)
    # 5000 nested arrays — comfortably past cpython's default recursion
    # limit (~1000) but far below the 1 MB size cap (~10 KB).
    depth = 5000
    target.write_text("[" * depth + "]" * depth, encoding="utf-8")

    # Function MUST return cleanly — either {} (parser blew up and we
    # caught it) or whatever the parser produced. Either way, no crash.
    result = read_marketplace_metadata(tmp_path)
    assert isinstance(result, dict)


# --- get_plugin_section / get_inner_section -------------------------------


def test_get_plugin_section_missing_returns_empty():
    assert get_plugin_section({}, "x") == {}
    assert get_plugin_section({"plugins": "wrong-type"}, "x") == {}
    assert get_plugin_section({"plugins": {"y": {}}}, "x") == {}


def test_get_inner_section_only_skills_or_agents():
    md = {
        "plugins": {
            "x": {
                "skills": {"foo": {"video_url": "https://y.com/v"}},
                "agents": {"bar": {"video_url": "https://y.com/a"}},
            },
        },
    }
    assert get_inner_section(md, "x", "skills", "foo") == {
        "video_url": "https://y.com/v"
    }
    assert get_inner_section(md, "x", "agents", "bar") == {
        "video_url": "https://y.com/a"
    }
    # Wrong kind / missing inner / wrong type all degrade to empty.
    assert get_inner_section(md, "x", "commands", "foo") == {}
    assert get_inner_section(md, "x", "skills", "missing") == {}
    assert get_inner_section({"plugins": {"x": "scalar"}}, "x", "skills", "f") == {}


# --- resolve_plugin_metadata ---------------------------------------------


def test_resolve_plugin_metadata_full_shape():
    md = {
        "plugins": {
            "p": {
                "cover_photo": ".agnes/cover.png",
                "video_url": "https://www.youtube.com/watch?v=abc",
                "category": "Productivity",
                "doc_links": [
                    {"name": "Setup", "path": "docs/setup.md"},
                    {"name": "API",   "url": "https://example.com/api.pdf"},
                ],
            }
        }
    }
    out = resolve_plugin_metadata(md, "p")
    assert out["cover_photo_ref"] == ("internal", ".agnes/cover.png")
    assert out["video_url"] == "https://www.youtube.com/watch?v=abc"
    assert out["category"] == "Productivity"
    assert len(out["doc_links"]) == 2
    assert out["doc_links"][0] == DocLinkRef(
        name="Setup", kind="internal", path="docs/setup.md"
    )
    assert out["doc_links"][1] == DocLinkRef(
        name="API", kind="external", url="https://example.com/api.pdf"
    )


def test_resolve_plugin_metadata_external_cover():
    md = {"plugins": {"p": {"cover_photo": "https://cdn.example.com/x.png"}}}
    out = resolve_plugin_metadata(md, "p")
    assert out["cover_photo_ref"] == ("external", "https://cdn.example.com/x.png")


def test_resolve_plugin_metadata_traversal_rejected():
    """Internal cover_photo with `..` is dropped at parse time."""
    md = {"plugins": {"p": {"cover_photo": "../etc/passwd"}}}
    out = resolve_plugin_metadata(md, "p")
    assert "cover_photo_ref" not in out


def test_resolve_plugin_metadata_video_must_be_http():
    """Non-http video_url is dropped silently with a warning."""
    md = {"plugins": {"p": {"video_url": "ftp://x/y"}}}
    out = resolve_plugin_metadata(md, "p")
    assert "video_url" not in out


def test_resolve_plugin_metadata_invalid_doc_links_dropped():
    """Each invalid doc_link entry is dropped; valid siblings survive."""
    md = {
        "plugins": {
            "p": {
                "doc_links": [
                    {"name": "ok", "url": "https://x.com/a.pdf"},
                    {"name": "missing-target"},
                    {"name": "both-set", "path": "x", "url": "https://y.com/y"},
                    "scalar-not-object",
                ]
            }
        }
    }
    out = resolve_plugin_metadata(md, "p")
    assert len(out["doc_links"]) == 1
    assert out["doc_links"][0].name == "ok"


def test_resolve_plugin_metadata_missing_plugin_returns_empty():
    assert resolve_plugin_metadata({"plugins": {}}, "p") == {}


# --- resolve_inner_metadata ----------------------------------------------


def test_resolve_inner_metadata_skill():
    md = {
        "plugins": {
            "p": {
                "skills": {
                    "s": {
                        "cover_photo": ".agnes/skills/s.png",
                        "video_url": "https://vimeo.com/123",
                        "doc_links": [{"name": "Howto", "path": "docs/s.md"}],
                    }
                }
            }
        }
    }
    out = resolve_inner_metadata(md, "p", "skills", "s")
    assert out["cover_photo_ref"] == ("internal", ".agnes/skills/s.png")
    assert out["video_url"] == "https://vimeo.com/123"
    assert len(out["doc_links"]) == 1


def test_resolve_inner_metadata_missing_returns_empty():
    assert resolve_inner_metadata({}, "p", "skills", "x") == {}


# --- collect_external_urls ------------------------------------------------


def test_collect_external_urls_includes_cover_and_external_docs():
    """Internal references skip the mirror; external cover + external doc URLs
    both get queued. Tagging by kind lets the mirror cache them in different
    sub-directories (cover vs docs)."""
    resolved = {
        "cover_photo_ref": ("external", "https://cdn.example.com/cover.png"),
        "doc_links": [
            DocLinkRef(name="internal", kind="internal", path="docs/x.md"),
            DocLinkRef(name="external", kind="external", url="https://e.com/d.pdf"),
        ],
    }
    urls = collect_external_urls(resolved)
    assert ("cover", "https://cdn.example.com/cover.png") in urls
    assert ("doc", "https://e.com/d.pdf") in urls
    assert len(urls) == 2


def test_collect_external_urls_internal_cover_only():
    """Internal cover and zero external doc_links → empty fetch list."""
    resolved = {
        "cover_photo_ref": ("internal", ".agnes/cover.png"),
        "doc_links": [],
    }
    assert collect_external_urls(resolved) == []


# --- collect_all_external_urls (walks plugin + skills + agents) -----------


def test_collect_all_external_urls_walks_full_tree():
    """v32 inner-level mirror: plugin + every skill + every agent's external
    URLs all end up in the fetch list, so the asset-mirror cache covers
    inner-detail look-ups too. Internal references stay out of the list."""
    md = {
        "plugins": {
            "demo": {
                "cover_photo": "https://cdn.example.com/plugin-cover.png",
                "doc_links": [
                    {"name": "external", "url": "https://e.com/d.pdf"},
                    {"name": "internal", "path": "docs/setup.md"},
                ],
                "skills": {
                    "skill-a": {
                        "cover_photo": "https://cdn.example.com/skill-a-cover.png",
                        "doc_links": [
                            {"name": "ref", "url": "https://e.com/skill-a.md"},
                        ],
                    },
                    "skill-b": {
                        # internal-only — should NOT contribute external URLs
                        "cover_photo": ".agnes/skills/b.png",
                        "doc_links": [
                            {"name": "internal", "path": "docs/b.md"},
                        ],
                    },
                },
                "agents": {
                    "agent-x": {
                        "video_url": "https://www.youtube.com/watch?v=abc",
                        "doc_links": [
                            {"name": "agent-doc", "url": "https://e.com/x.pdf"},
                        ],
                    },
                },
            }
        }
    }
    urls = collect_all_external_urls(md, "demo")
    # Convert to set of urls only — ordering doesn't matter for the assertion
    found = {url for _kind, url in urls}
    assert "https://cdn.example.com/plugin-cover.png" in found     # plugin cover
    assert "https://e.com/d.pdf" in found                          # plugin doc
    assert "https://cdn.example.com/skill-a-cover.png" in found    # skill cover
    assert "https://e.com/skill-a.md" in found                     # skill doc
    assert "https://e.com/x.pdf" in found                          # agent doc
    # Internal references and `video_url` (never mirrored) are absent
    assert "docs/setup.md" not in found
    assert ".agnes/skills/b.png" not in found
    assert "https://www.youtube.com/watch?v=abc" not in found


def test_collect_all_external_urls_no_metadata_returns_empty():
    """When the plugin has no entry, no URLs collected (no crash)."""
    assert collect_all_external_urls({"plugins": {}}, "demo") == []
    assert collect_all_external_urls({}, "demo") == []


# --- parse_doc_link extension allowlist (added in post-walkthrough fix) ---


def test_parse_doc_link_internal_path_must_be_allowlist_extension():
    """Internal `path` ending in something other than .pdf/.md/.markdown/.txt
    is rejected at parse time so the entry never reaches the served list."""
    ok, reason = parse_doc_link({"name": "Doc", "path": "docs/x.docx"})
    assert ok is False
    assert "unsupported_extension" in reason

    # .md still accepted
    ok, value = parse_doc_link({"name": "Doc", "path": "docs/x.md"})
    assert ok is True
    assert value.kind == "internal"


def test_parse_doc_link_external_url_with_explicit_bad_extension():
    """External URL that explicitly ends in .docx etc. is rejected at parse
    time — saves the mirror an HTTP HEAD on something we'd never accept."""
    ok, reason = parse_doc_link({"name": "Doc", "url": "https://x.com/y.docx"})
    assert ok is False
    assert "unsupported_extension" in reason


def test_parse_doc_link_external_url_without_extension_passes_parse():
    """URL without a clear extension (CDN pretty paths) survives parse — the
    Content-Type check at fetch time is the deciding gate."""
    ok, value = parse_doc_link({"name": "Doc", "url": "https://x.com/api/getting-started"})
    assert ok is True
    assert value.kind == "external"


# --- Rich plugin-level fields (added 2026-05-12) -------------------------
#
# display_name, tagline, description, use_cases, sample_interaction are all
# optional. Curator-friendly fields render in the plugin detail hero and
# the dedicated "Use cases" / "Sample interaction" sections.


def test_resolve_plugin_metadata_extracts_rich_fields():
    """Happy path — all 5 rich fields survive parsing into the resolved dict."""
    metadata = {
        "plugins": {
            "my-plugin": {
                "display_name": "Architecture Intelligence",
                "tagline": "Stop reading code — ask Claude.",
                "description": "Para 1.\n\nPara 2 with **bold**.",
                "use_cases": [
                    {"title": "Find owner", "description": "Find owners + deps.", "prompt": "/my-plugin:query who owns X?"},
                ],
                "sample_interaction": {
                    "user": "What does X do?",
                    "assistant": "X is a service that...",
                },
            }
        }
    }
    resolved = resolve_plugin_metadata(metadata, "my-plugin")
    assert resolved["display_name"] == "Architecture Intelligence"
    assert resolved["tagline"] == "Stop reading code — ask Claude."
    assert resolved["description"].startswith("Para 1.")
    assert len(resolved["use_cases"]) == 1
    assert resolved["use_cases"][0] == {
        "title": "Find owner",
        "description": "Find owners + deps.",
        "prompt": "/my-plugin:query who owns X?",
    }
    assert resolved["sample_interaction"] == {
        "user": "What does X do?",
        "assistant": "X is a service that...",
    }


def test_resolve_plugin_metadata_missing_rich_fields_returns_empty_keys():
    """Plugin with only `cover_photo` set — rich fields absent from output;
    the API layer treats absent keys as "use the fallback chain"."""
    metadata = {
        "plugins": {
            "minimal": {"cover_photo": ".example/x.png"},
        }
    }
    resolved = resolve_plugin_metadata(metadata, "minimal")
    assert "display_name" not in resolved
    assert "tagline" not in resolved
    assert "description" not in resolved
    assert "use_cases" not in resolved
    assert "sample_interaction" not in resolved


def test_resolve_plugin_metadata_use_cases_drops_invalid_entries():
    """Each use_case must carry non-empty title, description, prompt — bad
    entries are dropped with a warning; surviving entries preserve order."""
    metadata = {
        "plugins": {
            "p": {
                "use_cases": [
                    {"title": "Good", "description": "ok", "prompt": "do it"},
                    {"title": "Missing prompt", "description": "x"},
                    "not an object",
                    {"title": "", "description": "x", "prompt": "y"},  # empty title
                    {"title": "B", "description": "ok", "prompt": "p"},
                ]
            }
        }
    }
    resolved = resolve_plugin_metadata(metadata, "p")
    assert [uc["title"] for uc in resolved["use_cases"]] == ["Good", "B"]


def test_resolve_plugin_metadata_use_cases_non_list_is_dropped():
    """A use_cases value of the wrong type (object instead of array) gets
    dropped entirely with a warning. Curator typo shouldn't break parse."""
    metadata = {"plugins": {"p": {"use_cases": {"oops": "wrong shape"}}}}
    resolved = resolve_plugin_metadata(metadata, "p")
    assert "use_cases" not in resolved


def test_resolve_plugin_metadata_sample_interaction_requires_both_sides():
    """`sample_interaction` must carry both `user` AND `assistant`. Missing
    one half → drop the whole block; UI never renders half a dialog."""
    metadata_user_only = {
        "plugins": {"p": {"sample_interaction": {"user": "Q"}}}
    }
    assert "sample_interaction" not in resolve_plugin_metadata(metadata_user_only, "p")

    metadata_assistant_only = {
        "plugins": {"p": {"sample_interaction": {"assistant": "A"}}}
    }
    assert "sample_interaction" not in resolve_plugin_metadata(metadata_assistant_only, "p")

    metadata_empty_strings = {
        "plugins": {"p": {"sample_interaction": {"user": "  ", "assistant": "A"}}}
    }
    assert "sample_interaction" not in resolve_plugin_metadata(metadata_empty_strings, "p")


def test_resolve_plugin_metadata_strips_whitespace_in_strings():
    """display_name / tagline are single-line — strip leading/trailing
    whitespace. description preserves interior structure but trims edges."""
    metadata = {
        "plugins": {
            "p": {
                "display_name": "  Friendly  ",
                "tagline": "\n  Punchy line  \n",
                "description": "\n\nPara 1.\n\nPara 2.\n\n",
            }
        }
    }
    resolved = resolve_plugin_metadata(metadata, "p")
    assert resolved["display_name"] == "Friendly"
    assert resolved["tagline"] == "Punchy line"
    # description: leading newlines stripped, but interior newlines preserved
    # so the markdown renderer sees paragraph breaks.
    assert resolved["description"].startswith("Para 1.")
    assert "\n\nPara 2." in resolved["description"]


def test_resolve_plugin_metadata_wrong_type_field_logged_and_dropped():
    """A non-string display_name (curator typo: array instead of string)
    drops the field silently — UI falls back to manifest_name."""
    metadata = {"plugins": {"p": {"display_name": ["wrong", "type"]}}}
    resolved = resolve_plugin_metadata(metadata, "p")
    assert "display_name" not in resolved


# --- Rich skill / agent fields (added 2026-05-12) ------------------------
#
# Skill / agent level mirrors plugin-level: same 5 rich fields plus
# `invocation` (the literal slash/at command) and `when_to_use` (markdown
# disambiguation). Plus `category` for per-item override.


def test_resolve_inner_metadata_extracts_rich_fields():
    """Happy path — all skill-level rich fields survive parsing."""
    metadata = {
        "plugins": {
            "p": {
                "skills": {
                    "s": {
                        "display_name": "Confluence Search",
                        "tagline": "Find pages in the wiki.",
                        "category": "Documentation",
                        "description": "Para1.\n\nPara2.",
                        "invocation": "/p:s <your question>",
                        "when_to_use": "Use this for **Confluence only**.",
                        "use_cases": [
                            {"title": "T", "description": "D", "prompt": "P"},
                        ],
                        "sample_interaction": {"user": "Q", "assistant": "A"},
                    }
                }
            }
        }
    }
    resolved = resolve_inner_metadata(metadata, "p", "skills", "s")
    assert resolved["display_name"] == "Confluence Search"
    assert resolved["tagline"] == "Find pages in the wiki."
    assert resolved["category"] == "Documentation"
    assert resolved["invocation"] == "/p:s <your question>"
    assert resolved["when_to_use"].startswith("Use this for")
    assert len(resolved["use_cases"]) == 1
    assert resolved["sample_interaction"]["user"] == "Q"


def test_resolve_inner_metadata_missing_rich_fields_returns_empty_keys():
    """Skill with only cover_photo set — rich fields absent from output."""
    metadata = {
        "plugins": {
            "p": {"skills": {"s": {"cover_photo": ".agnes/s.png"}}}
        }
    }
    resolved = resolve_inner_metadata(metadata, "p", "skills", "s")
    for key in ("display_name", "tagline", "category", "description",
                "invocation", "when_to_use", "use_cases",
                "sample_interaction"):
        assert key not in resolved


def test_resolve_inner_metadata_per_item_category_set_separately():
    """Per-item `category` is part of the inner-section payload — used by
    the API layer to override the parent plugin's category badge. Regression
    test for the TypeError that surfaced when both parent_fields and the
    inner-enrichment returned `category` and they were unpacked into the
    same Pydantic constructor: explicit dict-merge needed (see
    app/api/marketplace.py curated_skill_detail)."""
    metadata = {
        "plugins": {
            "p": {
                "skills": {
                    "s": {"category": "Documentation"},
                }
            }
        }
    }
    resolved = resolve_inner_metadata(metadata, "p", "skills", "s")
    assert resolved["category"] == "Documentation"


def test_resolve_inner_metadata_agent_kind_works_identically():
    """Agents go through the same resolver path with `kind='agents'`."""
    metadata = {
        "plugins": {
            "p": {
                "agents": {
                    "a": {
                        "display_name": "CTO Architect",
                        "invocation": "@p:a",
                        "tagline": "Strategy decisions.",
                    }
                }
            }
        }
    }
    resolved = resolve_inner_metadata(metadata, "p", "agents", "a")
    assert resolved["display_name"] == "CTO Architect"
    assert resolved["invocation"] == "@p:a"
    assert resolved["tagline"] == "Strategy decisions."


# --- Per-field byte cap (CPU-burn defense) ----------------------------------


def test_validated_markdown_truncates_oversized_field():
    """Curator commits a field over the per-field cap (`MARKETPLACE_METADATA_FIELD_MAX_BYTES`)
    — resolver truncates rather than letting the renderer chew through 1 MiB on every
    request. Truncation must produce a valid UTF-8 string (drop trailing partial codepoint)."""
    from src.marketplace_metadata import (
        MARKETPLACE_METADATA_FIELD_MAX_BYTES,
        _validated_markdown,
    )
    # 2x the cap of one-byte chars + one multi-byte char straddling the cap.
    body = "a" * (MARKETPLACE_METADATA_FIELD_MAX_BYTES * 2)
    out = _validated_markdown(body, "description", "test")
    # Output is at most the cap (counting UTF-8 bytes) — typically much less
    # because we trim trailing whitespace too.
    assert len(out.encode("utf-8")) <= MARKETPLACE_METADATA_FIELD_MAX_BYTES


def test_validated_markdown_truncation_preserves_utf8_boundary():
    """Truncation must not split a multi-byte UTF-8 sequence."""
    from src.marketplace_metadata import (
        MARKETPLACE_METADATA_FIELD_MAX_BYTES,
        _validated_markdown,
    )
    # Build a body where the byte at position cap-1 is mid-character:
    # all-emoji content, each emoji = 4 bytes, so the cap likely lands inside one.
    emoji = "\U0001F600"  # 😀
    body = emoji * (MARKETPLACE_METADATA_FIELD_MAX_BYTES // 2)
    out = _validated_markdown(body, "description", "test")
    # Round-trip must succeed (no UnicodeDecodeError); resolver guarantees
    # valid UTF-8 by construction.
    assert out.encode("utf-8").decode("utf-8") == out


def test_validated_markdown_under_cap_passes_through_untouched():
    """The cap is a guard; small content is unchanged byte-for-byte."""
    from src.marketplace_metadata import _validated_markdown
    body = "## Title\n\nA paragraph with **bold** and `code`.\n"
    out = _validated_markdown(body, "description", "test")
    # Trailing whitespace stripped per existing contract; structure preserved.
    assert out == body.strip("\n").rstrip()


# --- Cache eviction stress (bounded LRU) ------------------------------------


def test_metadata_cache_bounded_lru_evicts_oldest(monkeypatch, tmp_path):
    """At >MAX entries the cache must drop oldest (FIFO of insert order via
    OrderedDict). Without bounded LRU, cache grows linearly with the number
    of marketplaces × historical mtimes — the silent-failure mode in the
    earlier per-marketplace eviction predicate."""
    from app.api import marketplace as mod
    from app.api.marketplace import _PLUGIN_METADATA_CACHE, _PLUGIN_METADATA_CACHE_MAX

    _PLUGIN_METADATA_CACHE.clear()
    monkeypatch.setattr(mod, "get_marketplaces_dir", lambda: str(tmp_path))

    # Seed cache with > MAX entries by simulating fresh marketplaces.
    # Each `_read_metadata_cached` call must add exactly one entry (since
    # mtime is unique per marketplace_id directory).
    for i in range(_PLUGIN_METADATA_CACHE_MAX + 50):
        mp_dir = tmp_path / f"marketplace-{i}"
        plugin_dir = mp_dir / ".claude-plugin"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "marketplace-metadata.json").write_text(
            '{"plugins": {}}', encoding="utf-8",
        )
        mod._read_metadata_cached(f"marketplace-{i}")

    assert len(_PLUGIN_METADATA_CACHE) == _PLUGIN_METADATA_CACHE_MAX, (
        f"cache must be bounded at {_PLUGIN_METADATA_CACHE_MAX}, "
        f"got {len(_PLUGIN_METADATA_CACHE)} — eviction is broken"
    )
    # Oldest-inserted entries (the first 50) should have been evicted.
    for i in range(50):
        assert all(
            k[0] != f"marketplace-{i}" for k in _PLUGIN_METADATA_CACHE
        ), f"marketplace-{i} should have been evicted"


# --- Truthy-vs-presence merge trap (resolver contract) ----------------------


def test_resolve_plugin_metadata_includes_explicit_empty_only_via_resolver_contract():
    """Resolver writes a key into ``out`` only when validation passes
    (truthy guard). API layer (``_curated_plugin_enrichment`` etc.) then
    propagates via presence check. Today's resolver drops empty-string
    `description: ""` (truthy guard inside `_validated_markdown` returns
    `""` → outer truthy guard skips). This test pins the contract so a
    future resolver-side change to keep empty-string descriptions doesn't
    silently corrupt the API output."""
    metadata = {"plugins": {"p": {"description": ""}}}
    resolved = resolve_plugin_metadata(metadata, "p")
    assert "description" not in resolved, (
        "empty string `description` must not reach `out` — resolver contract"
    )


# ---------------------------------------------------------------------------
# Curator-side deprecation (lifecycle fields)
# ---------------------------------------------------------------------------


def test_resolve_plugin_metadata_deprecated_fields():
    metadata = {
        "version": 1,
        "plugins": {
            "p": {
                "deprecated": True,
                "deprecation_note": "generates unreliable output",
                "replacement": "other-plugin",
            }
        },
    }
    out = resolve_plugin_metadata(metadata, "p")
    assert out["deprecated"] is True
    assert out["deprecation_note"] == "generates unreliable output"
    assert out["replacement"] == "other-plugin"


def test_resolve_plugin_metadata_deprecated_requires_literal_true():
    """Strict ``is True`` — a typo ("true", 1, [...]) must not retire a plugin,
    and note/replacement without the flag are ignored."""
    for bad in ("true", "yes", 1, [True], {"v": True}):
        out = resolve_plugin_metadata(
            {"version": 1, "plugins": {"p": {"deprecated": bad}}}, "p",
        )
        assert "deprecated" not in out, f"deprecated={bad!r} must be rejected"

    out = resolve_plugin_metadata(
        {"version": 1, "plugins": {"p": {"deprecation_note": "n", "replacement": "r"}}},
        "p",
    )
    assert "deprecated" not in out
    assert "deprecation_note" not in out
    assert "replacement" not in out


def test_resolve_plugin_metadata_deprecated_false_is_absent():
    out = resolve_plugin_metadata(
        {"version": 1, "plugins": {"p": {"deprecated": False}}}, "p",
    )
    assert "deprecated" not in out
