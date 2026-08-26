"""The Catalog's card wording follows the data, not the key name (#1206).

After the auto-membership reshape, `in_stack` on a catalog entry
means "a local copy exists", not "in the caller's stack". The key name still
says the old thing, so the card macro read it the old way and offered "Add to
stack" on a package listed under My Stack. The projection now states the
semantics (`in_stack_is_local`) and the macro reads the flag.

A wording defect with no server behaviour to assert, so these are template-
level checks: the macro rendered against each entry shape, plus a count that
ties the flag to the number of projections re-pointing the key.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app" / "web" / "templates"
STATIC = ROOT / "app" / "web" / "static"


# ── #1206: the wording follows the flag, not the key name ───────────────────


def _render_card(**entry):
    """Render the legacy `card` macro against one entry dict.

    The macro is loaded from disk with its real filename, so this exercises the
    shipped template rather than a copy.
    """
    # FileSystemLoader rooted at the real templates dir — the macro imports
    # siblings (`macros/_trustmark.html`), so a standalone loader cannot render
    # it, and stubbing those imports would be testing a copy.
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)
    tpl = env.from_string('{% from "macros/_stack_card.html" import card %}{{ card(entry) }}')
    base = {"id": "pkg1", "name": "Sales", "description": "d", "drilldown_url": "/x"}
    base.update(entry)
    return tpl.render(entry=base)


def test_downloaded_package_says_downloaded():
    """The reported contradiction: a package under My Stack whose own button
    invited you to add it to your stack."""
    html = _render_card(in_stack=True, in_stack_is_local=True)
    assert "Downloaded" in html
    assert "Remove local copy" in html
    assert "In stack" not in html
    assert "Add to stack" not in html


def test_not_downloaded_package_offers_the_download():
    html = _render_card(in_stack=False, in_stack_is_local=True)
    assert "Download locally" in html
    assert "Add to stack" not in html


def test_required_package_says_downloaded_automatic():
    html = _render_card(in_stack=True, in_stack_is_local=True, requirement="required")
    assert "Downloaded (automatic)" in html


def test_projections_without_the_flag_keep_the_old_wording():
    """The flag is opt-in so a consumer that never re-pointed `in_stack` — it
    still means stack membership there — is untouched by this change."""
    html = _render_card(in_stack=True)
    assert "In stack" in html
    assert "Downloaded" not in html

    html = _render_card(in_stack=False)
    assert "Add to stack" in html
    assert "Download locally" not in html


def test_both_projections_declare_the_local_semantics():
    """`_data_package_entry_dict` and `_memory_domain_entry_dict` both re-point
    `in_stack` at `entry.materialized`; each must therefore ship the flag, or
    its cards read the old meaning.

    The flag is resolved, not hardcoded: `in_stack` only means "a local copy
    exists" under auto-membership. Under the classic subscribe model it means
    membership again, and a card offering "Remove local copy" for a control
    that unsubscribes tells the user they are freeing disk while they are
    giving up access. So the guard requires the resolver, and a literal `True`
    fails it.
    """
    router = (ROOT / "app" / "web" / "router.py").read_text(encoding="utf-8")
    repoints = router.count('"in_stack": getattr(entry, "materialized", False),')
    flags = router.count('"in_stack_is_local": _resolve_in_stack_is_local(in_stack_is_local),')
    assert repoints, "the re-point disappeared — revisit this guard"
    assert flags == repoints, (
        f"{repoints} projections re-point `in_stack` at the local-copy state but only "
        f"{flags} resolve the flag — the cards on the unresolved one read the old meaning (#1206)"
    )
    assert '"in_stack_is_local": True,' not in router, (
        "hardcoded True — under the classic subscribe model that turns an unsubscribe "
        "control into a 'Remove local copy' button and the user loses access"
    )


def test_classic_mode_does_not_get_the_local_copy_wording():
    """The mode fork the flag exists to respect, exercised rather than read."""
    import app.web.router as router_mod

    assert router_mod._resolve_in_stack_is_local(True) is True
    assert router_mod._resolve_in_stack_is_local(False) is False


# ---------------------------------------------------------------------------
# What a REMOVAL means also follows the flag, not just what the button says
# ---------------------------------------------------------------------------

# Every page that renders the `_stack_card` macro over a projection which can
# set `in_stack_is_local`. `catalog_legacy.html` and `corporate_memory_legacy.html`
# (the frozen pre-redesign twins this comment used to name) were retired in
# Wave 0 legacy retirement (2026-08); the redesigned Catalog
# (`catalog_unified.html`) doesn't carry this inline JS at all — it renders
# cards via the shared macro without a client-side re-labeling step — so only
# the live memory page remains in scope here.
_STACK_PAGES = ("corporate_memory.html",)


def test_the_macro_hands_the_flag_to_the_js():
    """The template alone cannot carry this — the click handler needs it too.

    `_applyStackChange` has to know whether removing means "drop the local
    copy" (stays in the stack) or "leave the stack". Without the flag in the
    DOM it can only implement one of the two.
    """
    macro = (TEMPLATES / "macros" / "_stack_card.html").read_text(encoding="utf-8")
    assert "data-in-stack-is-local=" in macro, (
        "the card must expose `in_stack_is_local` as a data attribute — the JS decides what a removal means from it"
    )


def test_removal_keeps_the_card_when_in_stack_means_local_copy():
    """Removing a local copy must not delete the My Stack card or move the tab.

    Under the local-copy meaning the server's My Stack grid comes from
    `resolver.stack()`, whose effective set is `required ∪ available ∪
    materialized` — so the item is still in the stack after its download goes
    and renders again on the next load. Deleting the card and decrementing the
    badge makes it vanish and the count drop, then both come back on refresh:
    the same contradictory-count confusion this change set out to remove,
    relocated into the optimistic path.
    """
    for page in _STACK_PAGES:
        text = (TEMPLATES / page).read_text(encoding="utf-8")
        assert "card.dataset.inStackIsLocal === '1'" in text, (
            f"{page}: `_applyStackChange` must read the flag before deciding what a removal does"
        )
        assert "if (inStackIsLocal) {" in text, f"{page}: the My Stack removal branch must be gated on the flag"
        assert "if (myEl && !inStackIsLocal) {" in text, (
            f"{page}: the My Stack tab count must not move under local-copy semantics — membership is what it counts"
        )


def test_the_toast_follows_the_flag_too():
    """The last string that still spoke membership.

    Keeping the card in My Stack (see above) is what made the old toast
    actively wrong rather than merely off-vocabulary: "Removed from stack"
    fires, then the user watches the item sit in My Stack with the count
    unchanged. "queued" rather than "downloaded" because the request that
    succeeded is a subscribe — the bytes arrive with the next `agnes pull`.
    """
    for page in _STACK_PAGES:
        text = (TEMPLATES / page).read_text(encoding="utf-8")
        assert "'Local copy queued' : 'Local copy removed'" in text, (
            f"{page}: the toast must use local-copy wording under the local-copy meaning"
        )
        assert "'Added to stack' : 'Removed from stack'" in text, (
            f"{page}: and must keep membership wording where the key still means membership"
        )


def test_a_stack_change_reapplies_the_active_filter():
    """A card that stays put must still be re-filtered.

    `applyFilters` hides a card when the Downloaded chip is on and
    `data-in-stack` is '0', and otherwise runs only from the chip / status /
    search handlers. That sufficed while a removal deleted the card; now that
    the card survives and only flips the attribute, an item that is no longer
    downloaded would remain visible under a filter that claims to exclude it.
    """
    for page in _STACK_PAGES:
        text = (TEMPLATES / page).read_text(encoding="utf-8")
        fn_start = text.index("function _applyStackChange(")
        fn_end = text.index("document.addEventListener('click'", fn_start)
        assert "applyFilters();" in text[fn_start:fn_end], (
            f"{page}: `_applyStackChange` must re-apply the active filter before it returns"
        )
