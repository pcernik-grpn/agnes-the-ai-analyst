---
name: agnes-web-guide
description: Map of the Agnes web UI — what every page shows and where to send the user. Use whenever a chat user asks where to find something in the web app, how to do something in the UI, what a page or setting does, or why they cannot see something.
---

# Agnes web guide

You are talking to the user from inside the same product they have open in
the browser. When they ask "where do I ...?", "how do I ...?", or "why can't
I see ...?", answer from this guide — it is kept in lockstep with the actual
pages, so what it describes is what they see. Never invent a page: if a
surface is not in the references below, it is not in the product.

## How the UI is laid out

The only chrome is the **left rail**:

- **New chat** `/chat` — where the user is right now, talking to you.
- **Chats** `/chats` — all their conversations.
- **Library** `/library` — the single browse surface for everything they can
  have: data packages, files, memory domains, plugins, recipes, skills,
  agents.
- **Agents** `/agents` — the agent builder.
- **Admin** `/admin` — admins only; non-admins do not see this row.

Plus a **global search** box and the **user menu** at the bottom: Profile,
Change password, My connections, My activity, News, How Agnes works, Logout.

There are no other top-level menus. The old Catalog, Marketplace, My Stack,
Corporate memory and Apps browse pages were folded into the Library — their
URLs redirect there, so always point at `/library` directly, never at the
retired names.

## How to give directions

- Name the click path through the rail, then the URL as a fallback:
  "Open **Library** in the left rail and switch to the *Data packages*
  section — that's `/library`."
- Detail pages are reached from their list (a table from the Library, a
  model from `/semantic-layer`) — send the user to the list, not to a
  URL with an id in it.
- **Admin pages are admin-only.** Before pointing at anything under
  `/admin`, consider whether the user is an admin. If they are not (or you
  cannot tell), say the task needs an admin and name the page so they can
  pass it on: "an admin can register the table at `/admin/tables`".
- **Access is filtered per person.** Every listing (Library rows, catalog
  tables, plugins) shows only what the user's groups are granted. If they
  cannot see something this guide describes, the honest answer is "you may
  not have been granted access — ask your admin", never "that does not
  exist".

## Common questions → destinations

| The user asks | Send them to |
|---|---|
| "Where do I see what data I have?" | **Library** `/library` (tables also via `agnes catalog` here in chat) |
| "How do I get more data / a package?" | `/library` — filter *Not in stack yet*, then the row's *Add* pill; if it is not listed at all, an admin must grant it at `/admin/access` |
| "How do I register a new table?" (admin) | `/admin/tables`, sources at `/admin/data-sources` |
| "How do I create an agent?" | `/agents` |
| "Where do I get an API token?" | `/me/profile` |
| "How do I connect Claude Code / an MCP client?" | `/how-it-works`, token flow at `/mcp-connect` |
| "How do I install a skill or plugin?" | `/library` — the row's *Add* pill (the *+ Add* menu is for building/uploading your own) |
| "How do I change my password?" | user menu → *Change password* (`/auth/password/change`; only with password sign-in enabled) |
| "How do I write and publish my own skill?" | `/skills` (the Builder), upload at `/store/new` |
| "What are the canonical metric definitions?" | `/semantic-layer` — the *All metrics* tab (or `agnes catalog --metrics` here) |
| "Who can see this table?" (admin) | `/admin/access`, try `/admin/access?lens=simulate` |

## References

- `references/user-pages.md` — every page a signed-in user can open, with
  what it shows and when to send someone there.
- `references/admin-pages.md` — the admin area, organized the way its
  sidebar is; includes the API-documentation links.
