"""The skill builder's merged save path — the one surface with no coverage.

`doSave()` in `app/web/templates/skills.html` is a HAND-MERGE of two designs that
landed independently: the LLM pre-check gate (pre-check → `commitSave` → review
banner → status polling) and this branch's two feedback channels (progress on the
button via `busy()`, refusals as a work list via `setProblems()`). The merge
deleted `setResult()`, which the gate called six times, so a naive resolution
would have thrown `ReferenceError` on every save.

Two layers, mirroring `test_admin_keboola_where_filters_builder.py`:

  1. Contract — the wiring invariants, asserted against the rendered page. Each
     one corresponds to a defect that actually happened here, so this is a
     regression net rather than a restatement of the source.
  2. Executable — the refusal mapping (`problemsFrom`) run under `node`. That is
     the logic the merge rewrote: a structured guardrail rejection has to become
     one row per real problem, attributed to the field that owns it.

What this does NOT cover, deliberately: the live round trip — pressing Save,
watching the banner poll to a terminal state. That needs a DOM and a server, and
the builder's JS is an inline IIFE with no export hook. Layer 2 reaches the pure
functions by extracting their source; it cannot reach `doSave` itself.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_TEMPLATE = Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "skills.html"


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# ─────────────────────────────── 1. contract ────────────────────────────────


def test_no_reference_to_the_deleted_result_helper(seeded_app):
    """`setResult()` is gone; nothing may still call it.

    The gate called it six times. A merge that kept those calls renders a page
    that throws on the first Save — and no test would have noticed, because the
    page still loads and the console error only appears on click.
    """
    html = seeded_app["client"].get("/skills", headers=_auth(seeded_app["admin_token"])).text
    body = html.split("<script", 1)[-1]
    assert "setResult(" not in body, "the deleted progress helper is still called"


def test_save_button_reports_in_flight_state(seeded_app):
    """The button's disabled condition must include `inFlight`.

    `syncPreview()` runs on every keystroke and recomputes it. Without the
    `inFlight` term, one keypress during the ~30s pre-check re-enabled a button
    that still read "Reviewing with AI…" and still carried `aria-busy` — a
    control claiming to be live while every click was swallowed.
    """
    html = seeded_app["client"].get("/skills", headers=_auth(seeded_app["admin_token"])).text
    assert "!(canPublish() && !reviewInProgress && !inFlight)" in html


def test_every_save_path_routes_failures_to_the_work_list(seeded_app):
    """Bundle, markdown and edit saves all land in `fail`, not in prose.

    Three `.catch(fail)` sites, one per path — the third arrived with editing a
    saved item in the builder (`?edit=<id>`), which is a save like the other
    two and must not invent its own way of reporting a refusal. `fail` is what
    turns a refusal into the alert + per-field marks; a path that skips it
    silently loses the verdict.
    """
    html = seeded_app["client"].get("/skills", headers=_auth(seeded_app["admin_token"])).text
    assert html.count(".catch(fail)") == 3


def test_progress_is_named_on_the_control_that_started_it(seeded_app):
    """The pre-check relabels the button and restores it before re-labelling.

    `done(); done = busy(...)` is the ordering that matters: `busy()` captures the
    CURRENT label as the one to restore, so re-labelling without the preceding
    `done()` would compound and leave the button reading "Reviewing with AI…"
    forever.
    """
    html = seeded_app["client"].get("/skills", headers=_auth(seeded_app["admin_token"])).text
    assert "done(); done = busy('sk-save', 'Reviewing with AI…')" in html
    assert "done(); done = busy('sk-save', 'Saving…')" in html


def test_gate_and_review_banner_survived_the_merge(seeded_app):
    """The pre-check, the banner and the polling are all still wired.

    This branch replaced the gate's messaging, not the gate. If a future conflict
    resolution drops the pre-check or the poll, the submitter goes back to an
    entity stranded at `pending_llm` with nothing on screen — the exact failure
    the gate was built to remove.
    """
    html = seeded_app["client"].get("/skills", headers=_auth(seeded_app["admin_token"])).text
    assert "include_llm: true" in html, "synchronous pre-check dropped"
    assert "precheck_verdict_token" in html, "verdict token no longer forwarded"
    assert "sk-review-banner" in html, "review banner dropped"
    assert "/status" in html and "setInterval(poll" in html, "status polling dropped"
    # The refusal channel this branch introduced, on the same page.
    assert 'id="sk-alerts"' in html, "alert host dropped"


def test_llm_verdict_is_escaped_before_it_reaches_innerhtml(seeded_app):
    """Reviewer text is model output; it must not be interpolated raw.

    `updateBanner` builds HTML from `state.title` / `state.hint`, and the hint
    carries `sub.summary` straight from the reviewer.
    """
    html = seeded_app["client"].get("/skills", headers=_auth(seeded_app["admin_token"])).text
    assert "esc(state.title)" in html
    assert "esc(state.hint)" in html


def test_no_review_banner_absent_when_guardrails_off(seeded_app, monkeypatch):
    """Guardrails OFF: publication runs on the inline tier alone and nothing
    is parked, so the "stays hidden until an admin reviews it" warning would
    be a lie here. It must not render.
    """
    monkeypatch.setattr("app.instance_config.get_guardrails_enabled", lambda: False)
    html = seeded_app["client"].get("/skills", headers=_auth(seeded_app["admin_token"])).text
    # `.sk-no-review` also names the CSS rule in `<style>`, present regardless
    # of the banner's render state — assert on the div itself, not the class.
    assert '<div class="sk-no-review"' not in html
    assert "Automated review is not available on this instance." not in html


def test_no_review_banner_present_when_guardrails_on_but_llm_not_ready(seeded_app, monkeypatch):
    """Guardrails ON, no LLM provider configured: the real create path
    fail-closes at `pending_llm` with no review scheduled, so the warning
    is accurate and must render.
    """
    monkeypatch.setattr("app.instance_config.get_guardrails_enabled", lambda: True)
    monkeypatch.setattr("app.instance_config.get_guardrails_llm_provider_ready", lambda: False)
    html = seeded_app["client"].get("/skills", headers=_auth(seeded_app["admin_token"])).text
    assert '<div class="sk-no-review"' in html
    assert "Automated review is not available on this instance." in html


# ───────────────────────────── 2. executable ─────────────────────────────────


def _extract(*names: str) -> str:
    """Pull named top-level declarations out of the template's IIFE.

    The builder is an inline IIFE with no exports, so the only way to execute its
    pure helpers is to lift their source. Brace-matched from the declaration, so
    a nested function or object literal does not truncate the capture.
    """
    src = _TEMPLATE.read_text(encoding="utf-8")
    out = []
    for name in names:
        m = re.search(r"^\s*(?:function %s\s*\(|var %s\s*=)" % (name, name), src, re.M)
        assert m, "declaration not found: %s" % name
        i = src.index("\n", m.start()) if False else m.start()
        # Walk from the declaration to its matching closing brace / statement end.
        depth, j, started = 0, i, False
        while j < len(src):
            ch = src[j]
            if ch == "{":
                depth += 1
                started = True
            elif ch == "}":
                depth -= 1
                if started and depth == 0:
                    j += 1
                    break
            elif ch == ";" and not started:
                j += 1
                break
            j += 1
        out.append(src[i:j])
    return "\n".join(out)


def _run_problems_from(err_obj: dict, source: str = "write") -> list:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available — the refusal-mapping test needs a runtime")
    helpers = _extract("OFF_SURFACE_RE", "trimHint", "headline", "issueField", "BLOCKING_PHASES", "problemsFrom")
    script = (
        # `issueField` consults the builder's current type; stub it, since the
        # mapping under test is independent of which type is selected.
        "var cfg = function () { return { source: %s }; };\n" % json.dumps(source)
        + helpers
        + "\nprocess.stdout.write(JSON.stringify(problemsFrom(%s)));\n" % json.dumps(err_obj)
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, "node failed:\n%s" % out.stderr
    return json.loads(out.stdout)


def test_structured_rejection_becomes_one_row_per_problem():
    """A guardrail rejection is a work list, attributed field by field."""
    rows = _run_problems_from(
        {
            "detail": {
                "checks": {
                    "content": {
                        "status": "fail",
                        "issues": [
                            {"code": "body_too_short", "hint": "The body is too short. Add more detail."},
                            {"code": "desc_bad", "file": "<submission>", "hint": "Describe what it does."},
                        ],
                    }
                }
            }
        }
    )
    fields = [r["field"] for r in rows]
    assert "body" in fields, "a body_too_short issue must point at the body field"
    assert "description" in fields, "a <submission> issue is this form's Description"
    # The alert shows a headline; the field slot keeps the whole hint.
    body_row = next(r for r in rows if r["field"] == "body")
    assert body_row["text"] == "The body is too short."
    assert body_row["detail"] == "The body is too short. Add more detail."


def test_advisory_phases_are_not_listed_as_blockers():
    """`quality` warnings ride along in the same payload but never refuse a save,
    so listing them would invent blockers the server never had."""
    rows = _run_problems_from(
        {"detail": {"checks": {"quality": {"status": "warn", "issues": [{"hint": "Consider examples."}]}}}}
    )
    assert not any("examples" in (r.get("text") or "") for r in rows)


def test_security_findings_never_echo_the_matched_line():
    """A static-security finding names the reason and file, not the source line —
    the matched text is the reviewer's view, not a hint for the author."""
    rows = _run_problems_from(
        {
            "detail": {
                "checks": {
                    "static_security": {
                        "status": "fail",
                        "findings": [
                            {
                                "reason": "Hardcoded credential",
                                "file": "skill.md",
                                "line": 12,
                                "match": "SECRET_TOKEN=hunter2",
                            }
                        ],
                    }
                }
            }
        }
    )
    joined = json.dumps(rows)
    assert "Hardcoded credential" in joined
    assert "skill.md" in joined
    assert "hunter2" not in joined, "the matched line must never reach the author's alert"


def test_unstructured_error_still_produces_one_row():
    """A plain message must not vanish: an unattributed row carries the lot."""
    rows = _run_problems_from({"message": "Service unavailable."})
    assert len(rows) == 1
    assert rows[0]["field"] is None
    assert "Service unavailable." in rows[0]["text"]


# ──────────────────── 3. the check path's preview response ───────────────────
#
# `doCheck()`'s bundle branch used to keep `.components` and throw the rest of
# the preview response away — which is how the manifest pre-fill (#1175) and
# the metadata verdict (#1176) both went missing despite the server sending
# them. The contract tests below are the fence around what it keeps.


def test_check_keeps_the_whole_preview_response(seeded_app):
    """`.components` alone was the bug; name/description and field_issues are
    part of the same response and part of the same answer."""
    html = seeded_app["client"].get("/skills", headers=_auth(seeded_app["admin_token"])).text
    assert "applyPreviewMeta(res)" in html, "manifest pre-fill dropped from the check path"
    assert "res.field_issues" in html, "metadata verdict dropped from the check path"


def test_check_posts_the_metadata_it_asks_to_be_checked(seeded_app):
    """The builder must keep sending the fields the preview endpoint now
    validates — a check over a payload the server never receives is the
    original defect, in the other direction."""
    html = seeded_app["client"].get("/skills", headers=_auth(seeded_app["admin_token"])).text
    for field in ("name", "description", "category"):
        assert "fd.append('%s'" % field in html, f"bundleForm() no longer sends {field}"


def test_category_is_a_markable_field(seeded_app):
    """A verdict attributed to `category` needs somewhere to land: the field
    list, a label, an error slot, and a `dropProblem` on edit."""
    html = seeded_app["client"].get("/skills", headers=_auth(seeded_app["admin_token"])).text
    assert "var PROBLEM_FIELDS = ['name', 'description', 'category', 'body', 'bundle'];" in html
    assert "fieldErrHtml('category')" in html
    assert "dropProblem('category')" in html


def _run_apply_preview_meta(draft: dict, res: dict) -> dict:
    """Execute `applyPreviewMeta` against a draft, returning draft + filled."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available — the pre-fill test needs a runtime")
    # NAME_RE is a regex literal whose `{0,63}` quantifier confuses the
    # brace-matched extractor, so lift the literal itself — still read from the
    # template, so the two cannot drift.
    src = _TEMPLATE.read_text(encoding="utf-8")
    name_re = re.search(r"^\s*var NAME_RE = (/.*/);\s*$", src, re.M)
    assert name_re, "NAME_RE declaration not found"
    script = (
        "var draft = %s;\n" % json.dumps(draft)
        + "var NAME_RE = %s;\n" % name_re.group(1)
        + _extract("applyPreviewMeta")
        + "\nvar filled = applyPreviewMeta(%s);\n" % json.dumps(res)
        + "process.stdout.write(JSON.stringify({draft: draft, filled: filled}));\n"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, "node failed:\n%s" % out.stderr
    return json.loads(out.stdout)


def test_preview_meta_fills_blank_fields():
    """The reported bug: the server returned the manifest's description and the
    Description field stayed empty."""
    got = _run_apply_preview_meta(
        {"name": "", "description": ""},
        {"name": "from-manifest", "description": "What the manifest says."},
    )
    assert got["draft"]["description"] == "What the manifest says."
    assert got["draft"]["name"] == "from-manifest"
    assert sorted(got["filled"]) == ["description", "name"]


def test_preview_meta_never_overwrites_what_the_author_typed():
    """Same contract as applyFrontmatter(): an attachment fills blanks, and an
    attachment that rewrote an identity already typed would be worse than the
    bug it fixes."""
    got = _run_apply_preview_meta(
        {"name": "my-own-name", "description": "My own words."},
        {"name": "from-manifest", "description": "What the manifest says."},
    )
    assert got["draft"]["name"] == "my-own-name"
    assert got["draft"]["description"] == "My own words."
    assert got["filled"] == []


def test_preview_meta_refuses_a_manifest_name_that_is_not_a_slug():
    """The name field is slug-shaped; a manifest may not be. Filling it with
    something the save would refuse trades one dead end for another."""
    got = _run_apply_preview_meta(
        {"name": "", "description": ""},
        {"name": "Not A Slug", "description": "Fine description."},
    )
    assert got["draft"]["name"] == ""
    assert got["filled"] == ["description"]


def test_preview_meta_tolerates_an_empty_response():
    """A preview that returned no metadata must be a no-op, not a throw."""
    assert _run_apply_preview_meta({"name": "", "description": ""}, {})["filled"] == []
