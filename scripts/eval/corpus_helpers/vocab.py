"""Invented consulting-shaped vocabulary for the planted proving-run corpus.

Everything here is fictional. No name below refers to a real company, real
PE firm, or real person; the shapes (SOW/deliverable/proposal language,
engagement titles, sponsor relationships) are drawn from
tests/fixtures/eval/ontology.yaml's node/edge *types*, never from its own
worked examples. (Those examples used to carry a real customer's naming;
they were scrubbed to a fictional cast, but the rule stands either way —
the generator derives from the ontology's TYPES, never from whatever names
its examples happen to use.)
"""

from __future__ import annotations

import re
import unicodedata

FIRM_NAME = "Meridian Peak Advisors"

# ── Slug helpers (ontology.yaml conventions: id = "<type>:<kebab-slug>",
#    slug matches [a-z0-9][a-z0-9-]*) ───────────────────────────────────

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Czech letters that don't fully decompose to base+combining-mark under NFKD
# (ř, ě, and a couple of others survive NFKD as a single codepoint) — the
# AN2 fixture plants "Tomáš Padrák" so these need an explicit map.
_EXTRA_TRANSLITERATION = str.maketrans({"ř": "r", "Ř": "R", "ě": "e", "Ě": "E", "ů": "u", "Ů": "U"})


def _transliterate(name: str) -> str:
    """Best-effort ASCII transliteration so diacritics don't collapse a
    slug's meaning into a run of hyphens (e.g. "Tomáš" -> "tomas", not
    "tom-")."""
    s = name.translate(_EXTRA_TRANSLITERATION)
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def slugify(name: str) -> str:
    """Lowercase kebab-case slug matching the ontology's id grammar."""
    s = _transliterate(name).lower().replace("&", "and")
    s = _NON_ALNUM.sub("-", s).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return s or "x"


def node_id(node_type: str, name: str) -> str:
    return f"{node_type}:{slugify(name)}"


# ── Filler vocabulary (never used for planted/ground-truthed facts) ────
# Deliberately disjoint from the planted-reserved names in planted.py so
# filler content can never accidentally corroborate (or contradict) a
# planted fixture.

FILLER_CLIENTS = [
    "Brackenfell Logistics",
    "Tallowmere Group",
    "Quillstone Materials",
    "Verglas Robotics",
    "Ashwick Dairy Co-op",
    "Fenrose Packaging",
    "Larchmont Aerospace",
    "Woldbury Chemicals",
    "Sablecraft Furniture",
    "Northgale Utilities",
    "Emberline Foods",
    "Cragmoor Insurance",
    "Thistledown Retail",
    "Pallisade Energy",
    "Drystone Textiles",
    "Marrowgate Pharma",
    "Fallowbrook Freight",
    "Kettlewell Beverages",
    "Stonehaven Building Products",
    "Windrush Apparel",
]

FILLER_SPONSORS = [
    "Northbay Capital Partners",
    "Ferrowick Holdings",
    "Silverline Equity",
    "Copperfield Growth Partners",
    "Ashgrove Capital",
    "Millhaven Partners",
    "Corven Ridge Capital",
    "Bellcastle Equity",
]

FILLER_INDUSTRIES = [
    ("Industrial Manufacturing", "Manufacturing"),
    ("Consumer Packaged Goods", None),
    ("Specialty Chemicals", "Manufacturing"),
    ("Aerospace & Defense", "Manufacturing"),
    ("Insurance", None),
    ("Utilities", None),
    ("Retail", None),
    ("Pharmaceuticals", None),
    ("Freight & Logistics", "Logistics"),
    ("Textiles", "Manufacturing"),
]

FILLER_SERVICE_OFFERINGS = [
    "AI Value Backlog",
    "Sell-side IT Due Diligence",
    "Buy-side IT Due Diligence",
    "Data Platform Build",
    "ERP Rollout",
    "Post-merger IT Integration",
    "Cost Reduction Assessment",
    "Digital Maturity Assessment",
]

FILLER_FIRST_NAMES = [
    "Marlowe",
    "Desmond",
    "Petra",
    "Talia",
    "Yusuf",
    "Renata",
    "Callum",
    "Odette",
    "Bram",
    "Sioned",
    "Idris",
    "Marisol",
    "Tobias",
    "Ingrid",
    "Kwame",
    "Nadia",
    "Soren",
    "Ines",
    "Declan",
    "Aiko",
]

FILLER_LAST_NAMES = [
    "Hartwell",
    "Ondrusek",
    "Beaumaris",
    "Falkner",
    "Vasquez-Ortiz",
    "Whitlock",
    "Marchetti",
    "Okonjo",
    "Reznik",
    "Castellane",
    "Brix",
    "Lindqvist",
    "Abara",
    "Torvald",
    "Guimaraes",
    "Nakashima",
]

FILLER_SKILLS = [
    ("Data Architecture", "domain"),
    ("NetSuite", "tool"),
    ("Snowflake", "tool"),
    ("Change Management", "method"),
    ("M&A Carve-Out Modeling", "domain"),
    ("Process Mining", "method"),
    ("Risk Assessment", "domain"),
    ("Keboola Implementation", "tool"),
    ("Stakeholder Interviewing", "method"),
    ("Financial Modeling", "domain"),
]

FILLER_TOOLS = [
    ("Keboola", "Keboola"),
    ("NetSuite", "Oracle NetSuite"),
    ("Snowflake", "Snowflake Inc."),
    ("Power BI", "Microsoft"),
    ("Tableau", "Salesforce"),
    ("Workday", "Workday Inc."),
]

DOC_TYPES = ["sow", "deliverable", "notes", "email", "feedback", "other"]

_ENGAGEMENT_NOUNS = [
    "kickoff",
    "status update",
    "workstream summary",
    "steering committee readout",
    "close-out memo",
    "risk log",
    "scope note",
]


def filler_sentence(rng, client: str, offering: str) -> str:
    """One plausible, non-planted consulting sentence. Never asserted as a
    fact in the ground-truth graph -- filler is deliberately unmodeled."""
    templates = [
        f"{FIRM_NAME} continued the {offering} engagement with {client} this period.",
        f"The {client} team reviewed the latest {offering} findings during a working session.",
        f"No material changes to scope were logged for the {client} {offering} workstream.",
        f"A follow-up call with {client} stakeholders is scheduled to review open items.",
        f"The project team circulated an updated timeline for the {client} engagement.",
    ]
    return rng.choice(templates)


def filler_title(rng, client: str, offering: str) -> str:
    noun = rng.choice(_ENGAGEMENT_NOUNS)
    return f"{client} — {offering} {noun}"
