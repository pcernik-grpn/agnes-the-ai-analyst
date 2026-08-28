# Group-scoped visibility for store entities

**Status:** proposed
**Date:** 2026-08-27
**Scope:** letting an admin share a skill, plugin or agent template with
**named groups** rather than only with everyone or no one.

## Two questions this spec does not answer

Decisions for the owner. Both change the data model, so they come first.

1. **Does group visibility replace the binary, or sit beside it?** Either
   `private | groups | everyone` as one three-state choice, or "visible to
   everyone" and "visible to these groups" as separate axes. See
   [The model](#the-model).
2. **Does an admin outside the group still see it?** Today admins see every
   entity regardless of visibility (`list_entities`: "admins and the owner see
   everything"). Group-scoping either preserves that — an admin sees all, which
   is consistent with the rest of the product's god-mode — or it does not, in
   which case "restricted to Legal" means something stronger and the admin UI
   needs a way to audit what they cannot see.

## A correction to an earlier estimate

An earlier read of this called it a five-enforcement-point change, each point a
place a missed filter would leak the artifact. **That was too pessimistic**, and
the reason matters for scoping.

There is already a **serve chokepoint** that re-evaluates entity visibility on
every read: `user_store_installs.list_for_user` JOINs `store_entities` and
filters on `visibility_status`, in both backends. Its own docstring explains
why it is built that way:

> What makes that safe is that install is not a grant: the serve chokepoint
> (`user_store_installs.list_for_user`) re-evaluates both conditions on EVERY
> read, so a bundle the review goes on to block stops being served without this
> row needing to be removed.

That has two consequences.

**Delivery is one predicate, not four.** Adding "…and the caller is in a group
this entity is shared with" at that chokepoint covers the stack, the chat
sandbox and the served bundle together, because all of them resolve installs
through it.

**Revocation is free.** Someone who installed a skill while it was public stops
being served it the moment its visibility narrows — no sweep, no stale
`user_store_installs` rows to chase. The codebase already proves the pattern
works, for a different predicate: guardrail review blocking an entity after
install uses exactly this mechanism.

What genuinely remains is **discovery**, and a miss there leaks a *listing*, not
the artifact: `list_entities` (`app/api/store.py`), the flea browse and its
`category_counts` (`app/api/marketplace.py`), and search.

## What exists today

**Visibility is a column, not a grant.** `store_entities.visibility_status` is
`hidden` (owner-only — the builder's Private) or `pending`/`approved`
(instance-wide). `list_entities` filters on it: admins and the owner see
everything, "anyone else only sees approved". There is no group dimension in any
read path.

**`STORE_ENTITY` grants exist and mean something else.** `ResourceType.
STORE_ENTITY` is real and `resource_grants` carries `(group, store_entity, id)`
triples — but `required_store_entity_ids()` reads only `requirement='required'`,
and its docstring is explicit that these are "about what they carry, not about
what they may see". Granting fans out real installs to group members
(`_fanout_required_store_entity`) and locks them; it requires
`publisher_kind='organization'` (`_reject_required_on_user_published`), so an
admin cannot conscript a colleague's personal upload into everyone's stack.

**This is the trap.** The obvious implementation — "reuse the existing grants" —
would overload one table with two incompatible meanings: *must carry* and *may
see*. An entity granted `available` to a group would then be ambiguous, and the
existing `required` rows would silently acquire a visibility meaning they were
never written with. Whatever shape this takes, **the two must stay
distinguishable**.

## The model

Recommended: **a third visibility state, not a second axis.**

`visibility_status` gains `groups`, and the set of groups lives in
`resource_grants` under a *new* requirement tier (or a new resource type) so it
cannot be confused with the `required` rows that mean "carry this". A reader
sees an entity when:

- it is `approved` (everyone), or
- it is `groups` and one of their groups is in its set, or
- it is `hidden` and they are the owner (unchanged), or
- they are an admin (subject to question 2 above).

Why a state rather than an axis: "shared with everyone **and** these three
groups" has no meaning the reader can act on, and every UI that renders it has
to explain why both are set. A single choice — nobody / these groups / everyone
— is what an admin is actually deciding.

## Where the predicate goes

**One helper, called in four places**, so the rule has a single definition:

```
visible_entity_ids_for(user) -> None | frozenset[str]      # None = unrestricted
```

- `user_store_installs.list_for_user` — **the chokepoint**. Delivery and
  revocation. Both backends; the contract test parametrizes them.
- `store.list_entities` — discovery.
- `marketplace.py`'s flea browse — discovery.
- `marketplace.py`'s `category_counts` — a count that reveals existence.

Search rides `list_entities`' filter and needs nothing of its own if it does not
build its own query; **verify that before implementing** rather than assuming.

## What to be careful about

**Counts leak.** `category_counts` returning "Legal (3)" to someone who can see
none of the three tells them something. Cheap to get right at the start,
annoying to retrofit.

**The install endpoint is a separate door.** `store.py` already guards direct
install-by-id for hidden entities ("a user with the entity_id in hand could
otherwise install directly"). Group-scoping needs the same treatment, in the
same place, or the id becomes the bypass.

**Narrowing visibility on an entity someone is mid-session with.** The
chokepoint re-evaluates per read, so the next session drops it — which is right,
but it means a skill can disappear between turns. Worth a deliberate decision
rather than a discovered one.

**The conversational builder must not widen access.** Same rule as packages:
a turn may *propose* a group set into the unsaved panel; only Save writes it.
An admin should see who they are about to expose something to before it happens.

## Phases

1. The helper + the chokepoint, with a contract test across both backends.
   Delivery and revocation work; nothing in the UI offers it yet.
2. The three discovery reads, including counts.
3. The install-by-id guard.
4. The builder's Access section grows the third state and a group picker
   (propose-only), and the Library renders "Shared with N groups" where it
   currently prints Everyone / Private.

## Open questions

- Does an agent's own scope interact with this? An agent grounded in a skill its
  *owner* can see, running for someone who cannot, is a real case and this spec
  does not cover it.
- Is `publisher_kind='organization'` a precondition for group-scoping, as it is
  for `required`? Sharing a personal upload with one team seems legitimate in a
  way that conscripting it into their stack is not.
- What does the served marketplace zip/git channel do — filter, or refuse? It is
  a per-caller build already, so filtering is natural, but it is the surface
  furthest from the chokepoint and deserves its own test.
