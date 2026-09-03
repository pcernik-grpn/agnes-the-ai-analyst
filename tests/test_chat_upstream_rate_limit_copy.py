"""What the web client says when a request or a turn is refused.

An upstream 429 (a Vertex per-minute token/request quota is the usual one) is
transient — it refills on its own, and the broker has already retried before
the client ever sees a frame. Three prior mis-mappings made it read as
something permanent and someone else's fault:

  * a bare ``429`` in the message fell into the agent-monthly-budget branch and
    announced "This instance has used its message budget for the month", which
    is wrong and unfixable by the reader,
  * the provider's own ``rate_limit_error`` body matched the check for Agnes's
    OWN sender throttle, telling the reader they were typing too fast, and
  * every upload path ended in ``String(j.detail)``, so a refusal carrying a
    structured body — every rate limit and cap among them — rendered
    "[object Object]" in the dialog.

These tests IMPORT the shipped ``chat_errors.js`` under node and call it. Not a
regex slice out of chat.js: the copy lives in its own module precisely so the
transcript, the upload dialogs, the agent picker and the
``/_debug/error-surfaces`` preview share one definition, and a guard that
re-extracted a function body would keep passing after that module stopped
being the one the app loads.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

CHAT_ERRORS_JS = Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "js" / "chat_errors.js"


def _node() -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available — the client-copy test needs a runtime")
    return node


def _run_js(body: str):
    script = f"import * as M from {json.dumps(CHAT_ERRORS_JS.as_uri())};\n{body}\n"
    out = subprocess.run([_node(), "--input-type=module", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, f"node failed:\n{out.stderr}"
    return json.loads(out.stdout)


def _run_copy(cases: list[list[str]]) -> list[str]:
    return _run_js(f"process.stdout.write(JSON.stringify({json.dumps(cases)}.map(([k, m]) => M.chatErrorCopy(m, k))));")


def _run_tone(cases: list[list[str]]) -> list[str]:
    return _run_js(f"process.stdout.write(JSON.stringify({json.dumps(cases)}.map(([k, m]) => M.chatErrorTone(m, k))));")


def _run_request_tone(cases: list[list]) -> list[str]:
    return _run_js(
        f"process.stdout.write(JSON.stringify({json.dumps(cases)}.map(([s, c]) => M.requestErrorTone(s, c))));"
    )


def _run_request(cases: list[list]) -> list[str]:
    """Drive ``requestErrorCopy`` against a stand-in for the parts of Response
    it reads, so the real function runs without a network call."""
    return _run_js(
        "const fake = (s, d) => ({status: s, json: async () => {"
        "  if (d === null) throw new Error('no body');"
        "  return {detail: d};"
        "}});"
        f"const cases = {json.dumps(cases)};"
        "const out = [];"
        "for (const [s, d, fb] of cases) out.push(await M.requestErrorCopy(fake(s, d), fb));"
        "process.stdout.write(JSON.stringify(out));"
    )


def _say() -> dict:
    return _run_js("process.stdout.write(JSON.stringify(M.SAY));")


# ---------------------------------------------------------------------------
# chatErrorCopy — a failed turn
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind,message",
    [
        # What the in-sandbox SDK raises when the broker's retries are spent.
        ("runner_exception", "Error code: 429 - {'error': {'message': 'rate_limit_error'}}"),
        # Vertex's flavour of the same thing.
        ("runner_exception", "429 RESOURCE_EXHAUSTED: Quota exceeded for base model"),
        ("engine_error", "upstream returned 429"),
    ],
)
def test_an_upstream_rate_limit_is_described_as_temporary(kind, message):
    (copy,) = _run_copy([[kind, message]])
    # Plain words: "rate-limited upstream" was accurate and unreadable
    # (upstream of what, to someone who just asked about revenue?).
    assert "busy" in copy.lower(), copy
    assert "nothing is wrong on your side" in copy.lower(), copy
    assert "moment" in copy.lower(), copy
    # The three things it must NOT say: that the money ran out, that the
    # reader did something wrong, or that Agnes itself is broken.
    assert "month" not in copy.lower(), copy
    assert "faster than" not in copy.lower(), copy
    assert "could not finish" not in copy.lower(), copy


def test_the_agent_monthly_budget_keeps_its_own_copy():
    """Regression guard on the ordering: ``budget_exhausted`` carries a 429 of
    its own, and it must still win over the upstream-rate-limit branch — that
    one really is out of budget for the month and really does need an admin."""
    (copy,) = _run_copy([["budget_exhausted", "429"]])
    assert "month" in copy, copy
    assert "busy" not in copy.lower(), copy


def test_agnes_own_sender_throttle_still_blames_nobody_else():
    (copy,) = _run_copy([["rate_limit", "Rate limit hit: 100 messages/hour."]])
    assert "faster" in copy, copy
    assert "busy" not in copy.lower(), copy


def test_the_conversation_cap_is_not_described_as_an_upstream_limit():
    """Both answer 429. Only one is something the reader can act on, and the
    actions are opposite: delete a conversation vs. wait for a quota."""
    (copy,) = _run_copy([["concurrency_cap", "429 Too Many Requests"]])
    assert "conversations running" in copy.lower(), copy
    assert "busy" not in copy.lower(), copy
    assert "month" not in copy.lower(), copy


def test_an_ordinary_engine_failure_still_falls_through():
    """The new branch must not swallow unrelated errors — a genuine engine
    fault keeps the fallback that shows the detail."""
    (copy,) = _run_copy([["engine_error", "engine turn failed: boom"]])
    assert copy == "Agnes could not finish that answer. The engine reported: engine turn failed: boom"


def test_a_number_that_merely_contains_429_is_not_a_rate_limit():
    """Digit-boundary anchoring, not a bare substring: a token count or an id
    that happens to contain 429 must not be read as a rate limit."""
    (copy,) = _run_copy([["engine_error", "engine turn failed after 14293 tokens"]])
    assert "busy" not in copy.lower(), copy


# ---------------------------------------------------------------------------
# chatErrorCopy — the broker/sandbox could not be REACHED at all, as opposed
# to a completed call the provider rejected. Written from a real report: the
# sandbox's own client failed to reach the LLM broker and the failure reached
# the reader verbatim — "Cannot reach sandbox egress upstream at
# https://<host>/api/broker/anthropic: This operation was aborted" — a host
# and an abort naming nothing a reader can act on.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind,message",
    [
        (
            "engine_error",
            "Cannot reach sandbox egress upstream at https://example.com/api/broker/anthropic: "
            "This operation was aborted",
        ),
        ("engine_error", "connect ECONNREFUSED 127.0.0.1:8000"),
        ("engine_error", "getaddrinfo ENOTFOUND kai-agent"),
        ("engine_error", "Request failed with status code 502"),
        ("engine_error", "upstream responded 503 Service Unavailable"),
    ],
)
def test_a_broker_connectivity_failure_reads_as_a_transient_wait(kind, message):
    (copy,) = _run_copy([[kind, message]])
    assert "restarting or temporarily unavailable" in copy, copy
    # The raw, unactionable phrasing must not survive into the sentence a
    # reader is asked to act on — that's the whole bug.
    assert "This operation was aborted" not in copy
    assert "/api/broker/anthropic" not in copy


def test_a_slow_answer_is_not_read_as_a_connectivity_failure():
    """The connectivity family must not swallow the OTHER meaning of a
    stalled turn — an answer that took too long is still a stopped turn, not
    an unreachable broker."""
    (copy,) = _run_copy([["", "Request timed out after 300s waiting for a response"]])
    assert "That took too long and was stopped." in copy
    assert "restarting" not in copy.lower()


@pytest.mark.parametrize(
    "kind,message",
    [
        ("engine_error", "connect ECONNREFUSED 127.0.0.1:8000"),
        ("engine_error", "getaddrinfo ENOTFOUND kai-agent"),
        (
            "engine_error",
            "Cannot reach sandbox egress upstream at https://example.com/api/broker/anthropic: "
            "This operation was aborted",
        ),
    ],
)
def test_a_broker_connectivity_failure_is_a_wait_not_a_crash(kind, message):
    """A failed CONNECTION attempt is transient the same way a restart is —
    nothing is broken, the reader just waits. A bare 502/503 elsewhere in a
    message deliberately keeps its existing `_FAULT_RE` red: that regex
    exists specifically for "genuinely broken", and widening it to any 5xx
    would repaint messages that earned that color for an unrelated reason."""
    (tone,) = _run_tone([[kind, message]])
    assert tone == "warn", tone


# ---------------------------------------------------------------------------
# chatErrorTone — red is reserved for a genuine failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind,message",
    [
        ("runner_exception", "Error code: 429 - rate_limit_error"),
        ("runner_exception", "429 RESOURCE_EXHAUSTED: Quota exceeded"),
        ("concurrency_cap", "429 Too Many Requests"),
        ("budget_exhausted", "429"),
        ("server_restarting", "gateway is restarting"),
        ("turn_idle_timeout", "turn timed out"),
        ("max_session_tokens", "budget for this conversation is spent"),
    ],
)
def test_a_wait_is_not_painted_like_a_crash(kind, message):
    """Every one of these resolves by waiting or by something the reader can
    do. ``renderSystemNote`` used to tint every error frame with the danger
    palette, so a rate limit arrived in the same red as a crashed runner."""
    (tone,) = _run_tone([[kind, message]])
    assert tone == "warn", f"{kind}: {tone}"


@pytest.mark.parametrize(
    "kind,message",
    [
        ("runner_exception", "engine turn failed: boom"),
        ("subprocess_crashed", "exited with signal 9"),
        ("engine_error", "internal engine fault"),
    ],
)
def test_a_real_failure_still_reads_as_one(kind, message):
    (tone,) = _run_tone([[kind, message]])
    assert tone == "error", f"{kind}: {tone}"


# ---------------------------------------------------------------------------
# requestErrorCopy — a failed upload / session create / row action
# ---------------------------------------------------------------------------


def test_a_structured_refusal_never_renders_as_an_object():
    """The bug this function exists for: ``String(j.detail)`` on the object body
    every rate limit and cap carries produced "[object Object]" in the dialog."""
    copies = _run_request(
        [
            [429, {"kind": "rate_limit"}, "Upload failed."],
            [429, {"kind": "concurrency_cap", "hint": "cap = 3"}, "Upload failed."],
            [429, {"code": "budget_exhausted"}, "Upload failed."],
            [403, {"code": "access_denied"}, "Upload failed."],
        ]
    )
    for copy in copies:
        assert "object" not in copy.lower(), copy
        assert copy != "Upload failed.", "the fallback swallowed a case we can describe"


def test_a_machine_token_is_not_shown_as_an_explanation():
    """A ``detail`` of ``validation_failed`` is not a sentence. Only prose
    (something with a space in it) is surfaced; a bare token falls back to the
    caller's own context-specific line."""
    token, prose = _run_request(
        [
            [400, {"code": "validation_failed"}, "Upload failed."],
            [400, "The file has no header row.", "Upload failed."],
        ]
    )
    assert token == "Upload failed.", token
    assert prose == "The file has no header row.", prose


def test_a_missing_body_still_gets_a_sentence():
    (copy,) = _run_request([[500, None, "Upload failed."]])
    assert "Try again" in copy, copy


# ---------------------------------------------------------------------------
# one sentence per condition
# ---------------------------------------------------------------------------


def test_the_call_paths_agree_on_the_same_condition():
    """The consolidation this module exists for. The conversation cap once had
    three different sentences — chat's, the agent picker's, and the upload
    dialogs' — and one told the reader to "close" a conversation using a
    control that does not exist. Every path must resolve to one constant."""
    say = _say()
    (turn_cap,) = _run_copy([["concurrency_cap", "429"]])
    (request_cap,) = _run_request([[429, {"kind": "concurrency_cap"}, "Upload failed."]])
    assert turn_cap == say["conversationCap"] == request_cap

    (turn_budget,) = _run_copy([["budget_exhausted", "429"]])
    (request_budget,) = _run_request([[429, {"code": "budget_exhausted"}, "Upload failed."]])
    assert turn_budget == say["monthlyBudget"] == request_budget

    (turn_throttle,) = _run_copy([["rate_limit", "slow down"]])
    (request_throttle,) = _run_request([[429, {"kind": "rate_limit"}, "Upload failed."]])
    assert turn_throttle == say["senderThrottle"] == request_throttle


def test_the_agent_picker_shares_the_cap_sentence():
    """``_agentStartMessage`` in chat.js had its own wording for the same cap.
    It must reference the constant, not restate it."""
    chat_js = (CHAT_ERRORS_JS.parent / "chat.js").read_text(encoding="utf-8")
    body = chat_js[chat_js.index("function _agentStartMessage") :][:900]
    assert "SAY.conversationCap" in body, body[:400]


def test_no_shared_sentence_names_what_the_reader_was_doing():
    """These render in the transcript, in an upload dialog and in a toast on a
    rename. "nothing is wrong with your file" was true in exactly one of the
    three — it shipped on all three."""
    say = _say()
    for key, sentence in say.items():
        assert "your file" not in sentence.lower(), f"{key}: {sentence}"
        assert "upload" not in sentence.lower(), f"{key}: {sentence}"


# ---------------------------------------------------------------------------
# requestErrorTone — red means broken, and only broken
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,code",
    [
        (429, "rate_limit"),  # a quota that refills
        (429, "concurrency_cap"),  # a slot the reader can free
        (429, "budget_exhausted"),  # a limit, not a fault
        (403, "access_denied"),  # a grant to ask for
        (413, ""),  # a smaller file
        (415, ""),  # a different file
        (400, "validation_failed"),  # a bundle to fix
        (409, ""),  # a name already taken
    ],
)
def test_a_refusal_the_reader_can_act_on_is_not_red(status, code):
    """None of these is a fault. Every call site used to pass "error"
    literally, so an upload dialog was red for a rate limit and a toast was
    red for "Up to 4 attachments per message." — a limit, stated calmly, with
    nothing gone wrong."""
    (tone,) = _run_request_tone([[status, code]])
    assert tone == "warn", f"{status} {code}: {tone}"


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_a_server_fault_is_red(status):
    (tone,) = _run_request_tone([[status, ""]])
    assert tone == "error", f"{status}: {tone}"


@pytest.mark.parametrize(
    "sentence",
    [
        "Up to 4 attachments per message.",
        "That file is over the 20 MB attachment limit.",
        "File is too large — max 20 MB per upload.",
        "Only .zip or .skill files are accepted for store submissions.",
    ],
)
def test_a_stated_limit_is_not_red_even_with_no_status(sentence):
    """The safety net for copy we authored ourselves: a client-side size or
    count check has no response to classify, and these are cautions."""
    (tone,) = _run_request_tone([[None, sentence]])
    assert tone == "warn", f"{sentence}: {tone}"


def test_a_missing_setup_is_not_a_crash():
    """`not_configured` means an admin has not finished setting the product
    up. The reader cannot fix it, but nothing is broken — and the copy already
    reassures them their message survived, which red contradicts."""
    (tone,) = _run_tone([["not_configured", "no chat provider is configured"]])
    assert tone == "warn", tone


def test_no_shared_sentence_uses_deployment_vocabulary():
    """ "instance" is a word for whoever runs the deployment; the reader has a
    workspace. Same for "upstream", which was accurate and unreadable."""
    say = _say()
    for key, sentence in say.items():
        low = sentence.lower()
        assert "instance" not in low, f"{key}: {sentence}"
        assert "upstream" not in low, f"{key}: {sentence}"
