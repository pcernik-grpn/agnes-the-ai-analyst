# How to add Agnes-side info to your Curated Marketplace

> **This guide is for the Curated Marketplace channel only** — git-hosted
> marketplaces an Agnes admin registers in `/admin/marketplaces`.

You're maintaining a Claude Code marketplace registered as Curated in
Agnes, and you'd like the plugins in it to show with cover photos, demo
videos, and doc links inside the Agnes web UI. This guide is the whole
story.

## Quickstart

Create a JSON file at this exact path in your repo:

```
.claude-plugin/marketplace-metadata.json
```

The filename and path are both load-bearing — the parser looks for
**this exact path** and ignores anything else. Names you might be
tempted to use that **will not work**:

- `.agnes/agnes-metadata.json` — `.agnes/` is reserved for cover photo
  assets and per-request diagnostics (`version.json` in the served ZIP);
  metadata placed here is invisible to the parser.
- `marketplace-metadata.json` at the repo root — must be under
  `.claude-plugin/`.
- Any other directory — only `.claude-plugin/marketplace-metadata.json`
  is read.

Put the keys you want filled in. **Every key is optional.** Skip a
field, skip a plugin, skip the entire file — Agnes will render whatever
you provided and nothing else. Adding the file later (or expanding it)
just shows more on the next sync.

Minimal example — one plugin, one cover photo, nothing else:

```json
{
  "plugins": {
    "my-plugin": {
      "cover_photo": ".agnes/my-plugin-cover.png"
    }
  }
}
```

Done. After the next sync, the card for `my-plugin` in Agnes shows your
cover photo. Other plugins (and other fields) keep their defaults.

## The schema

The same shape applies at three levels: plugin, skill, agent. The rich
content fields (`display_name`, `tagline`, `description`, `use_cases`,
`sample_interaction`) render at all three levels. Skill/agent items
additionally accept `invocation` (literal command string for the chip on
the detail page) and `when_to_use` (markdown disambiguation block).

```json
{
  "plugins": {
    "<plugin-name>": {
      "cover_photo":    "...",
      "video_url":      "...",
      "category":       "...",

      "display_name":   "Friendly Plugin Name",
      "tagline":        "One punchy line explaining what this does.",
      "description":    "Multi-paragraph **markdown** body...",

      "use_cases": [
        {
          "title":       "Understand a service",
          "description": "Find owners, deps, tech stack.",
          "prompt":      "What does order-orchestration do?"
        }
      ],
      "sample_interaction": {
        "user":      "What does the order-orchestration service do?",
        "assistant": "The order-orchestration service is a B2B order-routing layer..."
      },

      "doc_links": [
        { "name": "Setup",   "path": "docs/setup.md" },
        { "name": "API ref", "url":  "https://example.com/api.pdf" }
      ],

      "skills": {
        "<skill-name>": {
          "cover_photo": "...",
          "video_url":   "...",
          "doc_links":   [...]
        }
      },

      "agents": {
        "<agent-name>": {
          "cover_photo": "...",
          "video_url":   "...",
          "doc_links":   [...]
        }
      }
    }
  }
}
```

**Fields, all optional:**

| Field                | What it does | Where it renders |
|----------------------|--------------|------------------|
| `cover_photo`        | Image (715 : 310 aspect recommended). | Hero window, listing card, inner-card grid. |
| `video_url`          | Demo video URL — YouTube / Vimeo / direct `.mp4`. | Detail page "Demo video" panel. |
| `category`           | Override the `marketplace.json` category. Must match Agnes vocabulary: `Code & Engineering`, `Data & Analytics`, `Documentation`, `Productivity`, `Communication`, `DevOps & Infra`, `Security`, `Research`, `Other`. | Category pill on cards + filter chips. |
| `doc_links[]`        | Optional standalone docs (PDF / MD / TXT). `{name, path}` for repo files or `{name, url}` for external. **Use only for genuine extras** (deep-dive PDFs, examples) — don't dump README / CLAUDE.md / SKILL.md here; the curated marketplace UI shows them only as downloadables, not rendered docs. | Detail page "Documentation" panel. |
| `display_name`       | Friendly plugin name (1 line, ≤ ~40 chars). | Hero h1, listing card name, mac-window titlebar label. |
| `tagline`            | Punchy value prop (1 line, ≤ ~120 chars — beyond that the listing card 2-line clamp truncates). | Hero subtitle, listing card description. |
| `description`        | Multi-paragraph markdown body. Bold, italic, lists, links, fenced code, tables, blockquotes supported. Raw HTML and inline JavaScript are stripped by the server-side sanitizer. | Detail page "What it does" panel, rendered as HTML. |
| `use_cases[]`        | Concrete usage examples. Each entry: `title` (heading), `description` (1-2 sentences), `prompt` (the literal text a user pastes into Claude Code). | Detail page "When to use it" 3-column card grid. |
| `sample_interaction` | One example dialog. `{user, assistant}` — both required; `assistant` accepts markdown (renders to safe HTML). | Detail page "Example" Claude Code-style dark Q&A panel. |
| `invocation`         | **Skill / agent only.** Literal command the user should run, e.g. `/my-plugin:tool <your question>` or `@my-agent:role`. Overrides the computed `<manifest_name>:<inner_name>` chip. Use this to add an args hint (`<your question>`) or to fix the prefix for agents (`@` instead of `/`). | "How to call it" code chip + Copy button. |
| `when_to_use`        | **Skill / agent only.** Markdown body explaining when to pick this skill/agent over a similar one. Sample: `Use this for **Confluence only**. For mixed sources, see /my-plugin:query.` | "When to use this" panel below "Example". |
| `skills`             | Map keyed by skill name (matching `name:` in the skill's `SKILL.md` frontmatter). | Skill detail page. |
| `agents`             | Map keyed by agent name (the agent `.md` filename without extension). | Agent detail page. |
| `deprecated`         | **Plugin only.** The literal boolean `true` retires the plugin: every Agnes instance consuming the repo admin-disables it on its next sync (one-way — removing the flag never auto-re-enables; an admin re-enable of a still-flagged plugin is re-disabled at the next sync). Any other value (`"true"`, `1`, …) is rejected with a warning so a typo can't retire a plugin. Non-Agnes consumers ignore the field. | DEPRECATED pill in the admin Details modal; plugin drops out of every served surface. |
| `deprecation_note`   | **Plugin only, with `deprecated`.** Single line: why it was retired. | Tooltip on the DEPRECATED pill. |
| `replacement`        | **Plugin only, with `deprecated`.** Single line: what to use instead (plugin name or short pointer). | Tooltip on the DEPRECATED pill. |

The rich-content fields (`display_name`, `tagline`, `description`,
`use_cases`, `sample_interaction`) are **read on-demand** from the working
tree at request time — curator edits to `marketplace-metadata.json` land at
the next page refresh without waiting for the next sync cycle. The visual
fields (`cover_photo`, `video_url`, `category`, `doc_links`) are persisted
into the marketplace database at sync time, because they participate in the
asset-mirror flow that needs to run once per push.

The lifecycle fields (`deprecated`, `deprecation_note`, `replacement`) sit in
both camps: the admin UI reads them on demand like the rich content, but the
**sync** is what acts on `deprecated` by admin-disabling the plugin. So a
freshly-pushed `"deprecated": true` shows its pill immediately and takes the
plugin off every served surface at the next sync (`agnes admin marketplace
sync <slug>` to do it now).

`<plugin-name>` matches the `name` field of the plugin in your
`marketplace.json`. Same for skill and agent names — they match what's
in the corresponding files inside the plugin.

## Where to put cover photos and docs

You can either ship them in your repo or link to a public URL.

**In your repo** — convention: drop them under `.agnes/` at the repo
root, then reference by path:

```json
{ "cover_photo": ".agnes/my-plugin-cover.png" }
{ "doc_links": [{ "name": "Setup", "path": "docs/setup.md" }] }
```

Files under `.agnes/` are stripped from the synthetic Claude Code
marketplace Agnes serves to user instances, so you can put
Agnes-only content there without bloating the plugin distribution.

**Public URL** — Agnes detects any value starting with `https://` (or
`http://`) and downloads it once at sync time, then serves the cached
copy:

```json
{ "cover_photo": "https://cdn.example.com/cover.png" }
{ "doc_links": [{ "name": "API ref", "url": "https://example.com/api.pdf" }] }
```

If the original URL goes 404 later, Agnes keeps showing the cached copy
it already has — link rot doesn't break your plugin's UI.

## Worked example

Copy this and adjust:

```json
{
  "plugins": {
    "data-explorer": {
      "cover_photo": ".agnes/data-explorer-cover.png",
      "video_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
      "category": "Data & Analytics",
      "doc_links": [
        { "name": "Setup guide", "path": "docs/setup.md" },
        { "name": "API reference (PDF)", "url": "https://example.com/data-explorer-api.pdf" }
      ],
      "skills": {
        "explore-table": {
          "cover_photo": ".agnes/skills/explore-table-cover.png",
          "doc_links": [
            { "name": "Cheatsheet", "path": "docs/skills/explore-table.md" }
          ]
        }
      },
      "agents": {
        "query-planner": {
          "cover_photo": "https://cdn.example.com/agents/query-planner.webp",
          "doc_links": [
            { "name": "Decision flow (PDF)", "url": "https://example.com/query-planner-flow.pdf" }
          ]
        }
      }
    },
    "report-generator": {
      "cover_photo": "https://cdn.example.com/report-generator-cover.png",
      "category": "Documentation"
    }
  }
}
```

The same example lives at
[`docs/examples/marketplace-metadata.json`](https://github.com/keboola/agnes-the-ai-analyst/blob/main/docs/examples/marketplace-metadata.json)
in the source repo.

---

## Allowed file types

Agnes accepts a small set of formats — anything else is silently skipped.

**Cover photos:** PNG (`.png`), JPEG (`.jpg` / `.jpeg`), WebP (`.webp`).

**Documentation files:** PDF (`.pdf`), Markdown (`.md` / `.markdown`),
plain text (`.txt`).
