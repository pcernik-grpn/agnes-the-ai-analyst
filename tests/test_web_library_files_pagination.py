"""Web UI: /library/{slug} Files section pagination + search, and the Facts
section pager bugs the same page shares a query string with (see the
shared-contract brief for this change).

Covers:
  - identity trap: a page slice landing on exactly one row must not make a
    multi-file collection render as a single-file artifact
  - `q`/`status` filtering (Band 1: file-name substring search)
  - `f.path` rendered as a second meta line when the row carries one
  - default Files order is newest-first
  - the Facts pager preserves an active `?q=` (real bug 1) and clamps an
    out-of-range `?facts_page=` instead of rendering an empty numbered page
    (real bug 2) — exercised with a stubbed facts repo so this file needs no
    Postgres backend.
"""

from __future__ import annotations

import time

from src.repositories import corpus_files_repo, file_corpora_repo


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _new_corpus(slug: str) -> str:
    return file_corpora_repo().create(name=slug, slug=slug, description=None, created_by="admin1")


def _seed_file(corpus_id: str, filename: str, *, status: str = "pending", path: str | None = None) -> str:
    fid = corpus_files_repo().add(
        corpus_id=corpus_id,
        filename=filename,
        sha256="sha_" + filename,
        file_type="csv",
        size_bytes=10,
        storage_path=None,
        path=path,
    )
    if status != "pending":
        corpus_files_repo().set_status(fid, status=status)
    return fid


# ---------------------------------------------------------------------------
# Identity trap — the whole point of Part 1
# ---------------------------------------------------------------------------


def test_page_2_of_26_files_still_renders_as_collection(seeded_app):
    """26 files, page size 25: page 2 holds exactly ONE row. The page must
    still read as a 26-file collection, never silently promote into looking
    like a single-file artifact — `_n`/`_single` must come from the true
    total, never from the page slice."""
    corpus_id = _new_corpus("big-collection")
    for i in range(26):
        _seed_file(corpus_id, f"file-{i:02d}.csv", status="indexed")

    r = seeded_app["client"].get(
        "/library/big-collection", params={"files_page": 2}, headers=_auth(seeded_app["admin_token"])
    )
    assert r.status_code == 200, r.text
    # Collection identity: kind='library', never 'file'.
    assert 'data-kind="library"' in r.text
    assert 'data-kind="file"' not in r.text
    # The hero meta states the TRUE total, not the 1-row page slice.
    assert "26 file" in r.text
    # And the page slice itself really did render only one row.
    assert r.text.count('data-preview-file="') == 1
    # The Files pager agrees: 26 files, 2 pages, standing on page 2.
    assert "Page 2 of 2" in r.text


def test_page_1_of_26_files_is_not_mistaken_for_single_file_either(seeded_app):
    """Same collection, page 1 (25 rows) — sanity check the OTHER page."""
    corpus_id = _new_corpus("big-collection-p1")
    for i in range(26):
        _seed_file(corpus_id, f"file-{i:02d}.csv", status="indexed")

    r = seeded_app["client"].get("/library/big-collection-p1", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200, r.text
    assert 'data-kind="library"' in r.text
    assert r.text.count('data-preview-file="') == 25
    assert "Page 1 of 2" in r.text


# ---------------------------------------------------------------------------
# Band 1 search (`q`) + status filter
# ---------------------------------------------------------------------------


def test_blank_q_behaves_as_absent(seeded_app):
    corpus_id = _new_corpus("blank-q-corpus")
    _seed_file(corpus_id, "one.csv")
    _seed_file(corpus_id, "two.csv")

    plain = seeded_app["client"].get("/library/blank-q-corpus", headers=_auth(seeded_app["admin_token"]))
    blank = seeded_app["client"].get(
        "/library/blank-q-corpus", params={"q": ""}, headers=_auth(seeded_app["admin_token"])
    )
    assert plain.status_code == 200 and blank.status_code == 200
    for r in (plain, blank):
        assert "one.csv" in r.text
        assert "two.csv" in r.text
        assert "No files match this search" not in r.text
        # The "N of M" note only appears once a filter is actually active
        # (checked by the rendered element, not the bare class name — which
        # the page's own <style> block also contains).
        assert 'class="files-search__note"' not in r.text


def test_q_narrows_the_list_and_the_count_line_agrees(seeded_app):
    corpus_id = _new_corpus("search-q-corpus")
    _seed_file(corpus_id, "alpha-one.csv")
    _seed_file(corpus_id, "alpha-two.csv")
    _seed_file(corpus_id, "beta.csv")

    r = seeded_app["client"].get(
        "/library/search-q-corpus", params={"q": "alpha"}, headers=_auth(seeded_app["admin_token"])
    )
    assert r.status_code == 200, r.text
    assert "alpha-one.csv" in r.text
    assert "alpha-two.csv" in r.text
    assert "beta.csv" not in r.text
    # The count line agrees with what actually rendered: 2 matches of 3 total.
    assert "2 of 3 files" in r.text
    assert r.text.count('data-preview-file="') == 2


def test_status_filter_narrows(seeded_app):
    corpus_id = _new_corpus("status-filter-corpus")
    _seed_file(corpus_id, "idx.csv", status="indexed")
    _seed_file(corpus_id, "rej.csv", status="rejected")
    _seed_file(corpus_id, "pend.csv", status="pending")

    r = seeded_app["client"].get(
        "/library/status-filter-corpus", params={"status": "rejected"}, headers=_auth(seeded_app["admin_token"])
    )
    assert r.status_code == 200, r.text
    assert "rej.csv" in r.text
    assert "idx.csv" not in r.text
    assert "pend.csv" not in r.text
    assert 'value="rejected" selected' in r.text
    assert r.text.count('data-preview-file="') == 1


def test_path_meta_line_renders_when_file_carries_one(seeded_app):
    """A crawled/source-managed file's source folder — thrown away until
    now — renders as a second, quiet meta line."""
    corpus_id = _new_corpus("path-meta-corpus")
    _seed_file(corpus_id, "report.pdf", path="Shared Docs/Finance/2024/report.pdf")
    _seed_file(corpus_id, "plain.csv")  # no path — no second meta line for this one

    r = seeded_app["client"].get("/library/path-meta-corpus", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200, r.text
    assert "Shared Docs/Finance/2024/report.pdf" in r.text
    assert 'class="file__meta file__meta--path"' in r.text


def test_default_files_order_is_newest_first(seeded_app):
    corpus_id = _new_corpus("order-corpus")
    _seed_file(corpus_id, "older.csv")
    time.sleep(0.02)  # guarantee a distinct created_at from the row above
    _seed_file(corpus_id, "newer.csv")

    r = seeded_app["client"].get("/library/order-corpus", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200, r.text
    assert r.text.index("newer.csv") < r.text.index("older.csv")


# ---------------------------------------------------------------------------
# Facts pager — real bugs 1 & 2 (see the shared-contract brief). Exercised
# with a stubbed facts repo: the real one is Postgres-only, and these two
# bugs live entirely in the ROUTE's pagination/query-string handling, not in
# anything the real repo computes.
# ---------------------------------------------------------------------------


class _FakeFactsRepo:
    """Enough of `facts_repo()`'s surface for the /library/{slug} route:
    a fixed `total` and empty per-page rows (the pager math and the query
    string it builds don't depend on row content)."""

    def __init__(self, total: int):
        self.total = total
        self.calls: list[tuple[int, int]] = []

    def collection_facts_summary(self, user, corpus_id, *, limit, offset):
        self.calls.append((limit, offset))
        return {"total": self.total, "facts": [], "type_counts": {}}


def test_facts_pager_preserves_an_active_q(seeded_app, monkeypatch):
    """Before this change the Facts pager was a bare `?facts_page={{ n }}`
    link: it dropped every OTHER query param, so paging Facts while a Files
    search was active silently reset that search back to nothing."""
    fake = _FakeFactsRepo(total=45)  # 3 pages at page_size 20
    monkeypatch.setattr("app.web.router._facts_repo_if_available", lambda: fake)

    corpus_id = _new_corpus("facts-pager-q-corpus")
    _seed_file(corpus_id, "a.csv")

    r = seeded_app["client"].get(
        "/library/facts-pager-q-corpus", params={"q": "alpha"}, headers=_auth(seeded_app["admin_token"])
    )
    assert r.status_code == 200, r.text
    # `&` is HTML-escaped to `&amp;` in a rendered href attribute.
    assert 'href="?q=alpha&amp;facts_page=2#facts-section"' in r.text


def test_facts_page_999_clamps_to_the_last_real_page(seeded_app, monkeypatch):
    """`?facts_page=999` used to render an empty list under the heading
    "Page 999 of 3", with no link back to a real page."""
    fake = _FakeFactsRepo(total=45)  # 3 pages at page_size 20
    monkeypatch.setattr("app.web.router._facts_repo_if_available", lambda: fake)

    corpus_id = _new_corpus("facts-clamp-corpus")
    _seed_file(corpus_id, "a.csv")

    r = seeded_app["client"].get(
        "/library/facts-clamp-corpus", params={"facts_page": 999}, headers=_auth(seeded_app["admin_token"])
    )
    assert r.status_code == 200, r.text
    assert "Page 999" not in r.text
    assert "Page 3 of 3" in r.text
    # Clamped BEFORE the (only) real fetch that matters: the route re-queries
    # once it learns page 999 is out of range, landing on the real last page.
    assert fake.calls[-1] == (20, 40)
