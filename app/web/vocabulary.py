"""The words the product uses for one concept, in one place.

Two nouns used to name the same thing — the set of resources a group or a
person can reach. ``bundle`` on the admin Access page, ``stack`` everywhere
else, roughly a hundred and thirty member-facing strings between them. Neither
named an entity in the system: there is no bundle table and no stack table,
only grants and subscriptions. A first-time reader had to infer both words
from the shape of the rows around them, and the page that used ``bundle``
said so in its own comments.

Both are retired in favour of saying what the thing does. A lens on the admin
page is **By resource**, the mirror of By group. A row in the Library says
what the caller's agents can do with it, which is the question they opened the
page to ask:

    Add to my agents   →   Agents can use this   →   Remove

The verbs live here rather than as literals at each site because this column
had already collected four spellings of one state, and every extra literal is
how a fifth arrives. ``tests/test_vocabulary_contract.py`` fails on a
retired phrase reaching a template.

The CLI keeps ``agnes stack add|browse|remove|list``: a command name is a
stable interface with its own compatibility promise, and the people typing it
have read the docs the words come from. That split is deliberate, not drift.
"""

from __future__ import annotations

#: The action, its resting state, and its undo — per what the row can DO.
#: One label ("Add to stack") used to sit on every row while the click posted
#: to four different endpoints meaning four different things: install a skill,
#: install a plugin, ask for a local copy of granted data, pin a collection.
#: A control has to name what it does.
ADD = "Add to my agents"
REMOVE = "Remove"

#: Resting states. They drop the possessive the action keeps: the action is a
#: sentence about you, the state is a fact about the row, and repeating "your
#: agents" on every line both clipped the cell and said nothing the lede above
#: the list had not already said.
HAS = "Agents can use this"
CAN_QUERY = "Agents can query this"

ADD_TOOLTIP = "You can reach this, but your agents cannot use it until you add it."
HAS_TOOLTIP = (
    "Your agents can use this — click to remove it. An agent with a narrowed scope still only "
    "sees what that scope allows."
)

#: Granted data is a different verb, and the difference is the whole reason
#: this is not one label. Under auto-membership a grant ALREADY put the
#: package in reach; the only thing left for the caller to decide is whether
#: `agnes pull` writes a local copy — which changes how fast their queries run
#: and nothing about what their agents can do. "Add to my agents" here would
#: claim to grant access the admin's grant already gave. The undo was already
#: worded this way ("Remove local copy"); now the two directions agree.
KEEP_LOCAL = "Keep a local copy"
DROP_LOCAL = "Remove local copy"

# --- the grant tier ------------------------------------------------------
#
#: Two API values, `required` and `available`, and two competing pairs of
#: words for them: Required/Available on the admin side, Automatic/Optional on
#: the member side, plus "Always downloaded" as a third phrasing on the detail
#: pages. Automatic/Optional wins because Available is not an opposite of
#: Required — BOTH tiers are available, both are granted, both are reachable —
#: and a member reading "Available" beside "Required" reasonably concludes the
#: second one is not, which is the confusion itself rather than a wording
#: preference.
#:
#: What the tier actually decides is narrower than any of those words suggest:
#: whether `agnes pull` keeps a local copy without the member asking. Hence
#: the sentences below, which name that and nothing else.
TIER_AUTOMATIC = "Automatic"
TIER_OPTIONAL = "Optional"
TIER_AUTOMATIC_HELP = "A local copy is always kept, and only an admin changes that."
TIER_OPTIONAL_HELP = "Reachable now; keep a local copy if you want one."

#: The API enum is NOT renamed, for the same reason `agnes stack` is not: a
#: wire value is an interface with its own compatibility promise. Only the
#: words a person reads move.
#:
#: NOR is every "Required" on screen this tier. A memory ITEM carries its own
#: ``is_required`` flag meaning **required reading** — the agent must always
#: load it — which has nothing to do with local copies and shares only the
#: word. Corporate memory's item badges, its Required filter and its
#: `statMandatory` counter all say Required about that axis and must keep
#: saying it; renaming them to Automatic would state something false about
#: downloads and lose the only word the reading axis has. This is why the
#: contract test below matches on grant-tier CONTEXT rather than on the word.

#: What the admin Access page's second lens is called. The mirror of By group:
#: one resource at a time, and which groups can reach it.
RESOURCE_LENS = "By resource"

#: Exposed to Jinja as ``words`` so a template never retypes one of these.
WORDS = {
    "add": ADD,
    "remove": REMOVE,
    "has": HAS,
    "can_query": CAN_QUERY,
    "add_tooltip": ADD_TOOLTIP,
    "has_tooltip": HAS_TOOLTIP,
    "keep_local": KEEP_LOCAL,
    "tier_automatic": TIER_AUTOMATIC,
    "tier_optional": TIER_OPTIONAL,
    "tier_automatic_help": TIER_AUTOMATIC_HELP,
    "tier_optional_help": TIER_OPTIONAL_HELP,
    "drop_local": DROP_LOCAL,
    "resource_lens": RESOURCE_LENS,
}

#: Phrases that must not reach a template again. Checked as substrings against
#: the rendered template source, case-insensitively.
RETIRED = (
    "Add to stack",
    "Add to my stack",
    "In stack",
    "In your stack",
    "Not in your stack",
    "Added to your stack",
    "Removed from your stack",
    "to your stack",
    "My Stack",
    "By bundle",
)

#: Deliberately NOT retired: "always downloaded". It was a third *label* for
#: the tier and is no longer one — it now trails the label as a description
#: ("Automatic — always downloaded"), which is the true and useful thing to
#: say. Retiring the phrase would have banned the explanation along with the
#: duplicate name.
#:
#: Nor is "Required" retired, for a sharper reason: a memory ITEM's
#: `is_required` means **required reading** — the agent must always load it —
#: a different axis that shares only the word. Corporate memory's item badges
#: and its Required filter must keep saying it. Retiring the word outright
#: would have renamed those to "Automatic" and stated something false about
#: downloads, so the grant tier is enforced by the label sites in
#: `tests/test_access_vocabulary.py`, not by a substring ban here.


def install(env) -> None:
    """Make the words reachable from any Jinja environment rendering these
    templates — the app's, and the ones macro tests build themselves.

    A macro that reads an env global renders correctly in the app and raises
    ``'words' is undefined`` in a bare ``Environment``, which is how six
    standalone macro tests broke the moment the literals moved out of the
    templates. Those tests render without FastAPI on purpose, so the fix is
    not to drag the app into them: it is to make "an environment that can
    render these templates" a thing you can ask for in one call.
    """
    env.globals["words"] = WORDS
