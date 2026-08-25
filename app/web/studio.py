"""Authoring-agent studio domains.

Each domain maps a builder page (`/admin/studio/<slug>`) to: a chat profile
(see ``app/chat/profiles.py``), the form fields the builder renders, and the
existing admin endpoint its Create action POSTs to. The page is generic — the
domain config drives the fields, the assistant profile, and the create call —
so all five authoring agents share one tested surface. Domains with
``submit_directly=True`` publish straight to their endpoint for every user
instead of routing non-admins through the moderation queue.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.store_categories import STORE_CATEGORIES


@dataclass(frozen=True)
class StudioField:
    key: str
    label: str
    type: str = "text"  # text | textarea | select
    placeholder: str = ""
    required: bool = False
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class StudioDomain:
    slug: str
    profile: str  # chat profile slug
    title: str
    subtitle: str
    endpoint: str  # admin endpoint the Create action POSTs to
    # True → the domain has its own moderation pipeline (e.g. the store's
    # guardrail + LLM review); EVERYONE posts directly to `endpoint` and the
    # authoring_suggestions queue rejects it (no _SAFE_REPLAY exists).
    submit_directly: bool = False
    fields: tuple[StudioField, ...] = field(default_factory=tuple)


_NAME = StudioField("name", "Name", required=True, placeholder="Finance — Controlling Q3")
_SLUG = StudioField("slug", "Slug", required=True, placeholder="finance-controlling-q3")
_DESC = StudioField("description", "Description", type="textarea", placeholder="What this is for.")

STUDIO_DOMAINS: dict[str, StudioDomain] = {
    "data-package": StudioDomain(
        slug="data-package",
        profile="data-package-builder",
        title="Data Package Builder",
        subtitle="Assemble a curated bundle of tables and grant it to a group.",
        endpoint="/api/admin/data-packages",
        fields=(_NAME, _SLUG, _DESC),
    ),
    "mcp": StudioDomain(
        slug="mcp",
        profile="mcp-connect",
        title="MCP Connection Builder",
        subtitle="Connect an external MCP server and grant its tools to a group.",
        endpoint="/api/admin/mcp-sources",
        fields=(
            StudioField(
                "name",
                "Name",
                required=True,
                placeholder="acme_tools",
            ),
            StudioField(
                "transport",
                "Transport",
                type="select",
                required=True,
                options=("http", "sse", "stdio"),
            ),
            StudioField("url", "URL", placeholder="https://mcp.example.com/sse"),
        ),
    ),
    "marketplace": StudioDomain(
        slug="marketplace",
        profile="marketplace-author",
        title="Marketplace Builder",
        subtitle="Register a curated marketplace (a git repo of skills/agents/plugins).",
        endpoint="/api/marketplaces",
        fields=(
            StudioField("name", "Name", required=True, placeholder="Engineering Skills"),
            StudioField("slug", "Slug", required=True, placeholder="engineering-skills"),
            StudioField("url", "Git URL", required=True, placeholder="https://github.com/org/repo"),
            StudioField("curator_name", "Curator", required=True, placeholder="Platform Team"),
            StudioField("curator_email", "Curator email", required=True, placeholder="team@example.com"),
        ),
    ),
    "corporate-memory": StudioDomain(
        slug="corporate-memory",
        profile="corporate-memory",
        title="Corporate Memory Builder",
        subtitle="Distill reusable knowledge into a memory domain granted to a group.",
        endpoint="/api/admin/memory-domains",
        # `content` seeds the domain's first knowledge item in the same POST.
        # Without it this builder created an empty container: the three
        # generic fields are name/slug/description, and the endpoint accepted
        # nothing else — so the page's own subtitle promised distilled
        # knowledge that the form had no way to carry.
        fields=(
            _NAME,
            _SLUG,
            _DESC,
            StudioField(
                "content",
                "Knowledge",
                type="textarea",
                placeholder=(
                    "The knowledge itself — what a colleague needs to know, "
                    "in your own words. Becomes this domain's first item."
                ),
            ),
        ),
    ),
    "semantic-layer": StudioDomain(
        slug="semantic-layer",
        profile="semantic-model-builder",
        title="Semantic Model Builder",
        subtitle="Author an Ossie semantic-model document — datasets, metrics, and AI context for your data.",
        endpoint="/api/semantic-models/apply",
        # The endpoint itself branches on authority (admin → applied,
        # non-admin → moderation queue), but the studio page's non-admin
        # Submit still routes through /api/studio/suggestions like every
        # other moderated domain — both roads land in the same queue.
        fields=(
            StudioField(
                "document",
                "Ossie document (YAML)",
                type="textarea",
                required=True,
                placeholder=(
                    "version: '0.2.0.dev0'\n"
                    "semantic_model:\n"
                    "  - name: my-model\n"
                    "    datasets:\n"
                    "      - name: orders\n"
                    "        source: db.public.orders\n"
                    "        fields: []\n"
                ),
            ),
            StudioField("description", "Description", type="textarea", placeholder="What this model covers."),
        ),
    ),
    "skill": StudioDomain(
        slug="skill",
        profile="skill-author",
        title="Skill Builder",
        subtitle="Author a reusable skill and publish it to the store.",
        endpoint="/api/store/entities/from-markdown",
        submit_directly=True,
        fields=(
            StudioField(
                "name",
                "Name",
                required=True,
                placeholder="quarterly-report-recipe",
            ),
            StudioField(
                "description",
                "Description",
                type="textarea",
                required=True,
                placeholder="Use when … (the trigger that tells an agent to load this skill).",
            ),
            StudioField(
                "category",
                "Category",
                type="select",
                options=tuple(["", *STORE_CATEGORIES]),
            ),
            StudioField(
                "skill_md",
                "Skill content (Markdown)",
                type="textarea",
                required=True,
                placeholder="Step-by-step instructions an AI agent should follow…",
            ),
        ),
    ),
    "agent": StudioDomain(
        slug="agent",
        profile="agent-author",
        title="Agent Builder",
        subtitle="Author a reusable Claude Code subagent and publish it to the store.",
        endpoint="/api/store/entities/from-markdown",
        submit_directly=True,
        fields=(
            StudioField(
                "name",
                "Name",
                required=True,
                placeholder="quarterly-report-reviewer",
            ),
            StudioField(
                "description",
                "Description",
                type="textarea",
                required=True,
                placeholder="Use when … (the trigger that tells Claude Code to delegate to this subagent).",
            ),
            StudioField(
                "category",
                "Category",
                type="select",
                options=tuple(["", *STORE_CATEGORIES]),
            ),
            # Field key is `skill_md` — matching the studio Skill Builder — because
            # POST /api/store/entities/from-markdown carries the Markdown body
            # under that JSON key for both `type=skill` and `type=agent` (see
            # CreateFromMarkdownBody in app/api/store.py). Other conventional
            # subagent frontmatter fields (`tools`, `model`) are left for the
            # user/assistant to add directly in the Markdown body rather than
            # exposed as separate builder fields.
            StudioField(
                "skill_md",
                "Agent content (Markdown)",
                type="textarea",
                required=True,
                placeholder="The subagent's system prompt — role, responsibilities, and how it should approach the task…",
            ),
        ),
    ),
}


def get_domain(slug: str) -> StudioDomain | None:
    return STUDIO_DOMAINS.get(slug)
