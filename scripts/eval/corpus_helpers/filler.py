"""Filler (non-planted) document generation for full-mode Run P scale.

Filler documents pad the corpus to >=1000 documents so the run measures
real extraction-quality metrics (EQ9: conflict rate per 1000 docs, orphan
rate per run) at scale. They are deliberately unmodeled in ground truth --
"Fixtures are planted, never sampled" (spec §15 intro) -- so filler content
never asserts a fact the harness would grade against; it only needs to look
like a plausible SharePoint document.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from . import vocab
from .planted import SITES, DocSpec

_START_DATE = date(2024, 6, 1)
_END_DATE = date(2026, 6, 30)
_FORMAT_WEIGHTS = [("md", 3), ("docx", 2), ("pptx", 1), ("xlsx", 2)]
_FORMAT_POOL = [fmt for fmt, weight in _FORMAT_WEIGHTS for _ in range(weight)]


def _random_date(rng: random.Random) -> date:
    span = (_END_DATE - _START_DATE).days
    return _START_DATE + timedelta(days=rng.randint(0, span))


def build_filler_docs(rng: random.Random, count: int) -> list[DocSpec]:
    """Deterministic (given `rng`) filler documents distributed across
    every site/library in the sharing plan."""
    site_library_pairs = [(site, library) for site, libraries in SITES.items() for library in libraries]

    docs: list[DocSpec] = []
    for i in range(count):
        site, library = site_library_pairs[i % len(site_library_pairs)]
        client = rng.choice(vocab.FILLER_CLIENTS)
        offering = rng.choice(vocab.FILLER_SERVICE_OFFERINGS)
        title = vocab.filler_title(rng, client, offering)
        fmt = rng.choice(_FORMAT_POOL)
        n_paragraphs = rng.randint(2, 4)
        paragraphs = [vocab.filler_sentence(rng, client, offering) for _ in range(n_paragraphs)]
        author_first = rng.choice(vocab.FILLER_FIRST_NAMES)
        author_last = rng.choice(vocab.FILLER_LAST_NAMES)
        author = f"{author_first.lower()}.{author_last.lower()}@meridianpeak.example"
        subpath = f"filler/{vocab.slugify(client)}/{vocab.slugify(title)}-{i:05d}.md"

        docs.append(
            DocSpec(
                doc_key=f"filler-{i:05d}",
                site=site,
                library=library,
                subpath=subpath,
                title=title,
                doc_type=rng.choice(["notes", "deliverable", "email", "other"]),
                document_date=_random_date(rng),
                paragraphs=paragraphs,
                requested_format=fmt,
                author=author,
                last_editor=author,
            )
        )
    return docs
