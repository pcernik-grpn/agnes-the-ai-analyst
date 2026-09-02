"""Live check of the auto-title prompt against the real model (TCRD-290).

Skipped by default (``-m "not live"`` in pytest.ini). Run it with a first-party
key::

    ANTHROPIC_API_KEY=… .venv/bin/pytest tests/test_chat_auto_title_live.py -m live -q

or against the Vertex path an instance may run on (Google ADC must be able to
call Vertex in that project)::

    AGNES_LIVE_VERTEX_PROJECT=<gcp-project> [AGNES_LIVE_VERTEX_REGION=global] \\
        .venv/bin/pytest tests/test_chat_auto_title_live.py -m live -q

The messages are request-shaped on purpose. Sent bare as the user turn (the
pre-TCRD-290 prompt), this shape made the title model answer them — "I don't
have access to SharePoint …" — instead of naming their topic about half the
time on a real instance, and the answer-shaped guard then correctly discarded
the reply, leaving the chat "Untitled". Synthetic text: none of it is a real
user's message.
"""

from __future__ import annotations

import os

import pytest

from app.chat import auto_title

pytestmark = pytest.mark.live

_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
_VERTEX_PROJECT = os.environ.get("AGNES_LIVE_VERTEX_PROJECT", "").strip()
_VERTEX_REGION = os.environ.get("AGNES_LIVE_VERTEX_REGION", "global").strip() or "global"

REQUEST_SHAPED_MESSAGES = [
    "Search our SharePoint for the engagement letters we signed with manufacturing clients in 2024 and "
    "summarize the scope of each one.",
    "Have we done AI value backlog work for automotive-aftermarket businesses? For each one, tell me the "
    "client, the sponsor, when it was signed, and the price.",
    "Quick check, no new work: list any .pptx, .pdf and lint report files currently in your workspace "
    "(with sizes and timestamps).",
    "Build me a short internal readout deck about last month's utilization by business unit. Around 5 "
    "slides, pptx only, audience is our leadership sync.",
    "Give me an access picture of this instance: which data packages exist, which groups are granted each "
    "one, and roughly how many people are in those groups.",
    "What data about my own usage and token consumption can you query for me? Check the catalog, then show "
    "my top 3 sessions by output tokens.",
    "Look at what data is registered here and tell me which important metrics have no canonical "
    "definition in the catalog yet.",
    "We have a prospect: a PE-owned building-products manufacturer who wants a rapid AI roadmap. Draft the "
    "precedent section of the proposal — cite our closest prior engagement.",
    "List the semantic models you can see, and tell me whether a skill called semantic-layer-first is "
    "loaded in your environment.",
    "How many rows are in the projects table? Answer with just the number.",
]


def _assert_is_a_title(message: str, title: str | None) -> None:
    assert title, f"no title for {message[:70]!r} (see the auto-title log lines above for why)"
    assert not auto_title._FIRST_PERSON.search(title), f"answer-shaped title {title!r} for {message[:70]!r}"
    assert len(title.split()) <= auto_title._TITLE_MAX_WORDS


@pytest.mark.skipif(not _API_KEY, reason="ANTHROPIC_API_KEY not set")
@pytest.mark.parametrize("message", REQUEST_SHAPED_MESSAGES)
def test_request_shaped_messages_get_titles_first_party(message: str):
    _assert_is_a_title(message, auto_title._generate_title_sync(message, api_key=_API_KEY))


@pytest.mark.skipif(not _VERTEX_PROJECT, reason="AGNES_LIVE_VERTEX_PROJECT not set")
@pytest.mark.parametrize("message", REQUEST_SHAPED_MESSAGES)
def test_request_shaped_messages_get_titles_vertex(message: str):
    _assert_is_a_title(
        message,
        auto_title._generate_title_sync(message, vertex=(_VERTEX_PROJECT, _VERTEX_REGION)),
    )
