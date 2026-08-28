"""The BigQuery project must be reachable from the admin surfaces.

``bq_fqn`` lets a registry row live in a project other than the configured
``data_source.bigquery.project``, and the query/scan paths honor it. Two admin
surfaces still hid it:

* **Server config.** ``project`` and ``location`` were absent from
  ``_KNOWN_FIELDS``, so the admin form filed them under "Other (YAML-only)
  keys" with no label, type or hint, while their siblings
  (``billing_project``, the byte caps) were documented.
* **Table registration.** The "Live from BigQuery" form asked only for
  dataset and source table, so composing a cross-project ``bq_fqn`` meant
  calling the API by hand.
"""

from __future__ import annotations

import pathlib

import pytest

TEMPLATE = pathlib.Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_tables.html"

# D4 — one registration flow: the "Live from BigQuery" register form (and
# its project-override field) moved out of admin_tables.html's own markup
# into the shared drawer partial, rendered generically across all four
# connectors and populated by register_table_form.js.
REGISTER_PARTIAL = (
    pathlib.Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "_register_table_form.html"
)
REGISTER_JS = pathlib.Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "js" / "register_table_form.js"


@pytest.fixture(scope="module")
def template_source() -> str:
    return TEMPLATE.read_text()


@pytest.fixture(scope="module")
def register_partial_source() -> str:
    return REGISTER_PARTIAL.read_text()


@pytest.fixture(scope="module")
def register_js_source() -> str:
    return REGISTER_JS.read_text()


class TestServerConfigKnownFields:
    def _bq_fields(self) -> dict:
        from app.api.admin import _KNOWN_FIELDS

        return _KNOWN_FIELDS["data_source"]["bigquery"]["fields"]

    def test_project_is_documented(self):
        fields = self._bq_fields()
        assert "project" in fields, "project must not hide under YAML-only keys"
        assert fields["project"]["kind"] == "string"
        assert fields["project"].get("hint")

    def test_location_is_documented(self):
        fields = self._bq_fields()
        assert "location" in fields
        assert fields["location"]["kind"] == "string"
        assert fields["location"].get("hint")

    def test_project_hint_mentions_the_cross_project_escape_hatch(self):
        """An operator reading the project field should learn that a table
        elsewhere is registered per-row, not by changing this global."""
        hint = self._bq_fields()["project"]["hint"]
        assert "bq_fqn" in hint

    def test_existing_bigquery_fields_are_untouched(self):
        fields = self._bq_fields()
        for name in (
            "billing_project",
            "bq_max_scan_bytes",
            "max_bytes_per_materialize",
            "query_timeout_ms",
        ):
            assert name in fields


class TestRegistrationFormExposesProject:
    """D4 — one registration flow: the "Live from BigQuery" form (and its
    project-override field) moved out of admin_tables.html's own markup
    into the shared drawer, rendered generically — `#rtfProject`, not a
    BQ-only `#bqProject` clone — and populated by
    `CONNECTORS.bigquery.buildPayload` (register_table_form.js)."""

    def test_form_has_a_project_input(self, register_partial_source):
        assert 'id="rtfProject"' in register_partial_source

    def test_payload_builder_reads_the_project_input(self, register_js_source):
        assert "getElementById('rtfProject')" in register_js_source
        # It must reach the payload, not just exist as a stray input.
        assert "bq_fqn" in register_js_source

    def test_project_input_is_marked_optional(self, register_partial_source):
        """Leaving it blank must keep the legacy dataset+table behaviour, so
        the field has to read as optional rather than required."""
        idx = register_partial_source.index('id="rtfProject"')
        # The "(optional)" marker sits in the <label>, which precedes the
        # <input>, so look both ways around the field.
        window = register_partial_source[max(0, idx - 400) : idx + 900]
        assert "required" not in window
        assert "optional" in window.lower()
