"""A send that never starts a turn must give the typed text back.

``submitUserMessage`` clears the composer synchronously (immediate feedback
while the runner boots). When ``ensureWsReady()`` then fails — chat disabled,
manager not initialized, session POST refused — the turn never began, so the
cleared draft is pure loss. Same static-source contract style as
test_chat_input_history_recall.py: no headless browser in CI.
"""

from pathlib import Path

CHAT_JS = Path("app/web/static/js/chat.js")


def _submit_body() -> str:
    js = CHAT_JS.read_text(encoding="utf-8")
    start = js.index("async function submitUserMessage")
    end = js.index('renderMessage({ role: "user", content: text });', start)
    return js[start:end]


def test_failed_send_restores_the_composer_text():
    body = _submit_body()
    assert "taFailed.value = text;" in body, (
        "the ensureWsReady() failure path must put the typed text back in "
        "#chat-input — the composer was cleared optimistically before the "
        "turn was known to start"
    )


def test_failed_send_still_reports_the_failure_and_unsettles_the_agent():
    body = _submit_body()
    assert "Could not start chat:" in body
    assert "_markConversationNotStarted();" in body
