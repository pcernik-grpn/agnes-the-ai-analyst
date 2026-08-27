"""The declarative planted-content plan.

Every fixture required by spec §15.1-§15.5 (S1-S4, the traps, AN1/AN2) is
defined here as data: which document(s) it lives in, the exact sentence
planted verbatim in each, and the fact-graph claim(s) that sentence backs
(in the §7.0 producer wire format). corpus_gen.py turns this plan into
files on disk + ground_truth.json; nothing here touches the filesystem.

Site/library topology (also the sharing plan, §15.5 "divergent sharing"):

    delivery-archive/          principal + associate   (delivered work)
      active-engagements/
      completed-engagements/
      client-feedback/
    proposals-and-pursuits/
      sow-drafts/               principal only          (not yet shared out)
      won-proposals/            principal + associate
      pricing-sheets/           secret-group-c only      (S2's secret half)
    client-services/            principal + associate
      account-notes/
      escalations/
    firm-operations/
      skills-registry/          principal + associate
      tooling-notes/            principal + associate
      legal-secret/             secret-group-c only      (S1/S3/S4's secret half)

All names below are invented (see vocab.py's module docstring).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from . import vocab

GROUPS = ["principal", "associate", "secret-group-c"]

SITES: dict[str, dict[str, list[str]]] = {
    "delivery-archive": {
        "active-engagements": ["principal", "associate"],
        "completed-engagements": ["principal", "associate"],
        "client-feedback": ["principal", "associate"],
    },
    "proposals-and-pursuits": {
        "sow-drafts": ["principal"],
        "won-proposals": ["principal", "associate"],
        "pricing-sheets": ["secret-group-c"],
    },
    "client-services": {
        "account-notes": ["principal", "associate"],
        "escalations": ["principal", "associate"],
    },
    "firm-operations": {
        "skills-registry": ["principal", "associate"],
        "tooling-notes": ["principal", "associate"],
        "legal-secret": ["secret-group-c"],
    },
}

STAFF_EMAILS = {
    "Priya Shenvi": "priya.shenvi@meridianpeak.example",
    "Dax Okonkwo-Reyes": "dax.okonkwo-reyes@meridianpeak.example",
    "Tomáš Padrák": "tomas.padrak@meridianpeak.example",
}
DEFAULT_AUTHOR = "delivery-team@meridianpeak.example"


@dataclass
class DocSpec:
    doc_key: str
    site: str
    library: str
    subpath: str  # relative to the library dir, forward slashes
    title: str
    doc_type: str
    document_date: date
    paragraphs: list[str]
    requested_format: str = "md"  # md/docx/pptx/xlsx/pdf-scan
    author: str = DEFAULT_AUTHOR
    last_editor: str = DEFAULT_AUTHOR
    format_shape: str | None = None  # EQ8 hint: table-heavy/deck/czech-diacritics/scan
    trap: str | None = None
    # scan-only fields
    scan_planted_text: str | None = None
    scan_extra_lines: list[str] = field(default_factory=list)


@dataclass
class ClaimNode:
    claim_key: str
    id: str
    type: str
    attrs: dict
    doc_key: str
    quote: str


@dataclass
class ClaimEdge:
    claim_key: str
    src: str
    type: str
    dst: str
    attrs: dict
    doc_key: str
    quote: str


@dataclass
class PreparedFalseClaim:
    claim_key: str
    subject_kind: str  # "fact" | "edge"
    subject_id: str  # node id, or "src|type|dst" for an edge
    attrs: dict
    doc_key: str
    quote: str
    reason: str


@dataclass
class PlantedPlan:
    docs: list[DocSpec]
    nodes: list[ClaimNode]
    edges: list[ClaimEdge]
    prepared_false_claims: list[PreparedFalseClaim]
    s_fixtures: dict[str, dict]
    traps: dict[str, dict]
    anonymization: dict[str, dict]


def build_plan() -> PlantedPlan:
    docs: list[DocSpec] = []
    nodes: list[ClaimNode] = []
    edges: list[ClaimEdge] = []
    false_claims: list[PreparedFalseClaim] = []

    def add_doc(**kwargs) -> str:
        docs.append(DocSpec(**kwargs))
        return kwargs["doc_key"]

    def add_node(claim_key, id_, type_, attrs, doc_key, quote) -> None:
        nodes.append(ClaimNode(claim_key, id_, type_, attrs, doc_key, quote))

    def add_edge(claim_key, src, type_, dst, attrs, doc_key, quote) -> None:
        edges.append(ClaimEdge(claim_key, src, type_, dst, attrs, doc_key, quote))

    firm = vocab.FIRM_NAME
    priya = vocab.node_id("person", "Priya Shenvi")
    dax = vocab.node_id("person", "Dax Okonkwo-Reyes")
    vantable = vocab.node_id("client", "Vantable Systems")
    corundum = vocab.node_id("client", "Corundum Foods")
    osprey = vocab.node_id("client", "Osprey Fulfillment Group")
    halyard = vocab.node_id("client", "Halyard Precision Manufacturing")
    anchorstone = vocab.node_id("sponsor", "Anchorstone Capital")
    redwater = vocab.node_id("sponsor", "Redwater Partners")
    ind_mfg = vocab.node_id("industry", "Manufacturing")
    ind_food = vocab.node_id("industry", "Food Manufacturing")
    ind_logistics = vocab.node_id("industry", "Logistics")
    svc_aivb = vocab.node_id("service_offering", "AI Value Backlog")
    svc_erp = vocab.node_id("service_offering", "ERP Rollout")
    skill_data_arch = vocab.node_id("skill", "Data Architecture")
    skill_netsuite = vocab.node_id("skill", "NetSuite")
    skill_risk = vocab.node_id("skill", "Risk Assessment")
    tool_keboola = vocab.node_id("tool", "Keboola")
    tool_netsuite = vocab.node_id("tool", "NetSuite")
    tool_snowflake = vocab.node_id("tool", "Snowflake")
    eng_vantable = vocab.node_id("engagement", "Vantable Systems AI Value Backlog")
    eng_corundum = vocab.node_id("engagement", "Corundum Foods Sell-side IT Due Diligence")
    eng_osprey = vocab.node_id("engagement", "Osprey Fulfillment Group AI Value Backlog")
    eng_halyard = vocab.node_id("engagement", "Halyard Precision Manufacturing ERP Rollout")
    finding_vantable_headline = vocab.node_id("finding", "vantable-aivb-headline")
    finding_corundum_feedback = vocab.node_id("finding", "corundum-dd-client-feedback")
    finding_corundum_risk = vocab.node_id("finding", "corundum-dd-edi-risk")
    finding_osprey_headline = vocab.node_id("finding", "osprey-aivb-headline")

    # ── Core planted facts (EQ3 precision/recall ground truth) ─────────

    add_doc(
        doc_key="core-vantable-sow",
        site="delivery-archive",
        library="active-engagements",
        subpath="vantable-systems/ai-value-backlog/sow.md",
        title="Vantable Systems — AI Value Backlog SOW",
        doc_type="sow",
        document_date=date(2025, 11, 3),
        requested_format="md",
        paragraphs=[
            f"{firm} will deliver the AI Value Backlog engagement for Vantable Systems, "
            "with a signed SOW date of November 3, 2025.",
            "Vantable Systems is a PE-owned industrial manufacturer with an estimated $210M in annual revenue.",
            "Vantable Systems operates in the Manufacturing industry.",
            "Vantable Systems is a portfolio company of Anchorstone Capital.",
            f"This engagement is scoped as an AI Value Backlog under {firm}' standard service catalog.",
            "Priya Shenvi is the Engagement Lead on the Vantable Systems AI Value Backlog engagement.",
            "Priya Shenvi brings deep Data Architecture experience to the engagement.",
            "The AI Value Backlog identified $1.4M in annualized automation opportunity across three business units.",
        ],
    )
    add_node(
        "core-1",
        eng_vantable,
        "engagement",
        {
            "name": "Vantable Systems — AI Value Backlog",
            "sow_date": "2025-11-03",
        },
        "core-vantable-sow",
        "Meridian Peak Advisors will deliver the AI Value Backlog "
        "engagement for Vantable Systems, with a signed SOW date of November 3, 2025.",
    )
    add_node(
        "core-2",
        vantable,
        "client",
        {
            "name": "Vantable Systems",
            "revenue_usd_estimate": 210000000,
            "ownership": "PE-owned",
        },
        "core-vantable-sow",
        "Vantable Systems is a PE-owned industrial manufacturer with an estimated $210M in annual revenue.",
    )
    add_edge(
        "core-3",
        vantable,
        "in_industry",
        ind_mfg,
        {},
        "core-vantable-sow",
        "Vantable Systems operates in the Manufacturing industry.",
    )
    add_edge(
        "core-4",
        vantable,
        "owned_by",
        anchorstone,
        {},
        "core-vantable-sow",
        "Vantable Systems is a portfolio company of Anchorstone Capital.",
    )
    add_edge(
        "core-5",
        eng_vantable,
        "of_type",
        svc_aivb,
        {},
        "core-vantable-sow",
        "This engagement is scoped as an AI Value Backlog under Meridian Peak Advisors' standard service catalog.",
    )
    add_edge(
        "core-6",
        priya,
        "worked_on",
        eng_vantable,
        {"role": "Engagement Lead"},
        "core-vantable-sow",
        "Priya Shenvi is the Engagement Lead on the Vantable Systems AI Value Backlog engagement.",
    )
    add_edge(
        "core-7",
        priya,
        "has_skill",
        skill_data_arch,
        {},
        "core-vantable-sow",
        "Priya Shenvi brings deep Data Architecture experience to the engagement.",
    )
    add_node(
        "core-8",
        finding_vantable_headline,
        "finding",
        {
            "finding_type": "headline",
            "text": "The AI Value Backlog identified $1.4M in annualized automation "
            "opportunity across three business units.",
        },
        "core-vantable-sow",
        "The AI Value Backlog identified $1.4M in annualized automation opportunity across three business units.",
    )
    add_edge(
        "core-9",
        eng_vantable,
        "has_finding",
        finding_vantable_headline,
        {},
        "core-vantable-sow",
        "The AI Value Backlog identified $1.4M in annualized automation opportunity across three business units.",
    )

    add_doc(
        doc_key="core-vantable-kickoff",
        site="delivery-archive",
        library="active-engagements",
        subpath="vantable-systems/ai-value-backlog/kickoff-notes.docx",
        title="Vantable Systems AI Value Backlog — Kickoff Notes",
        doc_type="notes",
        document_date=date(2025, 11, 12),
        requested_format="docx",
        paragraphs=[
            "The delivery team will use Keboola to build the data pipelines for this engagement.",
            "Vantable Systems already runs NetSuite as its core ERP platform.",
        ],
    )
    add_edge(
        "core-10",
        eng_vantable,
        "used_tool",
        tool_keboola,
        {},
        "core-vantable-kickoff",
        "The delivery team will use Keboola to build the data pipelines for this engagement.",
    )
    add_edge(
        "core-11",
        vantable,
        "uses_technology",
        tool_netsuite,
        {},
        "core-vantable-kickoff",
        "Vantable Systems already runs NetSuite as its core ERP platform.",
    )

    add_doc(
        doc_key="core-corundum-dd-summary",
        site="delivery-archive",
        library="active-engagements",
        subpath="corundum-foods/sell-side-it-dd/summary.md",
        title="Corundum Foods — Sell-side IT Due Diligence Summary",
        doc_type="deliverable",
        document_date=date(2026, 1, 5),
        requested_format="md",
        paragraphs=[
            f"{firm} completed a Sell-side IT Due Diligence engagement for Corundum "
            "Foods, a food manufacturer with roughly $96M in estimated revenue.",
            "Corundum Foods sits in the Food Manufacturing industry, a subsegment of Manufacturing.",
            "Corundum Foods is owned by Redwater Partners.",
            "Dax Okonkwo-Reyes served as Senior Consultant on the Corundum Foods engagement.",
            "Dax Okonkwo-Reyes has hands-on NetSuite implementation experience.",
        ],
    )
    add_node(
        "core-12",
        eng_corundum,
        "engagement",
        {
            "name": "Corundum Foods — Sell-side IT Due Diligence",
            "sow_date": "2026-01-05",
        },
        "core-corundum-dd-summary",
        "Meridian Peak Advisors completed a Sell-side IT "
        "Due Diligence engagement for Corundum Foods, a food manufacturer with "
        "roughly $96M in estimated revenue.",
    )
    add_node(
        "core-13",
        corundum,
        "client",
        {
            "name": "Corundum Foods",
            "revenue_usd_estimate": 96000000,
            "ownership": "PE-owned",
        },
        "core-corundum-dd-summary",
        "Meridian Peak Advisors completed a Sell-side IT "
        "Due Diligence engagement for Corundum Foods, a food manufacturer with "
        "roughly $96M in estimated revenue.",
    )
    add_edge(
        "core-14",
        eng_corundum,
        "for_client",
        corundum,
        {},
        "core-corundum-dd-summary",
        "Meridian Peak Advisors completed a Sell-side IT Due Diligence "
        "engagement for Corundum Foods, a food manufacturer with roughly "
        "$96M in estimated revenue.",
    )
    add_edge(
        "core-15",
        corundum,
        "in_industry",
        ind_food,
        {},
        "core-corundum-dd-summary",
        "Corundum Foods sits in the Food Manufacturing industry, a subsegment of Manufacturing.",
    )
    add_node(
        "core-16",
        ind_food,
        "industry",
        {"name": "Food Manufacturing", "parent": "Manufacturing"},
        "core-corundum-dd-summary",
        "Corundum Foods sits in the Food Manufacturing industry, a subsegment of Manufacturing.",
    )
    add_edge(
        "core-17",
        corundum,
        "owned_by",
        redwater,
        {},
        "core-corundum-dd-summary",
        "Corundum Foods is owned by Redwater Partners.",
    )
    add_edge(
        "core-18",
        dax,
        "worked_on",
        eng_corundum,
        {"role": "Senior Consultant"},
        "core-corundum-dd-summary",
        "Dax Okonkwo-Reyes served as Senior Consultant on the Corundum Foods engagement.",
    )
    add_edge(
        "core-19",
        dax,
        "has_skill",
        skill_netsuite,
        {},
        "core-corundum-dd-summary",
        "Dax Okonkwo-Reyes has hands-on NetSuite implementation experience.",
    )

    add_doc(
        doc_key="core-corundum-feedback",
        site="client-services",
        library="account-notes",
        subpath="corundum-foods/client-feedback.md",
        title="Corundum Foods — Client Feedback",
        doc_type="feedback",
        document_date=date(2026, 1, 20),
        requested_format="md",
        paragraphs=[
            '"The due diligence team gave us a clear-eyed view of the technology '
            'risk before we signed." — Corundum Foods, CFO',
        ],
    )
    add_node(
        "core-20",
        finding_corundum_feedback,
        "finding",
        {
            "finding_type": "client_feedback",
            "text": '"The due diligence team gave us a clear-eyed view of the technology '
            'risk before we signed." — Corundum Foods, CFO',
        },
        "core-corundum-feedback",
        '"The due diligence team gave us a clear-eyed view '
        'of the technology risk before we signed." — Corundum Foods, CFO',
    )
    add_edge(
        "core-21",
        eng_corundum,
        "has_finding",
        finding_corundum_feedback,
        {},
        "core-corundum-feedback",
        '"The due diligence team gave us a '
        'clear-eyed view of the technology risk before we signed." — '
        "Corundum Foods, CFO",
    )

    add_doc(
        doc_key="core-corundum-risk",
        site="client-services",
        library="escalations",
        subpath="corundum-foods/risk-note.md",
        title="Corundum Foods — Risk Note",
        doc_type="notes",
        document_date=date(2026, 1, 22),
        requested_format="md",
        paragraphs=[
            "Legacy EDI integration remains a single point of failure through the transition period.",
            "Dax Okonkwo-Reyes flagged the integration risk based on his Risk Assessment background.",
        ],
    )
    add_node(
        "core-22",
        finding_corundum_risk,
        "finding",
        {
            "finding_type": "risk",
            "text": "Legacy EDI integration remains a single point of failure through the transition period.",
        },
        "core-corundum-risk",
        "Legacy EDI integration remains a single point of failure through the transition period.",
    )
    add_edge(
        "core-23",
        eng_corundum,
        "has_finding",
        finding_corundum_risk,
        {},
        "core-corundum-risk",
        "Legacy EDI integration remains a single point of failure through the transition period.",
    )
    add_edge(
        "core-24",
        dax,
        "has_skill",
        skill_risk,
        {},
        "core-corundum-risk",
        "Dax Okonkwo-Reyes flagged the integration risk based on his Risk Assessment background.",
    )

    add_doc(
        doc_key="core-osprey-delivery",
        site="delivery-archive",
        library="completed-engagements",
        subpath="osprey-fulfillment-group/ai-value-backlog/delivery-summary.pptx",
        title="Osprey Fulfillment — AI Value Backlog Delivery",
        doc_type="deliverable",
        document_date=date(2026, 2, 20),
        requested_format="pptx",
        format_shape="deck",
        paragraphs=[
            f"{firm} kicked off the AI Value Backlog engagement for Osprey Fulfillment Group in February 2026.",
            "Osprey Fulfillment Group operates in the Logistics industry.",
            "This work follows Meridian Peak Advisors' standard AI Value Backlog service offering.",
            "Consolidating fulfillment reporting onto Snowflake cut month-end close by four days.",
            "The delivery team used Keboola for the underlying data integration work.",
        ],
    )
    add_node(
        "core-25",
        eng_osprey,
        "engagement",
        {
            "name": "Osprey Fulfillment Group — AI Value Backlog",
        },
        "core-osprey-delivery",
        "Meridian Peak Advisors kicked off the AI Value "
        "Backlog engagement for Osprey Fulfillment Group in February 2026.",
    )
    add_edge(
        "core-26",
        eng_osprey,
        "for_client",
        osprey,
        {},
        "core-osprey-delivery",
        "Meridian Peak Advisors kicked off the AI Value Backlog engagement "
        "for Osprey Fulfillment Group in February 2026.",
    )
    add_edge(
        "core-27",
        osprey,
        "in_industry",
        ind_logistics,
        {},
        "core-osprey-delivery",
        "Osprey Fulfillment Group operates in the Logistics industry.",
    )
    add_edge(
        "core-28",
        eng_osprey,
        "of_type",
        svc_aivb,
        {},
        "core-osprey-delivery",
        "This work follows Meridian Peak Advisors' standard AI Value Backlog service offering.",
    )
    add_node(
        "core-29",
        finding_osprey_headline,
        "finding",
        {
            "finding_type": "headline",
            "text": "Consolidating fulfillment reporting onto Snowflake cut month-end close by four days.",
        },
        "core-osprey-delivery",
        "Consolidating fulfillment reporting onto Snowflake cut month-end close by four days.",
    )
    add_edge(
        "core-30",
        eng_osprey,
        "has_finding",
        finding_osprey_headline,
        {},
        "core-osprey-delivery",
        "Consolidating fulfillment reporting onto Snowflake cut month-end close by four days.",
    )
    add_edge(
        "core-31",
        osprey,
        "uses_technology",
        tool_snowflake,
        {},
        "core-osprey-delivery",
        "Consolidating fulfillment reporting onto Snowflake cut month-end close by four days.",
    )
    add_edge(
        "core-32",
        eng_osprey,
        "used_tool",
        tool_keboola,
        {},
        "core-osprey-delivery",
        "The delivery team used Keboola for the underlying data integration work.",
    )

    # ── S1: fully isolated secret value ─────────────────────────────────

    s1_value = "Project Umbra-Kestrel"
    add_doc(
        doc_key="s1-secret-codename",
        site="firm-operations",
        library="legal-secret",
        subpath="engagement-addenda/vantable-systems-scope-extension.md",
        title="Vantable Systems — Confidential Scope Extension",
        doc_type="notes",
        document_date=date(2026, 4, 1),
        requested_format="md",
        paragraphs=[
            f"The confidential follow-on phase is internally codenamed {s1_value}.",
        ],
    )
    add_node(
        "s1-1",
        eng_vantable,
        "engagement",
        {"price_note": f"Internally codenamed {s1_value} for the follow-on phase."},
        "s1-secret-codename",
        f"The confidential follow-on phase is internally codenamed {s1_value}.",
    )

    # ── S2: attribute oracle (readable existence / secret price) ───────

    s2_price = 412000
    add_doc(
        doc_key="s2-secret-price",
        site="proposals-and-pursuits",
        library="pricing-sheets",
        subpath="osprey-fulfillment-group/ai-value-backlog-price.xlsx",
        title="Osprey Fulfillment Group — AI Value Backlog Pricing",
        doc_type="other",
        document_date=date(2026, 2, 15),
        requested_format="xlsx",
        format_shape="table-heavy",
        paragraphs=[
            f"The Osprey Fulfillment Group AI Value Backlog engagement is priced at ${s2_price:,}.",
        ],
    )
    add_node(
        "s2-1",
        eng_osprey,
        "engagement",
        {"price_usd": s2_price},
        "s2-secret-price",
        f"The Osprey Fulfillment Group AI Value Backlog engagement is priced at ${s2_price:,}.",
    )

    # ── S3: edge-only-in-secret-doc, both endpoints readable elsewhere ──

    add_doc(
        doc_key="s3-secret-priya-review",
        site="firm-operations",
        library="legal-secret",
        subpath="performance/priya-shenvi-review-2026.md",
        title="Priya Shenvi — Confidential Performance Review",
        doc_type="notes",
        document_date=date(2026, 4, 10),
        requested_format="md",
        paragraphs=[
            "Priya Shenvi also demonstrated strong NetSuite configuration skills during the review period.",
        ],
    )
    add_edge(
        "s3-1",
        priya,
        "has_skill",
        skill_netsuite,
        {},
        "s3-secret-priya-review",
        "Priya Shenvi also demonstrated strong NetSuite configuration skills during the review period.",
    )

    # ── S4: A -> B -> C chain, C's evidence entirely secret ─────────────

    add_doc(
        doc_key="s4-halyard-kickoff-readable",
        site="delivery-archive",
        library="active-engagements",
        subpath="halyard-precision/erp-rollout/kickoff-notes.md",
        title="Halyard Precision — ERP Rollout Kickoff Notes",
        doc_type="notes",
        document_date=date(2026, 3, 5),
        requested_format="md",
        paragraphs=[
            "Dax Okonkwo-Reyes is the lead consultant on the ERP Rollout engagement that began March 1, 2026.",
            f"This engagement follows {firm}' ERP Rollout service offering.",
        ],
    )
    add_node(
        "s4-1",
        eng_halyard,
        "engagement",
        {
            "name": "Halyard Precision Manufacturing — ERP Rollout",
            "start_date": "2026-03-01",
        },
        "s4-halyard-kickoff-readable",
        "Dax Okonkwo-Reyes is the lead consultant on the ERP Rollout engagement that began March 1, 2026.",
    )
    add_edge(
        "s4-2",
        dax,
        "worked_on",
        eng_halyard,
        {"role": "Lead Consultant"},
        "s4-halyard-kickoff-readable",
        "Dax Okonkwo-Reyes is the lead consultant on the ERP Rollout engagement that began March 1, 2026.",
    )
    add_edge(
        "s4-3",
        eng_halyard,
        "of_type",
        svc_erp,
        {},
        "s4-halyard-kickoff-readable",
        "This engagement follows Meridian Peak Advisors' ERP Rollout service offering.",
    )

    add_doc(
        doc_key="s4-halyard-client-secret",
        site="firm-operations",
        library="legal-secret",
        subpath="confidential/halyard-precision-manufacturing-client-brief.md",
        title="Halyard Precision Manufacturing — Confidential Client Brief",
        doc_type="notes",
        document_date=date(2026, 3, 6),
        requested_format="md",
        paragraphs=[
            "The ERP Rollout engagement is being delivered for Halyard Precision Manufacturing.",
            "Halyard Precision Manufacturing has asked that its name not be shared "
            "outside the deal team until the transaction closes.",
        ],
    )
    add_edge(
        "s4-4",
        eng_halyard,
        "for_client",
        halyard,
        {},
        "s4-halyard-client-secret",
        "The ERP Rollout engagement is being delivered for Halyard Precision Manufacturing.",
    )
    add_node(
        "s4-5",
        halyard,
        "client",
        {"name": "Halyard Precision Manufacturing"},
        "s4-halyard-client-secret",
        "Halyard Precision Manufacturing has "
        "asked that its name not be shared outside the deal team until the "
        "transaction closes.",
    )

    # ── Traps ────────────────────────────────────────────────────────

    add_doc(
        doc_key="trap-scan-signoff",
        site="firm-operations",
        library="tooling-notes",
        subpath="signoffs/osprey-scope-signoff-scan.pdf",
        title="Osprey Fulfillment — Scope Signoff (scanned)",
        doc_type="other",
        document_date=date(2026, 2, 25),
        requested_format="pdf-scan",
        format_shape="scan",
        trap="scan",
        paragraphs=[],
        scan_planted_text="Signed off by Priya Shenvi on 2026-02-25:",
        scan_extra_lines=["Osprey Fulfillment Group engagement scope approved."],
    )

    dup_paragraphs = [
        f"{firm} and Osprey Fulfillment Group held a retrospective covering the "
        "AI Value Backlog engagement's outcomes.",
    ]
    add_doc(
        doc_key="trap-duplicate-a",
        site="delivery-archive",
        library="completed-engagements",
        subpath="osprey-fulfillment-group/ai-value-backlog/retro.md",
        title="Osprey Fulfillment Group — AI Value Backlog Retrospective",
        doc_type="notes",
        document_date=date(2026, 3, 15),
        requested_format="md",
        trap="duplicate",
        paragraphs=dup_paragraphs,
    )
    add_doc(
        doc_key="trap-duplicate-b",
        site="client-services",
        library="account-notes",
        subpath="osprey-fulfillment-group/retro-copy.md",
        title="Osprey Fulfillment Group — AI Value Backlog Retrospective",
        doc_type="notes",
        document_date=date(2026, 3, 15),
        requested_format="md",
        trap="duplicate",
        paragraphs=dup_paragraphs,
    )

    add_doc(
        doc_key="trap-succession-v1",
        site="delivery-archive",
        library="active-engagements",
        subpath="corundum-foods/sell-side-it-dd/status-jan.md",
        title="Corundum Foods DD — Status (January)",
        doc_type="notes",
        document_date=date(2026, 1, 15),
        requested_format="md",
        trap="succession",
        paragraphs=[
            "The Corundum Foods Sell-side IT Due Diligence engagement is currently active.",
        ],
    )
    add_node(
        "trap-succession-1",
        eng_corundum,
        "engagement",
        {"status": "active"},
        "trap-succession-v1",
        "The Corundum Foods Sell-side IT Due Diligence engagement is currently active.",
    )
    add_doc(
        doc_key="trap-succession-v2",
        site="delivery-archive",
        library="completed-engagements",
        subpath="corundum-foods/sell-side-it-dd/status-may.md",
        title="Corundum Foods DD — Status (May)",
        doc_type="notes",
        document_date=date(2026, 5, 20),
        requested_format="md",
        trap="succession",
        paragraphs=[
            "The Corundum Foods Sell-side IT Due Diligence engagement is now completed.",
        ],
    )
    add_node(
        "trap-succession-2",
        eng_corundum,
        "engagement",
        {"status": "completed"},
        "trap-succession-v2",
        "The Corundum Foods Sell-side IT Due Diligence engagement is now completed.",
    )

    add_doc(
        doc_key="trap-contradiction-a",
        site="client-services",
        library="account-notes",
        subpath="osprey-fulfillment-group/ownership-note-a.md",
        title="Osprey Fulfillment Group — Ownership Note",
        doc_type="notes",
        document_date=date(2026, 3, 1),
        requested_format="md",
        trap="contradiction",
        paragraphs=["Osprey Fulfillment Group is owned by Anchorstone Capital."],
    )
    add_edge(
        "trap-contradiction-1",
        osprey,
        "owned_by",
        anchorstone,
        {},
        "trap-contradiction-a",
        "Osprey Fulfillment Group is owned by Anchorstone Capital.",
    )
    add_doc(
        doc_key="trap-contradiction-b",
        site="client-services",
        library="escalations",
        subpath="osprey-fulfillment-group/ownership-note-b.md",
        title="Osprey Fulfillment Group — Ownership Note (escalation)",
        doc_type="notes",
        document_date=date(2026, 3, 1),
        requested_format="md",
        trap="contradiction",
        paragraphs=["Osprey Fulfillment Group is a portfolio company of Redwater Partners."],
    )
    add_edge(
        "trap-contradiction-2",
        osprey,
        "owned_by",
        redwater,
        {},
        "trap-contradiction-b",
        "Osprey Fulfillment Group is a portfolio company of Redwater Partners.",
    )

    corundum_inc = vocab.node_id("client", "Corundum Foods, Inc.")
    add_doc(
        doc_key="trap-er-corundum-variant",
        site="proposals-and-pursuits",
        library="won-proposals",
        subpath="corundum-foods-inc/phase-2-kickoff.md",
        title="Corundum Foods, Inc. — Phase 2 Kickoff",
        doc_type="notes",
        document_date=date(2026, 6, 1),
        requested_format="md",
        trap="entity_resolution",
        paragraphs=[
            f"Corundum Foods, Inc. engaged {firm} for a second phase of NetSuite rollout work.",
        ],
    )
    add_node(
        "trap-er-1",
        corundum_inc,
        "client",
        {"name": "Corundum Foods, Inc."},
        "trap-er-corundum-variant",
        "Corundum Foods, Inc. engaged Meridian Peak Advisors for a second phase of NetSuite rollout work.",
    )

    add_doc(
        doc_key="trap-fabrication-postmortem",
        site="firm-operations",
        library="tooling-notes",
        subpath="postmortems/vantable-ai-value-backlog-postmortem.md",
        title="Vantable Systems AI Value Backlog — Postmortem",
        doc_type="notes",
        document_date=date(2026, 6, 10),
        requested_format="md",
        trap="fabrication",
        paragraphs=[
            "The Vantable Systems AI Value Backlog engagement wrapped ahead of schedule.",
            "Lessons learned are documented in the shared retrospective board.",
        ],
    )
    false_quote = "Vantable Systems reported prior-year revenue of $999 million."
    false_claims.append(
        PreparedFalseClaim(
            claim_key="trap-fabrication-1",
            subject_kind="fact",
            subject_id=vantable,
            attrs={"revenue_usd_estimate": 999000000},
            doc_key="trap-fabrication-postmortem",
            quote=false_quote,
            reason="quote is not a substring of the document's text — the verbatim gate (EQ1) must reject it",
        )
    )

    # ── AN1: canary token ────────────────────────────────────────────

    an1_canary = "Bartholomew Quillfeather Ashgrove"
    add_doc(
        doc_key="an1-canary-contact",
        site="client-services",
        library="account-notes",
        subpath="vantable-systems/kickoff-attendees.md",
        title="Vantable Systems — Kickoff Attendees",
        doc_type="notes",
        document_date=date(2025, 11, 11),
        requested_format="md",
        paragraphs=[
            f"{an1_canary}, Vantable Systems' IT director, joined the kickoff call.",
        ],
    )

    # ── AN2: Czech inflected-form pair ──────────────────────────────

    tomas = vocab.node_id("person", "Tomáš Padrák")
    add_doc(
        doc_key="an2-czech-workshop-nominative",
        site="firm-operations",
        library="skills-registry",
        subpath="staff/tomas-padrak-workshop-note.md",
        title="Interní poznámka — datový workshop",
        doc_type="notes",
        document_date=date(2026, 4, 15),
        requested_format="md",
        format_shape="czech-diacritics",
        paragraphs=[
            "Tomáš Padrák vedl interní workshop o datové architektuře pro tým Corundum Foods.",
        ],
    )
    add_node(
        "an2-1",
        tomas,
        "person",
        {
            "name": "Tomáš Padrák",
            "org": firm,
            "title": "Data Architect",
        },
        "an2-czech-workshop-nominative",
        "Tomáš Padrák vedl interní workshop o datové architektuře pro tým Corundum Foods.",
    )
    add_doc(
        doc_key="an2-czech-feedback-dative",
        site="firm-operations",
        library="skills-registry",
        subpath="staff/tomas-padrak-feedback-dative.md",
        title="Zpětná vazba k workshopu",
        doc_type="feedback",
        document_date=date(2026, 4, 22),
        requested_format="md",
        format_shape="czech-diacritics",
        paragraphs=[
            "Zpětná vazba z workshopu byla zaslána Tomáši Padrákovi k připomínkám.",
        ],
    )
    add_node(
        "an2-2",
        tomas,
        "person",
        {},
        "an2-czech-feedback-dative",
        "Zpětná vazba z workshopu byla zaslána Tomáši Padrákovi k připomínkám.",
    )

    s_fixtures = {
        "S1": {
            "description": "A secret-group-c-only value contributes nothing to a principal/associate caller.",
            "planted_value": s1_value,
            "doc_key": "s1-secret-codename",
            "claim_key": "s1-1",
        },
        "S2": {
            "description": "One engagement fact, two claims: existence readable, price_usd secret.",
            "readable_claim_key": "core-25",
            "secret_claim_key": "s2-1",
            "attribute": "price_usd",
            "value": s2_price,
        },
        "S3": {
            "description": "has_skill edge whose only claim is secret, between two independently readable facts.",
            "claim_key": "s3-1",
            "src": priya,
            "dst": skill_netsuite,
        },
        "S4": {
            "description": "A -[worked_on]-> B -[for_client]-> C chain; C's "
            "evidence (node + incoming edge) is entirely secret.",
            "a": dax,
            "b": eng_halyard,
            "c": halyard,
            "readable_claim_keys": ["s4-1", "s4-2", "s4-3"],
            "secret_claim_keys": ["s4-4", "s4-5"],
        },
    }

    traps = {
        "scan": {
            "description": "Image-only PDF; text exists only as pixels.",
            "doc_key": "trap-scan-signoff",
            "planted_text": "Signed off by Priya Shenvi on 2026-02-25: Osprey "
            "Fulfillment Group engagement scope approved.",
        },
        "duplicate": {
            "description": "Byte-identical content at two paths -> same sha256 / doc_id, distinct stable_id.",
            "doc_keys": ["trap-duplicate-a", "trap-duplicate-b"],
        },
        "succession": {
            "description": "Same subject, different document_date, changed value "
            "-> later wins as current, earlier stays queryable as "
            "history, no conflict entry.",
            "subject_id": eng_corundum,
            "attribute": "status",
            "claim_keys": ["trap-succession-1", "trap-succession-2"],
            "earlier_value": "active",
            "later_value": "completed",
        },
        "contradiction": {
            "description": "Same subject, same document_date, incompatible values "
            "-> both kept, review item, no silent pick.",
            "subject_id": osprey,
            "edge_type": "owned_by",
            "claim_keys": ["trap-contradiction-1", "trap-contradiction-2"],
            "document_date": "2026-03-01",
            "values": [anchorstone, redwater],
        },
        "entity_resolution": {
            "description": "Punctuation/spacing variant of the same real client -> expected merge, reversible.",
            "canonical_id": corundum,
            "variant_id": corundum_inc,
            "claim_key": "trap-er-1",
        },
        "fabrication": {
            "description": "A prepared false claim whose quote is NOT a substring "
            "of the document's text -> the verbatim gate (EQ1) "
            "must reject it.",
            "doc_key": "trap-fabrication-postmortem",
            "claim_key": "trap-fabrication-1",
        },
    }

    anonymization = {
        "AN1_canary": {
            "description": "A unique planted name that must appear nowhere in "
            "Agnes once the anonymizer runs ahead of ingestion.",
            "value": an1_canary,
            "doc_key": "an1-canary-contact",
        },
        "AN2_pair": {
            "description": "Same invented Czech person in two inflected surface "
            "forms across two documents -> expected one subject.",
            "canonical_id": tomas,
            "canonical_name": "Tomáš Padrák",
            "surface_forms": {
                "nominative": "Tomáš Padrák",
                "dative": "Tomáši Padrákovi",
            },
            "doc_keys": ["an2-czech-workshop-nominative", "an2-czech-feedback-dative"],
            "claim_keys": ["an2-1", "an2-2"],
        },
    }

    return PlantedPlan(
        docs=docs,
        nodes=nodes,
        edges=edges,
        prepared_false_claims=false_claims,
        s_fixtures=s_fixtures,
        traps=traps,
        anonymization=anonymization,
    )
