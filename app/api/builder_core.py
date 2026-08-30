"""What every builder turn has in common.

Three endpoints — ``entity_builder`` (Library items), ``agent_builder``
(agent profiles) and ``package_builder`` (data packages) — each used to
re-implement the same six things: the transcript model, the caps, the stub
gate, the LLM call, the ``{reply, patch, suggestions}`` contract, and the
prompt scaffolding around a draft. Three copies of one idea, already drifting
on whether a turn applies its patch. Anything added to how a builder behaves
had to be added three times, which meant it was added once and drifted twice.

This module owns the parts that are genuinely the same. What stays with each
adapter is what is genuinely per-type: the patchable field list, where its
candidates come from, what the thing IS in the words the model reasons about,
its slots, and its sanitizer. The sanitizer in particular is deliberately NOT
generalized — model output is untrusted input, and a per-type validator that
you can read in one screen is worth more than a shared one you have to
parameterize.

Two behaviours are new here rather than merely moved:

**The engine is named.** ``stub_enabled()`` used to be
``LOCAL_DEV_MODE == "1" or TESTING == "1"``, which meant a developer with a
real credential configured still got a scripted string-slicer, and the
response was shaped identically to a real turn so nothing on screen said so.
Every judgement about builder quality made on a local instance was made
against the stub. Now the stub has its own flag, and every turn reports which
engine answered it.

**The interview is explicit.** A builder used to open with a paragraph the
page hardcoded and then wait for the author to describe the whole artifact.
Nothing modelled what was still unknown, so the model had to guess what to
ask — and its prompt told it to ask at most one question, and only if the
answer would change the configuration. :class:`Slot` makes "what does this
thing still need, in what order" a declared, testable property of each type,
computed from the draft on every turn and handed to the model as the turn's
job.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

#: Transcript cap. Long enough for a real design conversation, short enough
#: that one turn cannot be made arbitrarily expensive by a client that keeps
#: appending to ``history``.
MAX_HISTORY = 40
MAX_MESSAGE_CHARS = 4000


class BuilderMessage(BaseModel):
    """One turn of the transcript, as the page replays it.

    ``text`` is TRUNCATED rather than rejected. It used to carry
    ``max_length``, which is right for a field the author types and wrong for
    one the page replays: a single reply over the cap made every later turn in
    that conversation 422 — a conversation with no way out, and the page had
    no idea why. The author's own message is still capped where it is typed
    (``MAX_MESSAGE_CHARS`` on each builder's request model); this cap only
    bounds what the transcript costs to resend.
    """

    role: str = Field(max_length=16)
    text: str = Field(default="")

    @field_validator("text", mode="before")
    @classmethod
    def _clip(cls, v: Any) -> Any:
        if isinstance(v, str) and len(v) > MAX_MESSAGE_CHARS:
            return v[:MAX_MESSAGE_CHARS]
        return v


# ---------------------------------------------------------------------------
# Which engine answers
# ---------------------------------------------------------------------------

#: Wire values for the ``engine`` field every turn returns. The page renders
#: ``stub`` as a standing notice: a degraded mode that looks identical to the
#: real one is a lie the product tells every day someone runs it locally.
ENGINE_STUB = "stub"
ENGINE_MODEL = "model"


def stub_enabled() -> bool:
    """Whether this process answers builder turns with the scripted stub.

    ``TESTING`` forces it — a test must never reach the network. Otherwise it
    is opt-in via ``AGNES_BUILDER_STUB``, which is the change from the old
    behaviour: ``LOCAL_DEV_MODE`` alone used to imply the stub, and
    ``LOCAL_DEV_MODE`` is also what provides local auto-auth, so there was no
    way to run a local instance with auth AND a real turn.
    """
    if os.getenv("TESTING") == "1":
        return True
    return os.getenv("AGNES_BUILDER_STUB") == "1"


# ---------------------------------------------------------------------------
# The interview
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Slot:
    """One thing a builder needs to know before what it is building is done.

    ``key``    stable id, reported to the page so progress survives a reload.
    ``label``  what the reader calls it ("the trigger", "who it is for").
    ``known``  reads the draft and says whether this is settled yet.
    ``ask``    what the model should find out, in the words it should reason
               about. Not a script — a turn that can INFER a slot well should
               fill it and say what it assumed rather than asking.

    Order matters: slots are declared in the order they unblock the most, and
    the prompt names the first open one as this turn's job. That ordering is
    the difference between a builder that leads and one that waits.
    """

    key: str
    label: str
    known: Callable[[Dict[str, Any]], bool]
    ask: str


def open_slots(slots: Sequence[Slot], draft: Dict[str, Any]) -> List[Slot]:
    """The slots this draft has not settled, in declared order."""
    out: List[Slot] = []
    for slot in slots:
        try:
            settled = bool(slot.known(draft))
        except Exception:  # a predicate must never be able to fail a turn
            logger.exception("builder slot predicate %r raised; treating as open", slot.key)
            settled = False
        if not settled:
            out.append(slot)
    return out


def slots_payload(slots: Sequence[Slot], draft: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The whole slot list with its state, for the panel's progress line.

    The page renders "4 of 6 known" from this rather than re-deriving the
    predicates in JavaScript, where they would drift from the ones the prompt
    is built out of.
    """
    still_open = {s.key for s in open_slots(slots, draft)}
    return [{"key": s.key, "label": s.label, "known": s.key not in still_open} for s in slots]


def slots_prompt_section(slots: Sequence[Slot], draft: Dict[str, Any]) -> List[str]:
    """Prompt lines naming what is settled, what is not, and the turn's job.

    Returns ``[]`` when a type declares no slots, so an adapter can adopt the
    core before it has thought about its interview.
    """
    if not slots:
        return []
    still_open = open_slots(slots, draft)
    lines: List[str] = [""]
    settled = [s for s in slots if s not in still_open]
    if settled:
        lines.append("Already settled: " + ", ".join(s.label for s in settled) + ".")
    if not still_open:
        lines.append(
            "Nothing is missing. Do not open a new question — say what you would "
            "improve, or that it looks ready to save."
        )
        return lines
    lines.append("Still open, in the order they matter:")
    for slot in still_open:
        lines.append(f"- {slot.label}: {slot.ask}")
    lines.append("")
    lines.append(
        f"YOUR JOB THIS TURN: settle “{still_open[0].label}”. Fill it in if you "
        "can infer it well from what you have — then say what you assumed, so it "
        "can be corrected. Ask about it only if guessing would waste their time. "
        "Either way, leave the panel further along than you found it: a turn that "
        "only asks a question is a turn that made the author do the work."
    )
    return lines


# ---------------------------------------------------------------------------
# The opening turn
# ---------------------------------------------------------------------------

#: A turn may carry no message when it is the FIRST one — that is the builder
#: opening the conversation rather than answering. Each page used to hardcode
#: an opening paragraph, which cost nothing and never failed, but also could
#: not mention what this instance actually has or what the first question is.
OPENING_JOB = (
    "The author has just opened the builder and has not said anything yet. "
    "Open the conversation yourself: one sentence on what you two are about to "
    "make, then settle the first open slot above — ask about it, or propose "
    "something concrete for it and invite a correction. Do not greet at length "
    "and do not list every field."
)


def is_opening_turn(message: str, history: Sequence[BuilderMessage]) -> bool:
    """Whether this turn is the builder speaking first.

    Only ever true with an empty transcript: a later empty message is a client
    bug, and answering it would let a page burn tokens on whitespace.
    """
    return not message.strip() and not history


def panel_prompt_section(
    fields: Sequence[str], draft: Dict[str, Any], *, truncate: Optional[Dict[str, int]] = None
) -> List[str]:
    """Prompt lines showing what the author's panel currently reads.

    ``truncate`` caps individual fields (a skill body is a document, and the
    whole of it in every prompt is waste).
    """
    caps = truncate or {}
    lines = ["The panel currently reads:"]
    for key in fields:
        raw = draft.get(key)
        value = raw if isinstance(raw, str) else ("" if raw is None else str(raw))
        value = value.strip()
        cap = caps.get(key)
        if cap and len(value) > cap:
            value = value[:cap] + "\n…(truncated)"
        lines.append(f"- {key}: {value or '(empty)'}")
    return lines


def history_prompt_section(history: Sequence[BuilderMessage]) -> List[str]:
    """Prompt lines replaying the transcript, capped at ``MAX_HISTORY``."""
    if not history:
        return []
    lines = ["", "The conversation so far:"]
    for m in history[-MAX_HISTORY:]:
        who = "Author" if m.role == "user" else "You"
        lines.append(f"{who}: {m.text}")
    return lines


#: Wording every adapter puts on its ``suggestions`` field. Spelled out here
#: because getting it wrong produces a specific, absurd failure: the model
#: emits the question IT is about to ask, the author clicks it, and the builder
#: answers its own question back at them.
SUGGESTIONS_DESCRIPTION = (
    "Up to three things the AUTHOR might say next — their words, their voice, "
    "answering or instructing you. Never a question you are asking them, never "
    "second-person ('what do you want…', 'who is it for?'), and never a "
    "restatement of the question in your own reply. Good: 'Just the sales "
    "team', 'Make it stricter about numbers', 'Add the invoice tables'."
)

#: Markdown emphasis the reply is not supposed to contain. Every adapter's
#: prompt says plain text, and models emit ``**bold**`` anyway — and the
#: transcript inserts the reply as TEXT (model output is never trusted as
#: HTML), so the asterisks are shown literally to the reader. Stripping the
#: markers is the fix that keeps that rule intact; rendering the markdown
#: would break it.
_EMPHASIS_RE = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", re.DOTALL)
_LEADING_MARKUP_RE = re.compile(r"^\s{0,3}(#{1,6}\s+|[-*+]\s+|\d+\.\s+)", re.MULTILINE)


def plain_reply(text: str) -> str:
    """The reply as prose, with markdown markers the contract forbids removed.

    Not a markdown renderer and not a sanitizer — the transcript still inserts
    this as text. It only stops ``**who is it for?**`` reaching a reader as
    literal asterisks.
    """
    if not isinstance(text, str) or not text:
        return ""
    out = _EMPHASIS_RE.sub(r"\2", text)
    out = _LEADING_MARKUP_RE.sub("", out)
    return out.strip()


def usable_suggestions(raw: Any, *, reply: str) -> List[str]:
    """Drop the chips that are the builder's own question, not the author's answer.

    From an observed failure: a chip reading "Who should this package go to —
    just Sales, or Sales + Finance?", which was the question the reply was
    asking. The author clicked it, and the builder answered its own question
    back at them. It happened repeatedly, because "follow-ups the author might
    say next" reads to a model as "what comes next in this conversation" — and
    what comes next, from its side, is the question.

    A chip is a thing the AUTHOR says, so the rule is: **statements only.** An
    author's next line here is an answer or an instruction ("Just the sales
    team", "Make it stricter about numbers"), never a question — and no
    heuristic reliably separates "who is this for?" (the builder's) from "which
    tables are unpackaged?" (the author's), because the difference is who is
    being addressed and the text does not carry that. The cost is losing the
    occasional legitimate author question from the MODEL's suggestions; the
    hardcoded starter chips each page ships are curated and never pass through
    here, so that kind of opener is unaffected.

    ``reply`` is taken for one reason and deliberately NOT used to filter:
    dropping chips that appear in the reply looks right and is wrong. When the
    reply enumerates the options ("Just the Sales team, or Sales + Finance?"),
    the chips "Just the sales team" and "Sales and Finance" appear in it *and
    are exactly the right answers*. Keeping the parameter documents the trap so
    the next person does not re-add the rule.
    """
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        text = plain_reply(item)
        if not text or text.rstrip().endswith("?"):
            continue
        out.append(text)
        if len(out) == 3:
            break
    return out


def turn_response(
    result: Dict[str, Any],
    *,
    patch: Dict[str, Any],
    engine: str,
    slots: Sequence[Slot],
    draft: Dict[str, Any],
    fallback_reply: str,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """The wire shape every builder turn answers with.

    ``slots`` is computed AFTER the patch has been merged into the draft by the
    caller, so the progress the page renders is the progress this turn produced
    rather than the state it started from.
    """
    reply = plain_reply(result.get("reply") if isinstance(result.get("reply"), str) else "") or fallback_reply
    body: Dict[str, Any] = {
        "reply": reply,
        "patch": patch,
        "suggestions": usable_suggestions(result.get("suggestions"), reply=reply),
        "engine": engine,
        "slots": slots_payload(slots, draft),
    }
    if extra:
        body.update(extra)
    return body


def merged_draft(draft: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """The draft as the page will hold it after applying this turn's patch."""
    out = dict(draft)
    out.update(patch or {})
    return out
