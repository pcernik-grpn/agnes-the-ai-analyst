# Token accounting -- per-arm methods

Verbatim from `eval_scoring_workbook_v0.2.xlsx`, README sheet cell C24
(under "Token cost is a separate axis") plus the Framework sheet's
token-visibility fallback table (B45:D48). FROZEN 2026-08-27 along with
the rest of the workbook -- see `README.md` in this directory for the
scope-note waiver this fixture is committed under.

## README!C24 -- concrete per-arm counting method

```
A0 — Claude, no connectors
API: Anthropic /v1/messages/count_tokens, prompt only, no tool schema needed

A1 — Claude + M365/SharePoint connector
API: Anthropic /v1/messages/count_tokens, include connector schema + retrieved document content, not just the reply

A2 — ChatGPT + SharePoint connector
If OpenAI's API supports a SharePoint connector (unconfirmed, check their docs): OpenAI's input token counting endpoint, same method as A1
If not: run manually in the app, save the full transcript, count it after the fact via the same OpenAI endpoint

A3 — Claude + M365 connector + seed pack
API: same as A1, plus seed pack content; flag any cache-hit tokens separately so it doesn't look artificially cheap

A4 — Agnes
If Agnes's outbound call to Claude is visible: Anthropic count_tokens on that payload
If not: whatever Agnes's OpenTelemetry export provides — confirm the exact mechanism with Keboola first
```

## Framework!B45:D48 -- token visibility fallback table

| Arm | Token visibility | Fallback proxy |
|---|---|---|
| A1/A3 (Claude) | Good via API; poor via consumer UI | Run via API where possible |
| A2 (ChatGPT) | Poor via UI | Turns-to-answer + documents retrieved |
| A4 (Agnes) | Should be good — OpenTelemetry logging built in | Confirm exact export method with Keboola |

See also Framework!B43: "Measure tokens-to-acceptable-answer, not tokens-per-response — count everything spent until a human would accept the output, including retrieval, retries, and clarifying turns. A wrong answer in 400 tokens was not efficient; it was cheap and useless."
