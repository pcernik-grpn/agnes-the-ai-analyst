"""Browser E2E for the authoring-agent studio pages (all four domains).

Deterministic: each domain's Create action calls its real admin endpoint
(data-packages / mcp-sources / marketplaces / memory-domains), so the assertion
never depends on the LLM. Runs against the docker-compose stack with
LOCAL_DEV_MODE (auto-admin) + fake-agent mode. Records a video per domain to
tests/e2e/_videos/.

Gated (see conftest + docker-compose.e2e.yml):
    AGNES_E2E=1            # boot the stack
    AGNES_E2E_DEV_MODE=1   # bypass auth, seed admin dev user
    AGNES_E2E_FAKE_AGENT=1 # deterministic runner (no Anthropic)
    ANTHROPIC_API_KEY=...  # any non-empty value (compose requires it)
Plus Playwright + Chromium (pip install -e '.[dev]'; playwright install chromium).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

try:
    from playwright.sync_api import Error as _PwErr
    from playwright.sync_api import sync_playwright as _spw

    _PW = True
except ImportError:  # pragma: no cover
    _spw = None  # type: ignore[assignment]
    _PwErr = Exception  # type: ignore[assignment,misc]
    _PW = False

_VIDEO_DIR = Path(__file__).parent / "_videos"

# (domain, {text-field: value}, {select-field: value})
CASES = [
    ("data-package", {"name": "E2E DP", "slug": "e2e-dp", "description": "e2e"}, {}),
    ("corporate-memory", {"name": "E2E Mem", "slug": "e2e-mem", "description": "e2e"}, {}),
    ("mcp", {"name": "e2e_mcp", "url": "https://mcp.example.com/sse"}, {"transport": "http"}),
    (
        "marketplace",
        {
            "name": "E2E MP",
            "slug": "e2e-mp",
            "url": "https://github.com/example/repo",
            "curator_name": "E2E",
            "curator_email": "e2e@example.com",
        },
        {},
    ),
    (
        "skill",
        {
            "name": "e2e-skill",
            "description": (
                "Use when exercising the studio skill builder end to end, from page load to store publish."
            ),
            "skill_md": (
                "Step one: open the page under test and confirm the layout. "
                "Step two: run the documented commands in order. "
                "Step three: verify the output matches the expected values and "
                "report any mismatch with the exact command and observed output."
            ),
        },
        {"category": "Other"},
    ),
]


@pytest.fixture
def video_ctx(docker_e2e_agnes):
    if not _PW:
        pytest.skip("playwright not installed — pip install -e '.[dev]'")
    if not os.environ.get("AGNES_E2E"):
        pytest.skip("E2E disabled — set AGNES_E2E=1")
    if not os.environ.get("AGNES_E2E_DEV_MODE"):
        pytest.skip("studio E2E needs admin — set AGNES_E2E_DEV_MODE=1")
    _VIDEO_DIR.mkdir(exist_ok=True)
    pw = _spw().start()
    try:
        browser = pw.chromium.launch()
    except _PwErr as exc:
        pw.stop()
        pytest.skip(f"chromium not installed — playwright install chromium ({exc})")
    yield browser, docker_e2e_agnes
    browser.close()
    pw.stop()


@pytest.mark.parametrize("domain,texts,selects", CASES, ids=[c[0] for c in CASES])
def test_builder_creates_entity(video_ctx, domain, texts, selects):
    browser, base = video_ctx
    ctx = browser.new_context(
        record_video_dir=str(_VIDEO_DIR),
        record_video_size={"width": 1280, "height": 800},
        viewport={"width": 1280, "height": 800},
    )
    page = ctx.new_page()
    try:
        # LOCAL_DEV_MODE auto-logs in as admin, so we land straight on the page.
        page.goto(f"{base}/admin/studio/{domain}", wait_until="domcontentloaded")
        page.wait_for_selector("#studio-create", timeout=15_000)
        for key, val in texts.items():
            page.fill(f"#studio-f-{key}", val)
        for key, val in selects.items():
            page.select_option(f"#studio-f-{key}", val)
        page.click("#studio-create")
        # The result line shows "Created: …" once the real endpoint returns 201.
        page.wait_for_selector("text=Created:", timeout=15_000)
        assert "Created:" in page.inner_text("#studio-result")
    finally:
        ctx.close()  # finalizes the .webm


def test_skill_builder_shows_advisory_lint_then_publishes(video_ctx):
    """Draft a skill with an oversized body → the first Publish click runs an
    advisory dry-run that surfaces an SL002 (bloat) finding; the second click
    ("Publish anyway") still creates the entity. Proves lint is advisory —
    findings inform but never block (#687)."""
    browser, base = video_ctx
    ctx = browser.new_context(
        record_video_dir=str(_VIDEO_DIR),
        record_video_size={"width": 1280, "height": 800},
        viewport={"width": 1280, "height": 800},
    )
    page = ctx.new_page()
    try:
        page.goto(f"{base}/admin/studio/skill", wait_until="domcontentloaded")
        page.wait_for_selector("#studio-create", timeout=15_000)
        page.fill("#studio-f-name", "e2e-bloated-skill")
        page.fill(
            "#studio-f-description",
            "Use when exercising the skill linter advisory panel end to end.",
        )
        # Well over the 8000-char SL002 bloat threshold.
        page.fill("#studio-f-skill_md", "word " * 2000)

        # First click: advisory dry-run. The lint panel appears with SL002 and
        # the button flips to "Publish anyway" — publication is NOT blocked.
        page.click("#studio-create")
        page.wait_for_selector("#studio-lint:not([hidden])", timeout=15_000)
        assert "SL002" in page.inner_text("#studio-lint-list")
        assert "Publish anyway" in page.inner_text("#studio-create")

        # Second click: publishes despite the finding.
        page.click("#studio-create")
        page.wait_for_selector("text=Created:", timeout=15_000)
        assert "Created:" in page.inner_text("#studio-result")
    finally:
        ctx.close()  # finalizes the .webm


@pytest.mark.real_llm
@pytest.mark.timeout(300)
def test_live_agent_assists_in_data_package_builder(video_ctx):
    """LIVE: a real Claude agent (docker chat sandbox, data-package-builder
    profile) answers in the builder's assistant panel. Needs
    AGNES_E2E_ANTHROPIC=1 + AGNES_E2E_DOCKER=1 (apps-runner sidecar up,
    agnes-chat-sandbox image built) + a real ANTHROPIC_API_KEY (no fake
    agent)."""
    browser, base = video_ctx
    ctx = browser.new_context(
        record_video_dir=str(_VIDEO_DIR),
        record_video_size={"width": 1280, "height": 800},
        viewport={"width": 1280, "height": 800},
    )
    page = ctx.new_page()
    try:
        page.goto(f"{base}/admin/studio/data-package", wait_until="domcontentloaded")
        page.wait_for_selector("#studio-msg", timeout=15_000)
        # The assistant panel opens a profiled chat session on load; the
        # sandbox spawn takes a few seconds before it can receive a message.
        page.wait_for_timeout(6_000)
        page.fill("#studio-msg", "Suggest a data package for finance reporting and which tables it should include.")
        page.press("#studio-msg", "Enter")
        # Wait for the real agent to stream a substantive answer into the panel.
        page.wait_for_function(
            "document.getElementById('studio-stream').textContent.replace(/\\s/g,'').length > 60",
            timeout=240_000,
        )
        text = page.inner_text("#studio-stream")
        assert len(text.strip()) > 60, text
    finally:
        ctx.close()  # finalizes the .webm


def test_admin_review_approves_a_suggestion(video_ctx):
    """Seed a suggestion via the API, then approve it from the moderation UI."""
    browser, base = video_ctx
    ctx = browser.new_context(
        record_video_dir=str(_VIDEO_DIR),
        record_video_size={"width": 1280, "height": 800},
        viewport={"width": 1280, "height": 800},
    )
    page = ctx.new_page()
    try:
        # Seed a pending suggestion through the real submit endpoint.
        seed = page.request.post(
            f"{base}/api/studio/suggestions",
            data=json.dumps({"domain": "data-package", "payload": {"name": "Reviewed", "slug": "reviewed"}}),
            headers={"Content-Type": "application/json"},
        )
        assert seed.ok, seed.status
        sid = seed.json()["id"]

        page.goto(f"{base}/admin/studio/suggestions", wait_until="domcontentloaded")
        page.wait_for_selector(f'button[data-id="{sid}"][data-act="approve"]', timeout=15_000)
        page.click(f'button[data-id="{sid}"][data-act="approve"]')
        # After approval the pending list no longer shows the card.
        page.wait_for_selector(f'button[data-id="{sid}"]', state="detached", timeout=15_000)

        # Verify via the API that it is now approved.
        resp = page.request.get(f"{base}/api/admin/authoring-suggestions?status=approved")
        assert resp.ok
        assert any(s["id"] == sid for s in resp.json())
    finally:
        ctx.close()  # finalizes the .webm
