// app/web/static/js/chat_errors.js
//
// One home for "what do we SAY when a request or a turn fails".
//
// Extracted from chat.js so three things can share it without duplicating a
// single sentence: the chat transcript, the upload dialogs, and the dev-only
// preview at /_debug/error-surfaces. A designer changing this copy sees the
// change in the preview, in chat, and in every upload dialog at once — which
// is the only way those three stay consistent.
//
// Pure functions, no DOM: importable from a page that has no chat on it, and
// runnable under plain `node` (which is how the guards in
// tests/test_chat_upstream_rate_limit_copy.py exercise them).

/** The sentences themselves, named once.
 *
 *  Three functions in this module answer overlapping questions, and each of
 *  them used to carry its own wording for the same condition — the per-user
 *  conversation cap had three different sentences across chat, the agent
 *  picker and the upload dialogs, one of which told the reader to "close"
 *  a conversation using a control that does not exist. A shared constant is
 *  what stops that recurring.
 *
 *  Written to be true in every context they render in: the transcript, an
 *  upload dialog, and a toast on a rename. That rules out naming the thing
 *  the reader was doing ("your file" is wrong on a rename), so they name the
 *  CAUSE and the way out instead. */
export const SAY = {
  // Delete is the control that frees a slot right away — DELETE
  // /sessions/{id} kills the live session before archiving it. Naming a
  // "close" action would send the reader looking for one that isn't there.
  conversationCap:
    "You already have the maximum number of conversations running. " +
    "Delete one you're finished with from the sidebar, then try again.",
  // "instance" is a deployment word; the reader has a workspace.
  monthlyBudget:
    "This workspace has used its AI budget for the month. An admin can raise it.",
  // Agnes's own per-sender throttle — the reader really is going too fast.
  senderThrottle:
    "You're sending messages faster than this workspace allows. Wait a few minutes and try again.",
  // The provider's per-minute token/request quota, or Vertex's
  // RESOURCE_EXHAUSTED. Transient by construction, and the broker has already
  // retried before anything reaches a reader.
  // "rate-limited upstream" was accurate and unreadable: upstream of WHAT,
  // to a reader who just asked about revenue? Says who is busy and whose
  // fault it is not, in words that need no diagram.
  upstreamRateLimit:
    "The AI model is busy right now — nothing is wrong on your side. " +
    "Try again in a moment.",
  // The sandbox's own client (or the broker's own outbound call) could not
  // reach its target AT ALL — connection refused, DNS failure, an abort
  // mid-connect, or a 502/503 a reverse proxy answers while the app
  // container is mid-restart. A real report read "Cannot reach sandbox
  // egress upstream at https://<host>/api/broker/anthropic: This operation
  // was aborted" verbatim — a host and an abort name nothing a reader can
  // act on. "restarting" (matched by `_TRANSIENT_RE` below) keeps this a
  // warn, not an error: nothing is broken, the reader just waits it out.
  // "workspace", not "instance" — a deployment word this module's own
  // vocabulary already avoids (see monthlyBudget above).
  instanceUnavailable: "This workspace is restarting or temporarily unavailable — try again in a minute.",
  noAccess: "You don't have access to do that.",
  serverFault: "Agnes couldn't process that. Try again in a moment.",
};

// Plain-language copy for a failed turn. Chat pasted `frame.kind` +
// `frame.message` straight into the stream, so the product's core action
// failed with "Something went wrong: engine_error — engine turn failed:
// 503: kai_integration_not_configured" — no cause a non-technical reader can
// act on, no next step, and a second truncated copy in a toast.
//
// The same error families already have written copy in
// components/builder_preview.js (`errorCopy`), which the preview surface has
// been using all along. This is that mapping, worded for chat: same families,
// same order, so the two surfaces cannot describe one failure differently.
/** One sentence for a failed fetch that is NOT a chat turn — an upload, a
 *  session create, a row action.
 *
 *  Every such call site used to end in `String(j.detail)`, which has two
 *  failure modes the reader sees: a `detail` that is an OBJECT (which every
 *  rate-limit and cap refusal is) renders as "[object Object]", and a `detail`
 *  that is a machine token renders the token. A 429 during an upload showed
 *  the first one.
 *
 *  `fallback` is the caller's own context-specific sentence, used when the
 *  server said nothing a person can read. The server's own `detail` is
 *  surfaced only when it looks like prose (it contains a space) — a bare
 *  `access_denied` is not an explanation. */
export async function requestErrorCopy(res, fallback) {
  let code = "";
  let detailText = "";
  try {
    const body = await res.json();
    const d = body && body.detail;
    if (d && typeof d === "object") {
      code = String(d.kind || d.code || "");
      detailText = String(d.hint || d.message || "");
    } else if (typeof d === "string") {
      detailText = d;
    }
  } catch (_) { /* empty or non-JSON error body */ }
  if (res.status === 429) {
    if (code === "concurrency_cap") return SAY.conversationCap;
    if (/budget/i.test(code)) return SAY.monthlyBudget;
    // Agnes's own sender throttle answers 429 too, and it is the one 429 that
    // really is about how fast the reader is going.
    if (code === "rate_limit") return SAY.senderThrottle;
    return SAY.upstreamRateLimit;
  }
  if (res.status === 403) return SAY.noAccess;
  if (res.status >= 500) return SAY.serverFault;
  if (detailText && detailText.includes(" ")) return detailText;
  return fallback;
}
/* ── Tone ────────────────────────────────────────────────────────────────
 *
 * One classifier for "is this a FAILURE or a WAIT", used by every surface.
 *
 * The distinction is the reader's, not the protocol's: red should mean
 * "something is broken and you cannot fix it", and nothing else. Most of what
 * reaches these functions is not that. A quota that refills, a cap you can
 * free, a file that is too big, a permission you do not have — none of them
 * are faults, and painting them red taught readers to distrust the product
 * over things it handled correctly.
 *
 * Every call site used to decide its own tone, and almost all of them passed
 * "error" literally: an upload dialog was red for every refusal including a
 * rate limit, and "Up to 4 attachments per message." — a limit, stated
 * calmly, nothing gone wrong — arrived in the same red as a crashed runner.
 */

// Transient or self-resolving: the reader waits, or the system is mid-restart.
// Nothing is broken and nothing needs deciding. The connection-refused/
// aborted-connect markers are the SAME family `instanceUnavailable` above
// matches on — a raw ``5\d\d`` NUMBER is deliberately left OUT of this list
// (it stays in `_FAULT_RE` below): a bare 502/503 elsewhere in a message is
// still exactly the "genuinely broken" case that regex exists for, and
// widening this one to any 5xx would repaint those red-for-a-reason
// messages too, not just a failed connection ATTEMPT.
const _TRANSIENT_RE =
  /(^|\D)429(\D|$)|rate.?limit|resource_exhausted|quota|concurrency_cap|budget|server_restarting|restarting|timeout|timed out|max_session_tokens|cannot reach|econnrefused|connection refused|operation was aborted|enotfound|getaddrinfo|network is unreachable|socket hang up/i;
// The reader's own next step, or somebody's: a smaller file, a different
// type, fewer attachments, a grant to ask for, a setting an admin has not
// filled in yet. A caution, not a fault — `not_configured` in particular is
// the product not being finished being set up, which is nobody's crash.
// "limit" and "maximum" are the safety net for a sentence we authored
// ourselves and forgot to tone (a size or count cap); the call sites that
// know their own cause should still pass the tone explicitly rather than
// leave it to a regex reading our own prose back to us.
const _ACTIONABLE_RE =
  /(^|\D)(40[0-9]|41[0-9]|42[0-9])(\D|$)|too large|too big|not allowed|unsupported|access_denied|forbidden|csrf|validation_failed|invalid|no header|\blimit\b|\bmaximum\b|\bup to\b|\baccepted\b|not_configured|no_provider|provider_unavailable|not configured/i;
// Genuinely broken, and not the reader's to fix.
const _FAULT_RE = /(^|\D)5\d\d(\D|$)|subprocess_crashed|signal \d|crashed|internal/i;

/** Shared decision. `text` is everything we know as one string; `status` is
 *  the HTTP status when there is one. Returns a `.notice` tone. */
function _tone(text, status) {
  if (typeof status === "number" && status >= 500) return "error";
  if (_TRANSIENT_RE.test(text)) return "warn";
  if (typeof status === "number" && status >= 400 && status < 500) return "warn";
  if (_FAULT_RE.test(text)) return "error";
  if (_ACTIONABLE_RE.test(text)) return "warn";
  return "error";
}

/** Tone for a failed TURN (an error frame from the runner).
 *
 *  `renderSystemNote` tinted every error frame with the danger palette, so a
 *  rate limit arrived in the transcript in the same red as a crashed runner. */
export function chatErrorTone(raw, kind) {
  return _tone(`${String(kind == null ? "" : kind)} ${String(raw == null ? "" : raw)}`);
}

/** Tone for a failed REQUEST (an upload, a rename, a session create).
 *
 *  Pass the status and, when the body carried one, the server's error code.
 *  Callers that already have a written sentence and no status — a client-side
 *  size check, say — can pass the sentence as `code`; the classifier reads
 *  everything it is given as one string. */
export function requestErrorTone(status, code) {
  return _tone(String(code == null ? "" : code), typeof status === "number" ? status : undefined);
}

export function chatErrorCopy(raw, kind) {
  const msg = String(raw == null ? "" : raw).trim();
  const k = String(kind == null ? "" : kind).trim();
  const both = `${k} ${msg}`;
  if (/not_configured|no_provider|provider_unavailable|integration/i.test(both)) {
    return "Agnes needs a chat engine to answer, and none is configured on this " +
      "instance yet. An admin sets that up — your message was not lost.";
  }
  if (/concurrency_cap/i.test(both)) {
    return SAY.conversationCap;
  }
  // Agnes's OWN sender limits (enforce_sender_limits in app/chat/manager.py),
  // delivered to the sender's own sockets only (so "you" is the reader),
  // matched on the frame's kind before the agent-budget family below: they
  // are not engine errors, so the fallback's "The engine reported:" would
  // send the reader — and whoever they ask — to the wrong place. The
  // per-conversation one is a budget of tokens billed across every turn,
  // not a context limit, so the copy must not suggest the answer was too
  // long or that the conversation should have been compacted (TCRD-291).
  if (/max_session_tokens/i.test(both)) {
    return "This conversation has reached its token budget, so it can't take another turn. " +
      "Start a new conversation to keep going. An admin can raise the per-conversation budget.";
  }
  if (/daily_budget/i.test(both)) {
    // Keyed on the SENDER (enforce_sender_limits sums the sender's own day),
    // so it is "your" cap, not the instance's.
    return "You've reached your daily spend cap on this instance. Try again tomorrow, or ask an admin to raise it.";
  }
  // Anchored on the frame's KIND, not on `both`: the provider's own
  // rate-limit errors carry the literal string "rate_limit_error" in their
  // body, and matching that against the message told a user hitting an
  // UPSTREAM quota that they were typing too fast — blaming the reader for a
  // capacity problem two systems away.
  if (/rate_limit/i.test(k)) {
    return SAY.senderThrottle;
  }
  // Agnes's own per-agent monthly budget. Keyed on its code so it stays ahead
  // of the upstream-rate-limit branch below, which a bare 429 also matches.
  if (/budget_exhausted/i.test(both) || /budget/i.test(k)) {
    return SAY.monthlyBudget;
  }
  // Upstream provider rate limiting: a per-minute token/request quota on the
  // model API, or Vertex's RESOURCE_EXHAUSTED. Transient by construction — the
  // quota refills on its own, usually within seconds, and the broker has
  // already retried before this frame was emitted. Previously a bare 429 fell
  // into the monthly-budget branch above and told the room the instance was
  // out of money for the month, which is both wrong and unfixable by the
  // reader; it is neither.
  if (/(^|\D)429(\D|$)|rate.?limit|resource_exhausted|quota/i.test(both)) {
    return SAY.upstreamRateLimit;
  }
  if (/runner_not_ready|did not become ready/i.test(both)) {
    return "The chat engine did not start in time. The first conversation after a restart " +
      "is the slow one, so trying again usually works — if it keeps failing, ask an admin " +
      "to check the chat engine.";
  }
  // Distinct from the generic "took too long" family right after this one,
  // which is about an ANSWER stalling mid-turn, not a failed connection
  // attempt — order matters here.
  if (/cannot reach|econnrefused|connection refused|operation was aborted|enotfound|getaddrinfo|network is unreachable|socket hang up|\b50[23]\b/i.test(both)) {
    return SAY.instanceUnavailable;
  }
  if (/timeout|timed out/i.test(both)) {
    return "That took too long and was stopped. Try a narrower question, or ask again.";
  }
  // Unrecognised: say plainly that it failed and keep the detail visible
  // rather than inventing a cause we do not know.
  return msg
    ? `Agnes could not finish that answer. The engine reported: ${msg}`
    : "Agnes could not finish that answer. Try again, or ask an admin to check the chat engine.";
}
