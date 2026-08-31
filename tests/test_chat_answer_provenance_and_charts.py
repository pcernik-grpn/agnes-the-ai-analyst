"""The onboarding tour's two promises, and the mechanics that have to back them.

The newcomer walkthrough tells a first-time user, on the very first card, that
Agnes "always shows where the answer came from", and on the upload card that
she will "cite it in my answers". Neither was true: the chat agent's system
prompt is the `claude_code` preset plus the workspace ``CLAUDE.md``, and that
file said nothing about naming a source. The only provenance on screen was the
raw inline tool-call blocks — a log, not a citation.

The chart half is the same shape of gap seen from the other side. Asked for a
trend, the agent reached for matplotlib, could not install it (the sandbox has
no route to PyPI), improvised an SVG written to ``/tmp``, and told the user to
open a path that does not exist on their machine. Two separate holes: the
library was absent, and there is no file-delivery channel out of a chat session
at all (``app/chat/artifact_harvest.py`` is deliberately wired only into the
one-shot agent API, not into interactive chat).

The one channel that does work is inline ``<svg>`` in the reply. That is not a
feature anyone built — it is a property of two existing decisions (marked passes
raw HTML through; ``svg`` is not in the sanitizer's blocklist) that nothing
records. So the tests below pin it: the day someone adds ``svg`` to
``_DANGEROUS_TAGS`` for a good security reason, this file tells them they are
also removing the only way an answer can show a chart.

Layers, in the house style:

1. Contract — the prompt rules exist and name the mechanism, the sandbox images
   carry matplotlib, the renderer keeps the channel open.
2. Executable — the markdown parser and the URL-scheme allowlist run under
   ``node``, because "inline SVG renders and a data: URI does not" is a claim
   about code, not about a comment.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WORKSPACE_CLAUDE_MD = Path("app/initial_workspace_default/CLAUDE.md")
SERVER_DEFAULT_TEMPLATE = Path("config/claude_md_template.txt")
DOCKER_SANDBOX = Path("app/initial_workspace_default/docker-sandbox/Dockerfile")
CLOUD_CHAT_DOC = Path("docs/cloud-chat.md")
TOUR_JS = Path("app/web/static/js/tour.js")
CHAT_JS = Path("app/web/static/js/chat.js")
CHAT_CSS = Path("app/web/static/css/chat.css")
CHAT_HTML = Path("app/web/templates/chat.html")
MARKED_JS = Path("app/web/static/vendor/marked.min.js")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _collapse_ws(text: str) -> str:
    """Text with runs of whitespace collapsed — prose assertions must not
    break because a sentence got re-wrapped at 79 columns."""
    return re.sub(r"\s+", " ", text)


def _prose(p: Path) -> str:
    return _collapse_ws(_read(p))


def _rendered_server_default_claude_md(*, is_sandbox: bool = False) -> str:
    """Render config/claude_md_template.txt the same way production does.

    ``config/claude_md_template.txt`` is shared by two different production
    call sites, and they are NOT the same content: WorkdirManager.run_init's
    ``_render_workspace_prompt`` callable (app/main.py) renders it for the
    ephemeral chat sandbox (``is_sandbox=True``); a laptop's ``agnes init``
    fetches it via ``GET /api/welcome`` (``is_sandbox=False``, the default
    here). The bundled ``WORKSPACE_CLAUDE_MD`` file only stands in for the
    former — the sandbox overwrites it with this render, but a laptop
    workspace never has that bundled file to begin with. Pass
    ``is_sandbox=True`` to reproduce what the chat sandbox actually renders.
    """
    from unittest.mock import patch

    import duckdb

    from src.claude_md import compute_default_claude_md
    from src.db import _ensure_schema

    conn = duckdb.connect(":memory:")
    try:
        _ensure_schema(conn)
        # RBAC reads inside compute_default_claude_md go through the repo
        # factory (e.g. get_accessible_tables -> data_packages_repo()), which
        # resolves its connection via src.repositories.get_system_db — not the
        # conn passed to compute_default_claude_md directly. Redirect that
        # name to this connection, same as tests/test_claude_md_renderer.py's
        # fixture, or the factory opens (and may not find) the real system DB.
        with patch("src.repositories.get_system_db", lambda: conn):
            user = {
                "id": "u1",
                "email": "alice@example.com",
                "name": "Alice",
                "is_admin": False,
                "groups": ["Everyone"],
            }
            return compute_default_claude_md(conn, user=user, server_url="https://example.com", is_sandbox=is_sandbox)
    finally:
        conn.close()


def _section(text: str, heading: str) -> str:
    """Body of a ``## <heading>`` markdown section, up to the next ``## ``
    heading or end of file."""
    m = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=\n## |\Z)", text, re.DOTALL | re.MULTILINE)
    assert m, f"section {heading!r} not found"
    return m.group(1).strip()


# ── 1. Contract ─────────────────────────────────────────────────────────────


def test_the_tour_still_promises_provenance():
    """The premise of every assertion below. If this copy is ever softened,
    the prompt rule it justifies should be revisited rather than silently kept."""
    tour = _read(TOUR_JS)
    assert "always show where the answer came from" in tour
    assert "cite it in my answers" in tour


def _assert_promises_provenance(md: str) -> None:
    """The rule started life as a prose `Sources:` line. That was the right
    first move and the wrong final shape: prose cannot be checked, so a wrong
    claim and a missing one both looked like an ordinary answer. It is now a
    fenced block the server parses and verifies against the turn's own tool
    calls — see `tests/test_chat_sources_ui.py` for the contract and
    `tests/test_chat_sources_verdict.py` for the verification.

    What this keeps guarding is what did not change: that the prompt asks at
    all — in BOTH of its homes, per the two callers below — and that it draws
    the hard edge rather than only describing the happy path.
    """
    assert "```sources" in md, "the prompt must ask for a sources block"
    assert re.search(
        r"never report a (number|figure) whose origin you cannot name",
        md,
        re.IGNORECASE,
    ), "the rule needs the hard edge, not just the happy path"
    assert "metric" in md.lower(), "a canonical metric must be citable alongside the table"
    assert re.search(
        r"cannot fill (in )?the `?sources`? block.{0,120}say so",
        md,
        re.IGNORECASE | re.DOTALL,
    ), (
        "the silent failure has to be closed too: an answer that simply omits the block "
        "is indistinguishable from one that forgot it, so the prompt must require the "
        "agent to admit an unfillable block in the answer text"
    )


def test_the_workspace_prompt_makes_the_promise_true():
    """The gap Petr reported: the tour promised provenance and nothing in the
    agent's instructions asked for it. This pins the bundled fallback file —
    see the sibling test below for the content that actually reaches the
    sandbox on the common path."""
    _assert_promises_provenance(_prose(WORKSPACE_CLAUDE_MD))


def test_the_server_rendered_default_makes_the_promise_true():
    """The bundled ``CLAUDE.md`` above is only the initial file dropped into a
    fresh workspace — in DEFAULT mode (no admin override, no Initial Workspace
    Template) ``WorkdirManager.run_init`` immediately overwrites it with the
    server-rendered default (``config/claude_md_template.txt``), and that is
    also what a laptop's ``agnes init`` writes. A prompt rule that lives only
    in the bundled file never reaches an agent on that — the common — path."""
    _assert_promises_provenance(_collapse_ws(_rendered_server_default_claude_md()))


def _assert_names_chart_channel(md: str) -> None:
    assert "inline SVG" in md
    assert "svg.fonttype" in md, "without this matplotlib emits glyph outlines and the SVG is huge"
    # Was "Never tell the user to open a file path. They cannot reach it."
    # That stopped being true when the session-files panel shipped: a file
    # under `outputs/` IS reachable. The rule the agent still needs is the
    # narrower one — a *chart* belongs in the reply, never handed over as a
    # file — so the refusal is pinned in its current, honest form rather than
    # dropped. See "## Files you produce" for the half that is now allowed.
    assert "Never send a chart as a file." in md
    assert "data:" in md, "the failing alternative has to be named to be refused"
    assert "broken image" in md, "say what the user sees, not just that it is forbidden"


def test_the_workspace_prompt_names_the_only_chart_channel():
    """Inline SVG is the delivery mechanism; a file path and a data: URI are the
    two plausible-looking things that silently fail. All three must be stated —
    the agent reached for exactly the failing ones. Pins the bundled fallback
    file — see the sibling test below for the server-rendered default."""
    _assert_names_chart_channel(_prose(WORKSPACE_CLAUDE_MD))


def test_the_server_rendered_default_names_the_only_chart_channel():
    """Same gap as the provenance rule, for the chart rule, on the sandbox
    surface specifically: the bundled file is not what the chat sandbox sees
    once ``render_workspace_prompt`` succeeds (app/main.py passes
    ``is_sandbox=True``) — this is what actually reaches it. The channel rules
    pinned here (no file path, no data: URI, chat renders inline SVG) are only
    true for the sandbox; see the sibling test below for what a laptop
    workspace is told instead."""
    _assert_names_chart_channel(_collapse_ws(_rendered_server_default_claude_md(is_sandbox=True)))


def _assert_laptop_chart_guidance(md: str) -> None:
    assert "inline SVG" in md
    assert "svg.fonttype" in md, "without this matplotlib emits glyph outlines and the SVG is huge"
    assert "data:" in md, "the failing alternative has to be named to be refused"
    assert "broken image" in md, "say what a chat surface shows for a data: URI"
    assert "this filesystem really is the user's own computer" in md, (
        "a laptop workspace's filesystem really is the analyst's machine — the sandbox's "
        "'not their computer' framing must not leak here"
    )
    assert "tell them the path" in md, "a file path IS reachable outside a chat surface — say so"
    assert "Never tell the user to open a file path." not in md, (
        "that blanket ban is sandbox-only; on a laptop terminal a file path is the right answer"
    )
    assert "You have `matplotlib`, `pandas` and `numpy` preinstalled." not in md, (
        "false on a laptop workspace unless the analyst installed them themselves — must be "
        "conditional ('if installed'), not asserted as fact"
    )


def test_the_server_rendered_default_gives_true_chart_guidance_on_a_laptop():
    """The gap this test guards: ``config/claude_md_template.txt`` is also what
    ``agnes init`` writes to a laptop workspace via ``GET /api/welcome``
    (``is_sandbox=False``, the default) — and there, 'matplotlib preinstalled'
    and 'this sandbox's filesystem is not their computer' are simply false,
    and 'never tell the user to open a file path' is wrong advice: on their
    own machine a file path is the natural way to deliver a chart. The rule
    that must survive on both surfaces — never use a ``data:`` URI, inline SVG
    is what a *chat* surface renders — still has to be stated; only the
    file-path / preinstalled claims differ."""
    _assert_laptop_chart_guidance(_collapse_ws(_rendered_server_default_claude_md(is_sandbox=False)))


def test_the_say_where_it_came_from_section_does_not_drift():
    """The bundled ``CLAUDE.md`` and ``config/claude_md_template.txt`` are two
    independent files with no shared source for this prose — copy-pasted
    rather than factored out, because one is a static file and the other a
    Jinja2 template with a very different overall shape (see CONTRIBUTING.md's
    sync-map row for this pair). That makes them free to drift silently: an
    edit to one rule's wording in one file, forgotten in the other, would
    leave the sandbox and the laptop CLI disagreeing about the rules with no
    test failure anywhere — exactly how the section went missing from this
    file the first time. This section never varies by surface, so pin it
    equal verbatim — a future edit is forced to touch both or explain why
    not. (The 'Charts' section is the sibling case that DOES vary by
    surface — see test_the_charts_sandbox_wording_does_not_drift below.)"""
    heading = "Say where every number came from"
    bundled = _section(_read(WORKSPACE_CLAUDE_MD), heading)
    server_default = _section(_read(SERVER_DEFAULT_TEMPLATE), heading)
    assert bundled == server_default, (
        f"the {heading!r} section text differs between {WORKSPACE_CLAUDE_MD} and "
        f"{SERVER_DEFAULT_TEMPLATE} — keep them byte-identical or this guard will always fail"
    )


def test_the_dashboard_figure_section_does_not_drift():
    """Same pairing and same reason as
    ``test_the_say_where_it_came_from_section_does_not_drift``: the bundled
    file is only the chat sandbox's fallback, and ``WorkdirManager.run_init``
    overwrites it with this template's render on the common path — so a rule
    that lives in the bundled file alone never reaches the agent at all. This
    section carries no sandbox-specific claim (both surfaces reach the same
    ``agnes app`` / ``agnes catalog`` CLI), so pin it equal verbatim against
    the template's raw source rather than a render."""
    heading = "Numbers that come from a dashboard or an app"
    bundled = _section(_read(WORKSPACE_CLAUDE_MD), heading)
    server_default = _section(_read(SERVER_DEFAULT_TEMPLATE), heading)
    assert bundled == server_default, (
        f"the {heading!r} section text differs between {WORKSPACE_CLAUDE_MD} and "
        f"{SERVER_DEFAULT_TEMPLATE} — keep them byte-identical or this guard will always fail"
    )


def test_the_charts_sandbox_wording_does_not_drift():
    """The bundled ``CLAUDE.md`` is chat-sandbox-only (never written to a
    laptop workspace), so its 'Charts' section is no longer meant to be
    byte-identical to ``config/claude_md_template.txt``'s raw *source* — that
    template also renders for a laptop workspace, where the sandbox-specific
    claims are false, so it now branches on ``is_sandbox`` (see the docstring
    at the top of that file). What is still genuinely shared between the two
    files is the sandbox's wording in full: comparing the bundled file
    against the template's ``is_sandbox=True`` *rendered* output (rather than
    its raw source) still pins that shared text equal verbatim, so an edit to
    the sandbox branch in one file, forgotten in the other, still fails this
    test — same guarantee as before, applied to the render instead of the
    source. See CONTRIBUTING.md's sync-map row for this pair."""
    bundled = _section(_read(WORKSPACE_CLAUDE_MD), "Charts")
    server_default = _section(_rendered_server_default_claude_md(is_sandbox=True), "Charts")
    assert bundled == server_default, (
        f"the sandbox-facing 'Charts' wording differs between {WORKSPACE_CLAUDE_MD} and "
        f"{SERVER_DEFAULT_TEMPLATE}'s is_sandbox=True branch — keep them identical or this "
        "guard will always fail"
    )


def _assert_names_the_file_handover_channel(md: str) -> None:
    assert "outputs/" in md, "the one directory every surface collects from has to be named"
    assert ".claude/" in md, (
        "the failing location has to be named to be refused — a deliverable left under "
        ".claude/ is filtered out of the engine's file browser and never reaches the user"
    )
    assert "download button" in md, (
        "the agent must be told NOT to promise a download control — whether the surface "
        "shows one is provider- and deployment-dependent, and it cannot see that from inside"
    )


def test_the_workspace_prompt_names_the_file_handover_channel():
    """A chart is *shown* in the reply; a document is *handed over* as a file,
    and where the agent writes it decides whether it can reach the user at all.
    `outputs/` is the intersection of all three collectors (the agent-API
    harvest scans ``/work/outputs``, the engine's sandbox browser lists the
    workspace tree, the host walk lists the session dir), while ``.claude/``
    is filtered out by the engine browser — which is exactly where the skill
    in #1611 wrote its SOW and deck, so they were invisible. Pins the bundled
    fallback file; see the sibling below for the server-rendered default."""
    _assert_names_the_file_handover_channel(_prose(WORKSPACE_CLAUDE_MD))


def test_the_server_rendered_default_names_the_file_handover_channel():
    """Same gap as the chart rule: in DEFAULT mode the bundled file above is
    immediately overwritten by this render, so a handover rule that lives only
    in the bundled file never reaches an agent on the common path."""
    _assert_names_the_file_handover_channel(_collapse_ws(_rendered_server_default_claude_md(is_sandbox=True)))


def test_a_laptop_workspace_is_not_told_to_write_into_outputs():
    """The mirror of ``test_the_server_rendered_default_gives_true_chart_guidance_on_a_laptop``:
    `outputs/` is a *sandbox* collection point. On a laptop workspace the
    filesystem really is the analyst's own machine, nothing harvests
    ``outputs/``, and the right answer is the path itself — so this section
    must stay inside the ``is_sandbox`` branch."""
    laptop = _collapse_ws(_rendered_server_default_claude_md(is_sandbox=False))
    assert "Files you produce" not in laptop, (
        "the outputs/ handover rule is sandbox-only — on a laptop there is no collector "
        "and naming the path IS the delivery"
    )
    assert "outputs/" not in laptop, (
        "assert on the directory too, not just the heading: a rename of the section would "
        "otherwise let the sandbox-only rule leak into the laptop render unnoticed"
    )


def test_the_file_handover_wording_does_not_drift():
    """Same pairing as ``test_the_charts_sandbox_wording_does_not_drift``: two
    independent files carry this sandbox prose with no shared source, so pin
    the bundled file equal to the template's ``is_sandbox=True`` render or an
    edit to one will silently leave the other behind."""
    heading = "Files you produce"
    bundled = _section(_read(WORKSPACE_CLAUDE_MD), heading)
    server_default = _section(_rendered_server_default_claude_md(is_sandbox=True), heading)
    assert bundled == server_default, (
        f"the sandbox-facing {heading!r} wording differs between {WORKSPACE_CLAUDE_MD} and "
        f"{SERVER_DEFAULT_TEMPLATE}'s is_sandbox=True branch — keep them identical or this "
        "guard will always fail"
    )


def test_the_prompt_no_longer_claims_files_cannot_be_delivered():
    """The sentence this replaced ('What you do not have is any way to hand the
    user a file') was true when the only channel out was inline SVG, and false
    from #1613 on — while still actively steering the agent away from writing
    the deliverable the user asked for. Pin the retraction: a future edit that
    reinstates the blanket claim has to fail here first."""
    for md in (
        _prose(WORKSPACE_CLAUDE_MD),
        _collapse_ws(_rendered_server_default_claude_md(is_sandbox=True)),
    ):
        assert "any way to hand the user a file" not in md, (
            "the blanket 'no file channel' claim is false since #1613 — say where to write the deliverable instead"
        )


def test_the_sandbox_image_carries_matplotlib():
    """`pip install matplotlib` inside the sandbox cannot reach PyPI, so the
    prompt rule above is unfulfillable unless the image ships it."""
    body = _read(DOCKER_SANDBOX)
    assert "matplotlib>=" in body, f"{DOCKER_SANDBOX} must bake matplotlib in"


def test_the_contract_label_matches_what_the_docs_tell_operators_to_expect():
    """The operator note in docs/cloud-chat.md tells the reader to rebuild the
    sandbox image after upgrading Agnes and confirm the new contract with
    `docker inspect … agnes.chat-sandbox.contract`. That check is worthless if
    the label never moves: this PR added `matplotlib` to the image's pip
    install block (a real contract change — a pre-upgrade image cannot draw a
    chart) without bumping `LABEL agnes.chat-sandbox.contract`, so a stale and
    a current image reported the identical value — exactly the case the
    paragraph exists to catch. Pins the two sides of that check to each other
    so they can't drift apart silently again."""
    label = re.search(r'LABEL agnes\.chat-sandbox\.contract="(\d+)"', _read(DOCKER_SANDBOX))
    assert label, "the contract LABEL moved — re-point this guard"
    doc = re.search(r"confirm the contract label reads `(\d+)`", _read(CLOUD_CHAT_DOC))
    assert doc, "docs/cloud-chat.md no longer states the expected contract label value"
    assert label.group(1) == doc.group(1), (
        f"Dockerfile LABEL is {label.group(1)!r} but the docs tell the operator to expect "
        f"{doc.group(1)!r} — bump whichever one is stale"
    )


def _dangerous_tags() -> set[str]:
    block = re.search(r"_DANGEROUS_TAGS = new Set\(\[(.*?)\]\)", _read(CHAT_JS), re.DOTALL)
    assert block, "_DANGEROUS_TAGS moved — re-point this guard"
    return set(re.findall(r'"([a-z]+)"', block.group(1)))


def test_the_sanitizer_keeps_the_chart_channel_open():
    """`svg` in `_DANGEROUS_TAGS` would close the only route a chart has to the
    user — silently, since the answer would still 'render'."""
    tags = _dangerous_tags()
    assert "svg" not in tags, "blocking <svg> removes the only chart channel — see this test's docstring"
    # The blocklist is still doing its job; this is not an argument for a laxer one.
    assert {"script", "iframe", "style", "object"} <= tags
    # …and the attribute pass is what makes an <svg> from an untrusted turn safe.
    assert 'name.startsWith("on")' in _read(CHAT_JS), "inline handlers must still be stripped"


def test_smil_and_foreignobject_stay_blocked():
    """Admitting `<svg>` while stripping `on*` handlers and unsafe URL schemes
    is not sufficient on its own, because SMIL defers both checks to runtime:
    they inspect the attributes an element HAS, and SMIL sets one it does not.

    Measured against this sanitizer before these five names were added, all
    three of these survived it completely intact — element, `attributeName`
    and payload:

        <a href="#x"><animate attributeName="href" values="javascript:…"></a>
        <a><animate attributeName="xlink:href" to="javascript:…"></a>
        <set attributeName="onload" to="…">

    The scheme allowlist never sees the first two (`values`/`to` are not
    URL-bearing attribute *names*) and the `on*` strip never sees the third
    (there the handler name is an attribute *value*). `foreignObject` is
    HTML-in-SVG and the usual container in these chains.

    The threat model is NOT the chart author. A co-presence peer's message
    renders through the same `renderMarkdownSafe`, and no prompt rule binds
    them — which is also why "matplotlib never emits SMIL" is not a defence.
    It is, however, why this costs nothing: measured on real matplotlib
    output, a figure contains zero SMIL elements and no foreignObject.
    """
    tags = _dangerous_tags()
    missing = {"animate", "animatetransform", "animatemotion", "set", "foreignobject"} - tags
    assert not missing, (
        f"{sorted(missing)} dropped from _DANGEROUS_TAGS — a chat message can set an "
        "href or an event handler at runtime, which the attribute pass cannot see"
    )


def test_a_chart_svg_is_contained_by_the_bubble():
    """matplotlib stamps an absolute pt width on the root element; unconstrained
    it takes the page's horizontal scroll with it."""
    css = _read(CHAT_CSS)
    assert ".msg-body > svg" in css
    assert ".msg-body > p > svg" in css, "a one-line SVG comes through wrapped in <p>"


def test_the_thread_title_reserves_room_for_the_header_action():
    """Measured on the rendered page: without this cap a title long enough to
    fill the row keeps its full width and the action is painted over its last
    ~109px, hiding the ellipsis. The title is the header's only flexible item
    and the action's `margin-left:auto` consumes the free space before flex
    shrinking runs, so neither `min-width:0` nor dropping the auto margin fixes
    it — see the rationale in chat.css."""
    css = _read(CHAT_CSS)
    assert "max-width: calc(100% - 130px)" in css, (
        "the thread title's reserve for .cloud-chat-thread-action is gone — a long title "
        "will render its ellipsis underneath the button"
    )


def test_copy_transcript_is_wired():
    """The answer to 'can I report this session?' — sessions are owner-only, so
    the clipboard is the only way a conversation leaves Agnes."""
    assert 'id="chat-copy-transcript"' in _read(CHAT_HTML)
    chat = _read(CHAT_JS)
    assert "wireCopyTranscript()" in chat, "the button must be wired at boot, not just declared"
    assert "/api/chat/sessions/${encodeURIComponent(chatId)}/messages" in chat
    assert "tool: ${call.label}" in chat, "tool calls are the provenance — a transcript without them is undiagnosable"


def test_the_clipboard_write_is_issued_synchronously_from_the_click():
    """`navigator.clipboard.writeText`/`.write` need an unbroken chain of
    synchronous calls back to the user gesture on WebKit; an `await` before
    the call — such as `await fetchTranscriptMarkdown(...)`, a real network
    round-trip — drops that transient activation, and the button reports
    "Couldn't copy to clipboard" even though the fetch and the write would
    both have succeeded. A promise-based `ClipboardItem` is the fix: the
    *value* can resolve after the fetch, but `navigator.clipboard.write(...)`
    itself must be *called* with nothing awaited first."""
    chat = _read(CHAT_JS)
    fn = re.search(r"function wireCopyTranscript\(\) \{.*?\n\}\n", chat, re.DOTALL)
    assert fn, "wireCopyTranscript moved — re-point this guard"
    body = fn.group(0)
    assert "ClipboardItem" in body, "the standards-track fix (a promise-based ClipboardItem) is gone"
    before_write, _, after = body.partition("await navigator.clipboard.write(")
    assert after, "navigator.clipboard.write(...) call not found in wireCopyTranscript"
    assert "const md = fetchTranscriptMarkdown(chatId, title);" in before_write, (
        "the transcript fetch must be a promise handed to ClipboardItem, not awaited first"
    )
    assert "await fetchTranscriptMarkdown" not in before_write, (
        "an `await` before the clipboard write drops the click's user-activation on WebKit"
    )


# ── 2. Executable ───────────────────────────────────────────────────────────


def _node() -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available — the rendering-path tests need a runtime")
    return node


def test_marked_passes_a_matplotlib_shaped_svg_through_intact():
    """Half one of 'inline SVG renders': the markdown parser must not mangle a
    multi-line figure. Shaped like real matplotlib output — pt dimensions, a
    <defs><style> block, <g>/<path>/<text> — because a single-line <svg> would
    take a different path through marked (inline, wrapped in <p>)."""
    node = _node()
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="460pt" height="200pt" viewBox="0 0 460 200">\n'
        " <defs>\n"
        '  <style type="text/css">*{stroke-linecap:butt;}</style>\n'
        " </defs>\n"
        ' <g id="figure_1">\n'
        '  <path d="M 10 10 L 50 90" style="stroke:#1f77b4;"/>\n'
        '  <text x="20" y="30">Q1</text>\n'
        " </g>\n"
        "</svg>"
    )
    md = f"Headcount over time:\n\n{svg}\n\nSources: hr_headcount\n"
    script = (
        f"const m = require({json.dumps(str(MARKED_JS.resolve()))});\n"
        "const parse = m.parse || m.marked.parse;\n"
        f"process.stdout.write(parse({json.dumps(md)}));\n"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, f"node failed:\n{out.stderr}"
    html = out.stdout
    assert '<text x="20" y="30">Q1</text>' in html, "the figure body was mangled"
    assert '<path d="M 10 10 L 50 90"' in html
    # Top-level block, not swallowed into a paragraph — which is why the CSS
    # containment rule above targets `.msg-body > svg`.
    assert "<p><svg" not in html


def test_the_scheme_allowlist_rejects_a_data_uri_image():
    """Half two: the obvious alternative (`![](data:image/png;base64,…)`) is
    stripped to a broken image, which is why the prompt rule forbids it. Runs
    the real regex out of chat.js, not a copy of it."""
    node = _node()
    chat = _read(CHAT_JS)
    decl = re.search(r"const _SAFE_URL_SCHEME_RE = .*?;", chat)
    assert decl, "_SAFE_URL_SCHEME_RE moved — re-point this guard"
    cases = [
        "data:image/png;base64,AAA",
        "https://example.com/chart.png",
        "/static/img/chart.png",
        "javascript:alert(1)",
    ]
    script = (
        decl.group(0)
        + "\n"
        + f"process.stdout.write(JSON.stringify({json.dumps(cases)}.map(v => _SAFE_URL_SCHEME_RE.test(v))));\n"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, f"node failed:\n{out.stderr}"
    data_ok, https_ok, relative_ok, js_ok = json.loads(out.stdout)
    assert data_ok is False, "a data: image would render — the prompt rule could be relaxed"
    assert js_ok is False, "javascript: must stay refused"
    assert https_ok is True and relative_ok is True, "ordinary image sources must still work"


def test_a_tool_call_row_with_no_tool_name_is_skipped_not_rendered_as_undefined():
    """The only `tool_calls` a persisted message ever actually carries on the
    live agent path are manager.py's cancelled/interrupted markers
    (`{"cancelled": true}`, `{"interrupted": true, "reason": …}`) — real tool
    calls are never re-attached to the stored assistant message. Before
    `formatToolCall()` existed, both the transcript export and the live
    renderer built `tool: ${tc.tool}` unconditionally: `tc.tool` is
    `undefined` for a marker, and `JSON.stringify(undefined, …)` is also
    `undefined`, so the row rendered as the literal text "tool: undefined"
    with an empty ```json``` fence. Runs the real function out of chat.js."""
    node = _node()
    chat = _read(CHAT_JS)
    decl = re.search(r"function formatToolCall\(tc\) \{.*?\n\}\n", chat, re.DOTALL)
    assert decl, "formatToolCall moved — re-point this guard"
    # formatToolCall now routes the label through _toolLabel (human labels,
    # tests/test_chat_tool_rendering_ui.py) — ship its registry block too so
    # the shipped function runs unmodified.
    labels = chat[chat.index("const _TOOL_LABELS") : chat.index("function renderApprovalRequest")]
    cases = [
        {"tool": "agnes_query", "args": {"sql": "SELECT 1"}},
        {"cancelled": True},
        {"interrupted": True, "reason": "kicked"},
        {},
    ]
    script = (
        labels
        + "\n"
        + decl.group(0)
        + "\n"
        + f"process.stdout.write(JSON.stringify({json.dumps(cases)}.map(formatToolCall)));\n"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, f"node failed:\n{out.stderr}"
    real_call, cancelled, interrupted, empty = json.loads(out.stdout)
    assert real_call == {
        "label": "Agnes query",
        "tool": "agnes_query",
        "argsJson": json.dumps({"sql": "SELECT 1"}, indent=2),
    }, "a real tool call carries its humanized label AND the raw id (the export has no tooltip)"
    assert cancelled is None, "a cancelled marker has no `tool` name and must be skipped, not stringified"
    assert interrupted is None, "an interrupted marker has no `tool` name and must be skipped, not stringified"
    assert empty is None


def _wire_copy_transcript_source() -> str:
    chat = _read(CHAT_JS)
    fn = re.search(r"function wireCopyTranscript\(\) \{.*?\n\}\n", chat, re.DOTALL)
    assert fn, "wireCopyTranscript moved — re-point this guard"
    return fn.group(0)


def _run_clipboard_scenarios(scenarios: dict) -> dict:
    """Drive the real ``wireCopyTranscript`` (extracted verbatim from
    chat.js) through a simulated click for each named scenario, with
    ``fetchTranscriptMarkdown``/``copyTextToClipboard``/``showToast``/``$``
    stubbed so the test controls exactly which step fails. Returns
    ``{scenario_name: {toasts, copyTextToClipboard_calls, unhandled_rejections}}``.
    """
    node = _node()
    harness = f"""
"use strict";
let unhandledCount = 0;
process.on("unhandledRejection", () => {{ unhandledCount++; }});

async function runScenario(opts) {{
  const calls = {{ copyTextToClipboard: [], showToast: [] }};
  let currentChatId = "chat-1";

  async function fetchTranscriptMarkdown(chatId) {{
    if (opts.fetchFails) throw new Error("network down");
    return "# transcript";
  }}
  async function copyTextToClipboard(text) {{
    calls.copyTextToClipboard.push(text);
    return opts.fallbackSucceeds !== false;
  }}
  function showToast(text, kind) {{
    calls.showToast.push({{ text, kind }});
  }}

  const _btn = {{
    disabled: false,
    addEventListener(evt, fn) {{
      if (evt === "click") this._handler = fn;
    }},
  }};
  function $(id) {{
    return id === "chat-copy-transcript" ? _btn : null;
  }}

  class FakeClipboardItem {{
    constructor(data) {{
      // Some implementations refuse a promise-valued entry synchronously —
      // the constructor throws before ever consuming `data`.
      if (opts.ctorThrows) throw new Error("refuses a promise-valued entry");
      this._data = data;
    }}
  }}
  async function fakeWrite(items) {{
    if (opts.writeDeniedEarly) {{
      const e = new Error("Write permission denied.");
      e.name = "NotAllowedError";
      throw e;
    }}
    // Spec behaviour: write() rejects if any item's data promise rejects.
    for (const item of items) {{
      for (const type of Object.keys(item._data)) {{
        await item._data[type];
      }}
    }}
  }}

  // Node 21+ predefines a read-only global `navigator` (and some versions a
  // `window`) — plain assignment throws "has only a getter"; redefine
  // instead so each scenario gets a fresh, writable stand-in.
  Object.defineProperty(global, "window", {{
    configurable: true,
    value: {{
      isSecureContext: true,
      ClipboardItem: opts.hasClipboardItem !== false ? FakeClipboardItem : undefined,
    }},
  }});
  global.ClipboardItem = global.window.ClipboardItem;
  Object.defineProperty(global, "navigator", {{
    configurable: true,
    value: {{ clipboard: {{ write: fakeWrite }} }},
  }});

{_wire_copy_transcript_source()}

  wireCopyTranscript();
  await _btn._handler();
  // Let any orphaned promise settle so a real unhandled rejection has a
  // chance to fire before we check the counter.
  await new Promise((resolve) => setTimeout(resolve, 20));

  return calls;
}}

(async () => {{
  const scenarios = {json.dumps(scenarios)};
  const results = {{}};
  for (const name of Object.keys(scenarios)) {{
    const before = unhandledCount;
    results[name] = await runScenario(scenarios[name]);
    results[name].unhandledRejections = unhandledCount - before;
  }}
  process.stdout.write(JSON.stringify(results));
}})().catch((err) => {{
  console.error("harness error:", err);
  process.exitCode = 1;
}});
"""
    out = subprocess.run([node, "-e", harness], capture_output=True, text=True)
    assert out.returncode == 0, f"node failed:\n{out.stderr}"
    return json.loads(out.stdout)


def test_a_rejected_clipboard_write_falls_back_and_the_toast_names_which_step_failed():
    """Before this fix, any rejection inside the `ClipboardItem` branch — a
    `NotAllowedError` from the permission gate, or an implementation that
    refuses a promise-valued entry — escaped straight to the outer `catch`,
    which unconditionally showed "Couldn't read this conversation" even
    though the transcript fetch had succeeded and the `copyTextToClipboard`
    fallback (which itself degrades to `execCommand`) was never tried."""
    results = _run_clipboard_scenarios(
        {
            # Write is denied (fetch succeeds, fallback works) — must retry
            # via copyTextToClipboard with the already-resolved markdown and
            # report success, not "couldn't read".
            "write_denied_fallback_succeeds": {
                "hasClipboardItem": True,
                "writeDeniedEarly": True,
                "fetchFails": False,
                "fallbackSucceeds": True,
            },
            # Write is denied AND the fallback itself fails — still a write
            # failure, not a fetch failure, so the toast must say so.
            "write_denied_fallback_fails": {
                "hasClipboardItem": True,
                "writeDeniedEarly": True,
                "fetchFails": False,
                "fallbackSucceeds": False,
            },
            # The transcript fetch itself fails — nothing to fall back to.
            "fetch_fails": {
                "hasClipboardItem": True,
                "writeDeniedEarly": False,
                "fetchFails": True,
            },
        }
    )

    write_ok = results["write_denied_fallback_succeeds"]
    assert write_ok["copyTextToClipboard"] == ["# transcript"], (
        "a rejected ClipboardItem write must retry via copyTextToClipboard with the "
        "already-resolved markdown, not give up"
    )
    assert write_ok["showToast"][-1] == {"text": "Transcript copied", "kind": "ok"}

    write_fail = results["write_denied_fallback_fails"]
    assert write_fail["copyTextToClipboard"] == ["# transcript"], "the fallback must still be attempted"
    assert write_fail["showToast"][-1]["text"] == "Couldn't copy to clipboard", (
        "a write failure must not be reported as a fetch failure"
    )

    fetch_fail = results["fetch_fails"]
    assert fetch_fail["copyTextToClipboard"] == [], "nothing to copy when the fetch itself failed"
    assert fetch_fail["showToast"][-1]["text"] == "Couldn't read this conversation"

    # The toast text must actually distinguish the two failure modes — this
    # is the bug: both used to say the same (wrong, for a write failure) thing.
    assert write_fail["showToast"][-1]["text"] != fetch_fail["showToast"][-1]["text"]


def test_a_synchronous_clipboard_item_failure_never_leaves_an_unhandled_rejection():
    """`new ClipboardItem({...: md.then(...)})` hands the constructor a
    *derived* promise. If the constructor (or `.write()`) throws
    synchronously before ever consuming that promise, nothing has attached a
    rejection handler to it — if the underlying fetch later fails, that
    orphaned promise becomes an unhandled rejection, independent of whatever
    the click handler's own try/catch reports. The fix must attach a
    no-op handler to it unconditionally."""
    results = _run_clipboard_scenarios(
        {
            # Constructor throws synchronously; the fetch later fails too —
            # the orphaned blob-promise must not surface as an unhandled
            # rejection, and the toast must still name the real cause.
            "ctor_throws_fetch_fails": {
                "hasClipboardItem": True,
                "ctorThrows": True,
                "fetchFails": True,
            },
            # Constructor throws synchronously but the fetch succeeds — must
            # still fall back and report success.
            "ctor_throws_fetch_succeeds": {
                "hasClipboardItem": True,
                "ctorThrows": True,
                "fetchFails": False,
                "fallbackSucceeds": True,
            },
        }
    )

    fails = results["ctor_throws_fetch_fails"]
    assert fails["unhandledRejections"] == 0, (
        "the promise handed to ClipboardItem must never become an unhandled rejection, even "
        "when the constructor throws synchronously and the fetch it wraps later fails"
    )
    assert fails["showToast"][-1]["text"] == "Couldn't read this conversation"

    succeeds = results["ctor_throws_fetch_succeeds"]
    assert succeeds["unhandledRejections"] == 0
    assert succeeds["copyTextToClipboard"] == ["# transcript"], (
        "a synchronous ClipboardItem failure must still fall back once the fetch resolves"
    )
    assert succeeds["showToast"][-1] == {"text": "Transcript copied", "kind": "ok"}


def _fetch_transcript_markdown_source() -> str:
    chat = _read(CHAT_JS)
    fn = re.search(r"async function fetchTranscriptMarkdown\(.*?\n\}\n", chat, re.DOTALL)
    assert fn, "fetchTranscriptMarkdown moved — re-point this guard"
    return fn.group(0)


def test_the_exported_title_survives_a_conversation_switch_mid_export():
    """``wireCopyTranscript`` already snapshots ``chatId`` synchronously at
    click time (see test_the_clipboard_write_is_issued_synchronously_from_
    the_click). The title beside it must be captured at that same instant —
    reading it any later leaves a window, opened by the two real `await`s in
    ``fetchTranscriptMarkdown`` (`fetch(...)` then `res.json()`), during
    which opening another conversation calls `setThreadTitle()` and repaints
    `#chat-thread-title`. Runs the real `wireCopyTranscript` +
    `fetchTranscriptMarkdown` out of chat.js and simulates exactly that race:
    the stubbed `fetch` repaints the title node before resolving, standing in
    for a user switching conversations while the export is in flight."""
    node = _node()
    harness = f"""
"use strict";
const titleNode = {{ textContent: "Old Chat" }};
const _btn = {{
  disabled: false,
  addEventListener(evt, fn) {{ if (evt === "click") this._handler = fn; }},
}};
function $(id) {{
  if (id === "chat-copy-transcript") return _btn;
  if (id === "chat-thread-title") return titleNode;
  return null;
}}
let currentChatId = "chat-old";
const calls = {{ copyTextToClipboard: [] }};
async function copyTextToClipboard(text) {{
  calls.copyTextToClipboard.push(text);
  return true;
}}
function showToast() {{}}

// No ClipboardItem / non-secure context: forces the plain fetch-then-copy
// fallback path, so the title flows straight through fetchTranscriptMarkdown
// with nothing else in between.
Object.defineProperty(global, "window", {{ configurable: true, value: {{ isSecureContext: false }} }});
Object.defineProperty(global, "navigator", {{ configurable: true, value: {{}} }});
Object.defineProperty(global, "fetch", {{
  configurable: true,
  value: async (url) => {{
    // The race: between the click firing (chatId/title captured) and this
    // response arriving, the user opens a different conversation — the real
    // setThreadTitle() repaints #chat-thread-title exactly like this.
    titleNode.textContent = "New Chat";
    return {{ ok: true, json: async () => [] }};
  }},
}});

{_fetch_transcript_markdown_source()}

{_wire_copy_transcript_source()}

(async () => {{
  wireCopyTranscript();
  await _btn._handler();
  process.stdout.write(JSON.stringify({{ md: calls.copyTextToClipboard[0] || null }}));
}})().catch((err) => {{
  console.error("harness error:", err);
  process.exitCode = 1;
}});
"""
    out = subprocess.run([node, "-e", harness], capture_output=True, text=True)
    assert out.returncode == 0, f"node failed:\n{out.stderr}"
    md = json.loads(out.stdout)["md"]
    assert md is not None, "wireCopyTranscript never reached copyTextToClipboard"
    assert md.startswith("# Old Chat\n"), (
        f"exported transcript must keep the title captured at click time, got: {md.splitlines()[0]!r}"
    )
    assert "New Chat" not in md, (
        "the title read mid-flight (after fetch/json awaits) leaked into the export — "
        "capture it synchronously alongside chatId, not off the live DOM inside "
        "fetchTranscriptMarkdown"
    )
