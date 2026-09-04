"""Tests for the corporate_memory governance section in /admin/server-config.

corporate_memory is the deepest-nested schema in instance.yaml — the
canonical reference is `config/instance.yaml.example` lines 224-317.
The whole section is optional; when omitted the system runs in legacy
democratic-wiki mode with no admin review. The registry must still
expose the full schema so admins can opt in via the editor without
hand-editing YAML.

Coverage:
- editable section + registry exposure
- top-level scalar fields (distribution_mode, approval_mode, …)
- 4-level nested object access (sources.session_transcripts.detection_types)
- map shapes with dotted-string data keys (confidence.base)
- POST flow merges nested edits into the on-disk YAML.
"""


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_corporate_memory_in_editable_sections(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/api/admin/server-config", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert "corporate_memory" in body["editable_sections"]


def test_corp_memory_top_level_fields_present(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/api/admin/server-config", headers=_auth(token))
    fields = r.json()["known_fields"]["corporate_memory"]
    for k in [
        "distribution_mode",
        "approval_mode",
        "auto_publish_min_confidence",
        "review_period_months",
        "notify_on_new_items",
    ]:
        assert k in fields, f"missing top-level field {k!r}"
    assert fields["distribution_mode"]["kind"] == "select"
    assert "hybrid" in fields["distribution_mode"]["options"]
    assert fields["distribution_mode"]["default"] == "hybrid"
    assert fields["approval_mode"]["kind"] == "select"
    assert "review_queue" in fields["approval_mode"]["options"]
    assert "threshold" in fields["approval_mode"]["options"]
    # #1573: approval_mode="threshold" needs a cutoff to compare confidence
    # against — this is the knob that makes it actually differ from
    # review_queue (see services/corporate_memory/governance.py).
    assert fields["auto_publish_min_confidence"]["kind"] == "float"
    assert fields["auto_publish_min_confidence"]["default"] == 0.80
    assert fields["review_period_months"]["kind"] == "int"
    assert fields["review_period_months"]["default"] == 6
    assert fields["notify_on_new_items"]["kind"] == "bool"
    assert fields["notify_on_new_items"]["default"] is True


def test_corp_memory_nested_sources_session_transcripts_detection_types(seeded_app):
    """Deep schema: sources.session_transcripts.detection_types is an
    array of strings. The registry must navigate object → object → array."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/api/admin/server-config", headers=_auth(token))
    fields = r.json()["known_fields"]["corporate_memory"]
    sources = fields["sources"]
    assert sources["kind"] == "object"
    sess = sources["fields"]["session_transcripts"]
    assert sess["kind"] == "object"
    dt = sess["fields"]["detection_types"]
    assert dt["kind"] == "array"
    assert dt["item_kind"] == "string"
    assert "correction" in dt["default"]
    assert "confirmation" in dt["default"]
    assert "unprompted_definition" in dt["default"]


def test_corp_memory_session_transcripts_enabled_and_detection_types_are_live(seeded_app):
    """#1957 interim hotfix: enabled + detection_types are now read on every
    verification-processor run — the schema hint must say so (not "restart"),
    so an admin doesn't bounce the server for nothing."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/api/admin/server-config", headers=_auth(token))
    sess = r.json()["known_fields"]["corporate_memory"]["sources"]["fields"]["session_transcripts"]
    assert sess["fields"]["enabled"]["kind"] == "bool"
    assert sess["fields"]["enabled"]["default"] is True
    assert "Live" in sess["fields"]["enabled"]["hint"]
    assert "Live" in sess["fields"]["detection_types"]["hint"]


def test_corp_memory_session_transcripts_confidence_base_removed_max_turns_now_live(seeded_app):
    """issue #1971 Part 6: confidence_base was a dead duplicate of
    confidence.base['user_verification.<detection_type>'] and never read —
    removed rather than left as a misleading knob. max_turns_per_session
    WAS wired in the same pass — its hint must say "Live", not "#1971"."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/api/admin/server-config", headers=_auth(token))
    sess = r.json()["known_fields"]["corporate_memory"]["sources"]["fields"]["session_transcripts"]
    assert "confidence_base" not in sess["fields"]
    assert "Live" in sess["fields"]["max_turns_per_session"]["hint"]


def test_corp_memory_claude_local_md_now_live(seeded_app):
    """issue #1971 Part 6: sources.claude_local_md.enabled was documented
    but never read — now wired (a False value skips the whole collector
    run). confidence_base was a dead duplicate, removed."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/api/admin/server-config", headers=_auth(token))
    claude_local_md = r.json()["known_fields"]["corporate_memory"]["sources"]["fields"]["claude_local_md"]
    assert "confidence_base" not in claude_local_md["fields"]
    assert "Live" in claude_local_md["hint"]
    assert "Live" in claude_local_md["fields"]["enabled"]["hint"]


def test_corp_memory_distribution_mode_hint_describes_real_enforcement(seeded_app):
    """The hint used to say distribution_mode was NOT YET ENFORCED (#1573).
    It has been enforced since select_distributable_items landed — the hint
    must describe the real three-mode behavior, not the stale caveat."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/api/admin/server-config", headers=_auth(token))
    hint = r.json()["known_fields"]["corporate_memory"]["distribution_mode"]["hint"]
    assert "NOT YET ENFORCED" not in hint
    assert "select_distributable_items" in hint


def test_corp_memory_review_period_and_entity_resolution_still_marked_not_wired(seeded_app):
    """The remaining known-inert corporate_memory knobs (#1971) must say so
    rather than silently implying they already take effect."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/api/admin/server-config", headers=_auth(token))
    fields = r.json()["known_fields"]["corporate_memory"]
    assert "#1971" in fields["review_period_months"]["hint"]
    assert "#1971" in fields["entity_resolution"]["hint"]
    assert "#1971" in fields["extraction"]["fields"]["contradiction_check"]["hint"]
    assert "#1971" in fields["contradiction_detection"]["hint"]


def test_corp_memory_extraction_model_and_sensitivity_check_now_live(seeded_app):
    """issue #1971 Part 6: extraction.model (a per-feature override of the
    global ai.model, mirroring extraction.facts.model) and
    extraction.sensitivity_check are now read; extraction.contradiction_check
    stays unwired (see the dedicated 'still not wired' test above)."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/api/admin/server-config", headers=_auth(token))
    extraction = r.json()["known_fields"]["corporate_memory"]["extraction"]
    assert extraction["kind"] == "object"
    assert "model" in extraction["fields"]
    assert "Live" in extraction["fields"]["model"]["hint"]
    assert "sensitivity_check" in extraction["fields"]
    assert extraction["fields"]["sensitivity_check"]["kind"] == "bool"
    assert extraction["fields"]["sensitivity_check"]["default"] is True
    assert "Live" in extraction["fields"]["sensitivity_check"]["hint"]


def test_corp_memory_confidence_base_is_map_of_floats(seeded_app):
    """confidence.base is a map<string, float>. Keys preserve dotted
    namespace (data, not path) — e.g. user_verification.correction is
    one map key, not two nested levels."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/api/admin/server-config", headers=_auth(token))
    fields = r.json()["known_fields"]["corporate_memory"]
    conf_base = fields["confidence"]["fields"]["base"]
    assert conf_base["kind"] == "map"
    assert conf_base["key_kind"] == "string"
    assert conf_base["value_kind"] == "float"
    # Dotted keys preserved as data keys (not path):
    assert "user_verification.correction" in conf_base["default"]
    assert conf_base["default"]["user_verification.correction"] == 0.90
    assert conf_base["default"]["admin_mandate"] == 1.00


def test_corp_memory_confidence_decay_is_4_level_nested(seeded_app):
    """confidence.decay.floor goes object → object → object → map.
    Pins down the renderer's ability to drill 4 levels deep."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/api/admin/server-config", headers=_auth(token))
    fields = r.json()["known_fields"]["corporate_memory"]
    decay = fields["confidence"]["fields"]["decay"]
    assert decay["kind"] == "object"
    assert decay["fields"]["mode"]["kind"] == "select"
    assert "exponential" in decay["fields"]["mode"]["options"]
    assert "linear" in decay["fields"]["mode"]["options"]
    assert decay["fields"]["mode"]["default"] == "exponential"
    floor = decay["fields"]["floor"]
    assert floor["kind"] == "map"
    assert floor["value_kind"] == "float"
    assert floor["default"]["admin_mandate"] == 0.50


def test_corp_memory_entity_resolution_map_of_arrays(seeded_app):
    """entity_resolution.entities is a map<string, array<string>>."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/api/admin/server-config", headers=_auth(token))
    fields = r.json()["known_fields"]["corporate_memory"]
    er = fields["entity_resolution"]
    assert er["kind"] == "object"
    entities = er["fields"]["entities"]
    assert entities["kind"] == "map"
    assert entities["value_kind"] == "array"
    assert entities["value_item_kind"] == "string"
    assert "MRR" in entities["default"]["metrics"]


def test_corp_memory_domain_owners_map_of_email_arrays(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/api/admin/server-config", headers=_auth(token))
    fields = r.json()["known_fields"]["corporate_memory"]
    do = fields["domain_owners"]
    assert do["kind"] == "map"
    assert do["value_kind"] == "array"
    assert do["value_item_kind"] == "string"


def test_corp_memory_domains_array_of_strings(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/api/admin/server-config", headers=_auth(token))
    fields = r.json()["known_fields"]["corporate_memory"]
    domains = fields["domains"]
    assert domains["kind"] == "array"
    assert domains["item_kind"] == "string"
    assert "finance" in domains["default"]
    assert "engineering" in domains["default"]


def test_corp_memory_section_renders_in_html(seeded_app, monkeypatch, tmp_path):
    """SECTION_META must include corporate_memory so the section header
    has a friendly title + help instead of falling back to the raw key."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    import yaml as _yaml

    (state / "instance.yaml").write_text(
        _yaml.dump(
            {
                "data_source": {"type": "bigquery", "bigquery": {"project": "p"}},
            }
        )
    )
    import app.instance_config as ic

    ic._instance_config = None
    try:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        c.cookies.set("access_token", token)
        try:
            r = c.get("/admin/server-config", headers={"Accept": "text/html"})
        finally:
            c.cookies.clear()
        assert r.status_code == 200, r.text
        body = r.text
        # SECTION_META entry — title is operator-friendly.
        assert "corporate_memory" in body, "section name not exposed in template JS"
        assert "Corporate memory" in body, "SECTION_META entry missing"
    finally:
        ic._instance_config = None


def test_post_corp_memory_section_persists(seeded_app, monkeypatch, tmp_path):
    """POST corporate_memory section: distribution_mode + nested model +
    array of domains all merge into instance.yaml."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    import app.instance_config as ic

    ic._instance_config = None
    try:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        r = c.post(
            "/api/admin/server-config",
            headers=_auth(token),
            json={
                "sections": {
                    "corporate_memory": {
                        "distribution_mode": "admin_curated",
                        "review_period_months": 12,
                        "extraction": {"model": "claude-sonnet-4-6"},
                        "domains": ["finance", "engineering", "product"],
                    },
                },
            },
        )
        assert r.status_code in (200, 204), r.text
        # Re-read from disk to verify persistence + merge.
        import yaml as _yaml

        loaded = _yaml.safe_load((state / "instance.yaml").read_text())
        cm = loaded.get("corporate_memory", {})
        assert cm.get("distribution_mode") == "admin_curated"
        assert cm.get("review_period_months") == 12
        assert cm.get("extraction", {}).get("model") == "claude-sonnet-4-6"
        assert cm.get("domains") == ["finance", "engineering", "product"]
    finally:
        ic._instance_config = None


def test_post_corp_memory_with_dotted_map_keys_persists(seeded_app, monkeypatch, tmp_path):
    """The renderer's data-path encoding must let an admin save a
    confidence.base entry whose KEY contains a literal dot (e.g.
    user_verification.correction). Server-side this is just a dict; we
    verify by POSTing the patch directly and reading it back."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    import app.instance_config as ic

    ic._instance_config = None
    try:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        r = c.post(
            "/api/admin/server-config",
            headers=_auth(token),
            json={
                "sections": {
                    "corporate_memory": {
                        "confidence": {
                            "base": {
                                "user_verification.correction": 0.95,
                                "admin_mandate": 1.0,
                            },
                        },
                    },
                },
            },
        )
        assert r.status_code in (200, 204), r.text
        import yaml as _yaml

        loaded = _yaml.safe_load((state / "instance.yaml").read_text())
        base = loaded["corporate_memory"]["confidence"]["base"]
        # Dotted key survives literally — not split into nested objects.
        assert base["user_verification.correction"] == 0.95
        assert base["admin_mandate"] == 1.0
    finally:
        ic._instance_config = None
