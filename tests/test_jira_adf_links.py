"""Tests for URL survival when Jira's ADF rich text is flattened to plain text.

``extract_text_from_adf`` feeds three stored columns — ``comments.body``,
``issues.description`` and ``issues.context`` — and used to walk ``text`` nodes
only. Two ADF constructs keep their target outside any text node, so both
vanished:

* a smart link (``inlineCard`` / ``blockCard`` / ``embedCard``) has NO text child
  at all, only ``attrs.url``. A comment whose whole body was one smart link
  stored as ``''`` — visible loss, at least;
* a ``text`` node with a ``link`` mark keeps its target in
  ``marks[].attrs.href``. The anchor text survived and the href did not, so the
  sentence still read as valid data with the URL gone. That is the common case
  and the dangerous one: nothing downstream could tell it had happened.

The fix inlines the URLs into the same text column — no new column, no schema
change — because the consumers of these columns are LLM analysis and full-text
search over the text itself, which is exactly what makes an inlined URL findable.

Whitespace is the other half of the contract, and it is asserted structurally
rather than case by case: every test here renders through ``_render``, which
checks that the renderer never manufactures a run of spaces the source text
nodes did not already contain. A fragment that renders to nothing must leave no
gap, while a ``codeBlock``'s newlines and indentation are content and survive
verbatim.
"""

import pytest

from connectors.jira.transform import extract_text_from_adf, transform_comments, transform_issue

CARD_TYPES = ("inlineCard", "blockCard", "embedCard")


def _doc(*content: dict) -> dict:
    return {"type": "doc", "version": 1, "content": list(content)}


def _para(*content: dict) -> dict:
    return {"type": "paragraph", "content": list(content)}


def _text(text: str, href: str | None = None) -> dict:
    node: dict = {"type": "text", "text": text}
    if href is not None:
        node["marks"] = [{"type": "link", "attrs": {"href": href}}]
    return node


def _card(url: str, node_type: str = "inlineCard") -> dict:
    return {"type": node_type, "attrs": {"url": url}}


def _source_text_nodes(node) -> list[str]:
    """Every ``text`` node string in a document, in order."""
    out: list[str] = []
    if isinstance(node, list):
        for item in node:
            out.extend(_source_text_nodes(item))
    elif isinstance(node, dict):
        if isinstance(node.get("text"), str):
            out.append(node["text"])
        if "content" in node:
            out.extend(_source_text_nodes(node["content"]))
    return out


def _render(doc) -> str:
    """Render ``doc``, asserting the renderer manufactured no whitespace run.

    Every test goes through here rather than calling the renderer directly, so
    the invariant covers each document in this file — including ones added later
    — instead of a hand-maintained second copy of the corpus that silently drifts
    out of step with the cases the tests actually assert on.
    """
    rendered = extract_text_from_adf(doc)
    if "  " not in "".join(_source_text_nodes(doc)):
        assert "  " not in rendered, f"renderer manufactured a double space: {rendered!r}"
    return rendered


def _legacy_walk(node) -> str:
    """The pre-fix renderer, verbatim, as a reference for "what was stored".

    A re-implemented reference normally drifts from the real thing. This one
    cannot: it models history, which is frozen. It is what lets the recorded
    ``previously_stored`` values below be checked rather than merely trusted.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return " ".join(_legacy_walk(item) for item in node)
    if not isinstance(node, dict):
        return ""
    parts = []
    if "text" in node:
        parts.append(node["text"])
    if "content" in node:
        parts.append(_legacy_walk(node["content"]))
    return " ".join(parts).strip()


# --------------------------------------------------------------------------
# Smart links (no text child at all)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("node_type", CARD_TYPES)
def test_card_alone_in_paragraph_is_the_whole_body(node_type):
    """The mode that stored ``''``. Every card flavour carries ``attrs.url``."""
    url = "https://example.atlassian.net/browse/SUPPORT-6700"
    # The trailing space-only text node is Jira's own, not a fixture artefact:
    # the editor appends one after an inline smart link.
    doc = _doc(_para(_card(url, node_type), _text(" ")))

    assert _render(doc) == url


def test_card_mid_sentence_reads_in_its_original_position():
    """The dangerous mode: a sentence that still parsed, minus the URL."""
    doc = _doc(
        _para(
            _text("The client answered in "), _card("https://example.atlassian.net/browse/SUPPORT-11353"), _text(" ")
        ),
        _para(_text("and "), _card("https://example.atlassian.net/browse/SUPPORT-11352"), _text(" ")),
        _para(_text("closing this ticket as duplicated")),
    )

    assert _render(doc) == (
        "The client answered in https://example.atlassian.net/browse/SUPPORT-11353 "
        "and https://example.atlassian.net/browse/SUPPORT-11352 "
        "closing this ticket as duplicated"
    )


def test_card_nested_inside_a_table_cell_is_reached():
    """The walk is generic: nesting depth is not part of the rule."""
    url = "https://example.com/runbook"
    doc = _doc(
        {
            "type": "table",
            "content": [
                {
                    "type": "tableRow",
                    "content": [
                        {"type": "tableHeader", "content": [_para(_text("Runbook"))]},
                        {"type": "tableCell", "content": [_para(_card(url))]},
                    ],
                }
            ],
        }
    )

    assert _render(doc) == f"Runbook {url}"


@pytest.mark.parametrize(
    "attrs",
    [
        pytest.param({"data": {"@id": "https://example.com/resolved"}}, id="json-ld-@id"),
        pytest.param({"data": {"url": "https://example.com/resolved"}}, id="json-ld-url"),
    ],
)
def test_block_card_with_json_ld_data_instead_of_url(attrs):
    """A resolved blockCard can spell its target ``attrs.data``, same link."""
    assert _render(_doc(_para({"type": "blockCard", "attrs": attrs}))) == "https://example.com/resolved"


def test_card_without_any_target_contributes_nothing():
    """An unresolvable card must not leave a stray token or a double space."""
    doc = _doc(_para(_text("before"), {"type": "inlineCard", "attrs": {}}, _text("after")))

    assert _render(doc) == "before after"


# --------------------------------------------------------------------------
# ``link`` marks (href outside the text node)
# --------------------------------------------------------------------------


def test_link_mark_with_label_text_keeps_both_label_and_href():
    doc = _doc(
        _para(_text("Synced from "), _text("ENG-1943", href="https://linear.app/example/issue/ENG-1943"), _text(" now"))
    )

    assert _render(doc) == "Synced from ENG-1943 (https://linear.app/example/issue/ENG-1943) now"


def test_autolink_where_text_equals_href_is_not_duplicated():
    url = "https://example.com/docs"
    doc = _doc(_para(_text("see "), _text(url, href=url)))

    assert _render(doc) == f"see {url}"


def test_autolink_differing_only_by_a_trailing_slash_is_not_duplicated():
    """Jira normalises the href; one copy of a URL that is whole either way."""
    doc = _doc(_para(_text("https://example.com", href="https://example.com/")))

    assert _render(doc) == "https://example.com/"


def test_autolink_where_jira_inferred_the_scheme_keeps_the_authored_word():
    """Jira autolinks freely — a bare ``SKILL.md`` becomes ``http://SKILL.md``.

    The href adds only a scheme the autolinker guessed, and the anchor text
    already carries every character of the target, so emitting the href would
    rewrite an authored word into a URL that never existed.
    """
    doc = _doc(_para(_text("edit "), _text("SKILL.md", href="http://SKILL.md"), _text(" first")))

    assert _render(doc) == "edit SKILL.md first"


def test_autolink_of_a_bare_hostname_keeps_the_authored_spelling():
    """Same rule, the case that actually dominates live data."""
    assert _render(_doc(_para(_text("www.example.io", href="https://www.example.io")))) == "www.example.io"


def test_mailto_href_is_emitted_verbatim_including_the_scheme():
    doc = _doc(_para(_text("write to "), _text("support@example.com", href="mailto:support+unsubscribe@example.com")))

    assert _render(doc) == "write to support@example.com (mailto:support+unsubscribe@example.com)"


def test_link_mark_on_whitespace_only_text_keeps_the_href():
    """No label to keep, but the target is still the only content there."""
    doc = _doc(_para(_text("before"), _text(" ", href="https://example.com/x"), _text("after")))

    assert _render(doc) == "before https://example.com/x after"


def test_link_mark_alongside_other_marks_is_still_found():
    node = {
        "type": "text",
        "text": "the docs",
        "marks": [{"type": "strong"}, {"type": "link", "attrs": {"href": "https://example.com/d"}}],
    }

    assert _render(_doc(_para(node))) == "the docs (https://example.com/d)"


@pytest.mark.parametrize(
    "marks",
    [
        pytest.param([{"type": "link"}], id="no-attrs"),
        pytest.param([{"type": "link", "attrs": {}}], id="no-href"),
        pytest.param([{"type": "link", "attrs": {"href": ""}}], id="empty-href"),
        pytest.param([{"type": "link", "attrs": {"href": "   "}}], id="blank-href"),
        pytest.param([{"type": "link", "attrs": None}], id="null-attrs"),
        pytest.param("nope", id="marks-not-a-list"),
    ],
)
def test_malformed_link_mark_falls_back_to_plain_text(marks):
    """A mark with no usable href must not raise and must not invent a token."""
    assert _render(_doc(_para({"type": "text", "text": "label", "marks": marks}))) == "label"


# --------------------------------------------------------------------------
# Credential redaction (a href is verbatim except for the secrets in it)
# --------------------------------------------------------------------------


def test_jwt_in_a_card_url_is_redacted_and_the_rest_is_verbatim():
    """The motivating case: a JSM unsubscribe link is a signed bearer token.

    These columns ship to analyst laptops as parquet, so the URL is kept whole
    and identifiable while the credential goes.
    """
    doc = _doc(_para(_card("https://example.atlassian.net/servicedesk/customer/portal/9/S-1/unsubscribe?jwt=eyJhbGci")))

    assert _render(doc) == "https://example.atlassian.net/servicedesk/customer/portal/9/S-1/unsubscribe?jwt=REDACTED"


@pytest.mark.parametrize(
    "param",
    ["jwt", "token", "access_token", "refresh_token", "mfaManagementToken", "apikey", "api_key", "signature", "sig"],
)
def test_credential_parameter_names_are_matched_across_vendor_spellings(param):
    """A substring match, so a vendor's own compound spelling is still caught."""
    doc = _doc(_para(_card(f"https://example.com/x?{param}=s3cret")))

    assert _render(doc) == f"https://example.com/x?{param}=REDACTED"


@pytest.mark.parametrize("param", ["tok", "key", "pin", "auth", "code", "sdata"])
def test_short_credential_names_are_matched_whole(param):
    """Matched exactly, never as a substring — see the two tests below."""
    assert (
        _render(_doc(_para(_card(f"https://example.com/x?{param}=abc123"))))
        == f"https://example.com/x?{param}=REDACTED"
    )


@pytest.mark.parametrize("param", ["author", "design", "keyword", "authority", "codebase", "pinned", "data", "userid"])
def test_innocent_parameters_that_merely_contain_a_secret_word_are_left_alone(param):
    """The false-positive trap: ``sig`` in ``design``, ``auth`` in ``author``.

    Over-redaction is the same bug class this whole change exists to fix, so the
    short names are exact matches and these must survive untouched.
    """
    url = f"https://example.com/x?{param}=visible"

    assert _render(_doc(_para(_card(url)))) == url


def test_only_the_secret_value_goes_and_separators_are_preserved():
    doc = _doc(_para(_card("https://example.com/p?utm_source=mail&token=abc&page=3;sig=xyz&userid=77")))

    assert _render(doc) == "https://example.com/p?utm_source=mail&token=REDACTED&page=3;sig=REDACTED&userid=77"


def test_a_secret_in_the_fragment_is_redacted_too():
    """OAuth's implicit flow puts the token after the ``#``, not in the query."""
    doc = _doc(_para(_card("https://example.com/cb#access_token=abc&state=ok")))

    assert _render(doc) == "https://example.com/cb#access_token=REDACTED&state=ok"


def test_a_url_with_no_parameters_is_untouched():
    for url in ("https://example.com/a/b", "https://example.com/a?", "https://example.com/a#frag"):
        assert _render(_doc(_para(_card(url)))) == url


def test_an_autolinked_credential_url_is_redacted_once_with_no_raw_copy():
    """The ordering trap this is written to catch.

    The autolink comparison runs on the RAW href. Redacting first would make the
    href stop matching its own anchor text, and the labelled form would then
    print the raw URL next to the redacted one — publishing the very credential
    it had just removed.
    """
    url = "https://example.com/x?token=s3cret"
    rendered = _render(_doc(_para(_text(url, href=url))))

    assert rendered == "https://example.com/x?token=REDACTED"
    assert "s3cret" not in rendered


def test_an_autolink_jira_gave_a_scheme_is_redacted_in_the_label_it_emits():
    """That branch emits the label, which in that branch IS the target."""
    rendered = _render(_doc(_para(_text("example.com/x?tok=s3cret", href="http://example.com/x?tok=s3cret"))))

    assert rendered == "example.com/x?tok=REDACTED"
    assert "s3cret" not in rendered


def test_a_labelled_links_anchor_text_is_prose_and_is_not_rewritten():
    """Only URLs the renderer emits are redacted; authored prose is left alone.

    An author writing ``?token=<project-token>`` as documentation keeps it — that
    is a placeholder in a sentence, not a credential, and it was stored verbatim
    before this change too.
    """
    doc = _doc(_para(_text("use ?token=<project-token>", href="https://example.com/oauth?token=real")))

    assert _render(doc) == "use ?token=<project-token> (https://example.com/oauth?token=REDACTED)"


# --------------------------------------------------------------------------
# Non-link rendering is unchanged
# --------------------------------------------------------------------------


def test_media_node_still_contributes_nothing():
    """Attachment-only comments keep storing ``''`` — filenames live in
    ``attachments``, and alt text is deliberately out of scope here."""
    media = {
        "type": "mediaSingle",
        "content": [{"type": "media", "attrs": {"id": "abc", "type": "file", "alt": "screenshot.png"}}],
    }

    assert _render(_doc(media)) == ""
    assert _render(_doc(_para(_text("see")), media)) == "see"


def test_plain_rich_text_renders_as_before():
    doc = _doc(
        _para(_text("Hello "), {"type": "text", "text": "world", "marks": [{"type": "strong"}]}),
        {
            "type": "bulletList",
            "content": [
                {"type": "listItem", "content": [_para(_text("first"))]},
                {"type": "listItem", "content": [_para(_text("second"))]},
            ],
        },
    )

    assert _render(doc) == "Hello world first second"


def test_code_block_newlines_and_indentation_survive_verbatim():
    """Only fragment *edges* are normalised; a code block's interior is content."""
    code = "def f():\n\tif x:\n\t\treturn 1"
    doc = _doc(_para(_text("run")), {"type": "codeBlock", "content": [{"type": "text", "text": code}]})

    assert _render(doc) == f"run {code}"


def test_a_node_that_renders_to_nothing_does_not_leave_a_gap():
    assert _render(_doc(_para(_text("a"), {"type": "hardBreak"}, _text("b")))) == "a b"


def test_authored_double_space_is_preserved_as_content():
    """A run of spaces the author typed is content; only gaps are normalised.

    Rendered directly, not through ``_render``: this is the one document whose
    source text nodes carry a double space, so it is the case the invariant in
    ``_render`` deliberately exempts.
    """
    assert extract_text_from_adf(_doc(_para(_text("two  spaces")))) == "two  spaces"


def test_degenerate_inputs_are_unchanged():
    assert _render(None) == ""
    assert _render([]) == ""
    assert _render(_doc()) == ""
    assert _render(42) == ""
    assert _render({"type": "text", "text": 7}) == ""


# --------------------------------------------------------------------------
# The three stored columns share this one renderer
# --------------------------------------------------------------------------


def test_comment_body_and_issue_description_and_context_all_inline_urls():
    url = "https://example.atlassian.net/browse/SUPPORT-1"
    doc = _doc(_para(_card(url), _text(" ")))

    raw_issue = {
        "key": "SUPPORT-1",
        "id": "1",
        "fields": {
            "description": doc,
            "customfield_10330": doc,
            "comment": {"comments": [{"id": "114248", "body": doc, "jsdPublic": True}], "total": 1},
        },
    }

    issue = transform_issue(raw_issue)
    assert issue["description"] == url
    assert issue["context"] == url

    (comment,) = transform_comments(raw_issue)
    assert comment["body"] == url


# --------------------------------------------------------------------------
# Regression cases captured from live Jira (real comment IDs)
# --------------------------------------------------------------------------

#: ``comment_id -> (body, previously_stored, expected)``. The ADF shapes and the
#: ``previously_stored`` values are real: bodies taken from
#: ``POST /rest/api/3/comment/list``, stored values read out of the comments
#: parquet, both on 2026-08-20. Only the HOSTNAMES are substituted for
#: placeholders — this is the public distribution, so no deployment of it belongs
#: in a fixture. Nothing under test depends on the host: a card contributes
#: nothing to ``previously_stored`` either way.
LIVE_REGRESSIONS: dict[str, tuple[dict, str, str]] = {
    # Card-only comments — stored as the empty string.
    "114248": (
        _doc(_para(_card("https://example.atlassian.net/browse/SUPPORT-6700"), _text(" "))),
        "",
        "https://example.atlassian.net/browse/SUPPORT-6700",
    ),
    "118232": (
        _doc(_para(_card("https://example.atlassian.net/browse/SUPPORT-7005"), _text(" "))),
        "",
        "https://example.atlassian.net/browse/SUPPORT-7005",
    ),
    "185818": (
        _doc(_para(_card("https://github.com/example/example-repo/pull/679"), _text(" "))),
        "",
        "https://github.com/example/example-repo/pull/679",
    ),
    "186565": (
        _doc(_para(_card("https://example.slack.com/archives/C0EXAMPLE1/p1763104269904019"), _text(" "))),
        "",
        "https://example.slack.com/archives/C0EXAMPLE1/p1763104269904019",
    ),
    # Card mid-sentence — stored as a sentence that read as valid data.
    "155620": (
        _doc(
            _para(
                _text("The client answered in "),
                _card("https://example.atlassian.net/browse/SUPPORT-11353"),
                _text(" "),
            ),
            _para(_text("and "), _card("https://example.atlassian.net/browse/SUPPORT-11352"), _text(" ")),
            _para(_text("closing this ticket as duplicated")),
        ),
        "The client answered in and closing this ticket as duplicated",
        (
            "The client answered in https://example.atlassian.net/browse/SUPPORT-11353 "
            "and https://example.atlassian.net/browse/SUPPORT-11352 closing this ticket as duplicated"
        ),
    ),
}


@pytest.mark.parametrize("comment_id", sorted(LIVE_REGRESSIONS))
def test_live_regression_bodies(comment_id):
    body, previously_stored, expected = LIVE_REGRESSIONS[comment_id]

    # The recorded value really is what the old walk produced, so the pin below
    # is evidence rather than a transcription anyone has to take on trust.
    assert _legacy_walk(body) == previously_stored

    rendered = _render(body)
    assert rendered == expected
    # Additive: whatever the old walk kept is still there.
    assert set(previously_stored.split()) <= set(rendered.split())
