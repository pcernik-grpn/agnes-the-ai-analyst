#!/usr/bin/env python3
"""Planted proving-run corpus generator (fact-graph spec §15.5, Run P).

Deterministically generates a filesystem corpus shaped like a SharePoint
crawl -- >=4 top-level "sites", each with 2-3 "libraries", nested folders,
mixed document formats -- plus a `ground_truth.json` manifest recording
every planted fact/edge in the producer wire format (spec §7.0), every
S1-S4 security fixture, every trap (scan / duplicate / superseded version /
same-date contradiction / entity-resolution pair / adversarial
fabrication), the AN1 canary and the AN2 Czech-inflection pair, and a
`sharing.yaml` mapping every site/library to the Agnes groups it would be
granted to.

This generator does not depend on any Agnes app code -- it is a standalone
fixture factory consumed by the (separately written) S/C/EQ/AN acceptance
tests and by Run P itself. All planted names are invented; see
corpus_helpers/vocab.py's module docstring for why.

Usage:
    python scripts/eval/corpus_gen.py --small --out tests/fixtures/eval/planted_corpus_small
    python scripts/eval/corpus_gen.py --out data/eval/planted_corpus_full --n-docs 1000
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

try:
    from corpus_helpers import filler, planted, writers
except ImportError:  # running as a bare script (no package context)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from corpus_helpers import filler, planted, writers

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is a core dependency
    yaml = None

DEFAULT_SEED = 20260827
SMALL_TOTAL_DOCS = 30  # no filler; the planted set alone lands close to this
FULL_MIN_DOCS = 1000
CORPUS_GEN_VERSION = "1.0.0"

_MIME_BY_FORMAT = {
    "md": "text/markdown",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
}


def _resolve_groups(site: str, library: str) -> list[str]:
    return list(planted.SITES[site][library])


def _write_one_doc(out_dir: Path, spec: planted.DocSpec) -> dict[str, Any]:
    library_dir = out_dir / spec.site / spec.library
    target_no_ext = (library_dir / spec.subpath).with_suffix("")
    target_no_ext.parent.mkdir(parents=True, exist_ok=True)

    if spec.requested_format == "pdf-scan":
        actual_path = target_no_ext.with_suffix(".pdf")
        writers.write_scan_pdf(actual_path, spec.scan_planted_text or "", spec.scan_extra_lines)
        actual_format = "pdf"
        was_fallback = False
    else:
        actual_path, actual_format, was_fallback = writers.write_document(
            target_no_ext, spec.requested_format, spec.title, spec.paragraphs
        )

    content = actual_path.read_bytes()
    sha256_full = hashlib.sha256(content).hexdigest()
    doc_id = f"document:{sha256_full[:16]}"
    relpath = actual_path.relative_to(out_dir).as_posix()

    modified_dt = datetime(
        spec.document_date.year,
        spec.document_date.month,
        spec.document_date.day,
        9,
        0,
        0,
        tzinfo=timezone.utc,
    )

    return {
        # crawler's 17-field make_row shape (spec §7.0), source="local"
        "doc_id": doc_id,
        "stable_id": f"local:{relpath}",
        "name": actual_path.name,
        "path": relpath,
        "site": spec.site,
        "drive": spec.library,
        "source": "local",
        "mime": _MIME_BY_FORMAT.get(actual_format, "application/octet-stream"),
        "size": len(content),
        "created": modified_dt.isoformat(),
        "modified": modified_dt.isoformat(),
        "author": spec.author,
        "last_editor": spec.last_editor,
        "sha256": sha256_full,
        "extracted_path": None,
        # generator-side EXPECTATION, not a live crawl artifact (no crawler
        # ran) -- what the real pipeline should converge to.
        "extract_status": "ok",
        "crawled_at": None,
        # extension fields (not part of the crawler's 17-field contract)
        "document_date": spec.document_date.isoformat(),
        "doc_type": spec.doc_type,
        "groups": _resolve_groups(spec.site, spec.library),
        "format": actual_format,
        "format_fallback": was_fallback,
        "format_shape": spec.format_shape,
        "trap": spec.trap,
        "doc_key": spec.doc_key,
    }


def _resolve_doc_id(doc_records: dict[str, dict], doc_key: str) -> str:
    return doc_records[doc_key]["doc_id"]


def _build_nodes_edges(plan: planted.PlantedPlan, doc_records: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    nodes = [
        {
            "claim_key": n.claim_key,
            "id": n.id,
            "type": n.type,
            "attrs": n.attrs,
            "evidence": [{"doc_id": _resolve_doc_id(doc_records, n.doc_key), "quote": n.quote}],
        }
        for n in plan.nodes
    ]
    edges = [
        {
            "claim_key": e.claim_key,
            "src": e.src,
            "type": e.type,
            "dst": e.dst,
            "attrs": e.attrs,
            "evidence": [{"doc_id": _resolve_doc_id(doc_records, e.doc_key), "quote": e.quote}],
        }
        for e in plan.edges
    ]
    return nodes, edges


def _resolve_refs(obj: Any, doc_records: dict[str, dict]) -> Any:
    """Recursively replace `doc_key`/`claim_keys`/`doc_keys` references in
    the plan's descriptive dicts (s_fixtures/traps/anonymization) with the
    resolved doc_id, leaving everything else untouched."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "doc_key" and isinstance(v, str):
                out["doc_key"] = v
                out["doc_id"] = _resolve_doc_id(doc_records, v)
            elif k == "doc_keys" and isinstance(v, list):
                out["doc_keys"] = v
                out["doc_ids"] = [_resolve_doc_id(doc_records, dk) for dk in v]
            else:
                out[k] = _resolve_refs(v, doc_records)
        return out
    if isinstance(obj, list):
        return [_resolve_refs(v, doc_records) for v in obj]
    return obj


def _build_prepared_false_claims(plan: planted.PlantedPlan, doc_records: dict[str, dict]) -> list[dict]:
    out = []
    for fc in plan.prepared_false_claims:
        out.append(
            {
                "claim_key": fc.claim_key,
                "subject_kind": fc.subject_kind,
                "subject_id": fc.subject_id,
                "attrs": fc.attrs,
                "doc_id": _resolve_doc_id(doc_records, fc.doc_key),
                "quote": fc.quote,
                "reason": fc.reason,
            }
        )
    return out


def _build_sharing_plan() -> dict[str, Any]:
    return {
        "groups": planted.GROUPS,
        "sites": {
            site: {"libraries": {library: {"groups": groups} for library, groups in libraries.items()}}
            for site, libraries in planted.SITES.items()
        },
    }


def generate(
    out_dir: Path,
    *,
    seed: int = DEFAULT_SEED,
    small: bool = False,
    n_docs: int | None = None,
) -> dict[str, Any]:
    """Generate the corpus + ground_truth.json under `out_dir`. Returns the
    manifest dict (also written to disk)."""
    out_dir.mkdir(parents=True, exist_ok=True)

    plan = planted.build_plan()
    planted_docs = list(plan.docs)

    if small:
        filler_docs: list[planted.DocSpec] = []
        mode = "small"
    else:
        target = n_docs if n_docs is not None else FULL_MIN_DOCS
        target = max(target, FULL_MIN_DOCS, len(planted_docs))
        n_filler = target - len(planted_docs)
        rng = random.Random(seed)
        filler_docs = filler.build_filler_docs(rng, n_filler)
        mode = "full"

    all_docs = planted_docs + filler_docs

    doc_records: dict[str, dict] = {}
    for spec in all_docs:
        record = _write_one_doc(out_dir, spec)
        doc_records[spec.doc_key] = record

    nodes, edges = _build_nodes_edges(plan, doc_records)

    manifest = {
        "generator": {
            "name": "scripts/eval/corpus_gen.py",
            "version": CORPUS_GEN_VERSION,
            "seed": seed,
            "mode": mode,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        "groups": planted.GROUPS,
        "sites": _build_sharing_plan()["sites"],
        "documents": [doc_records[spec.doc_key] for spec in all_docs],
        "nodes": nodes,
        "edges": edges,
        "prepared_false_claims": _build_prepared_false_claims(plan, doc_records),
        "planted_elements": {
            "s_fixtures": _resolve_refs(plan.s_fixtures, doc_records),
            "traps": _resolve_refs(plan.traps, doc_records),
            "anonymization": _resolve_refs(plan.anonymization, doc_records),
        },
        "counts": {
            "documents": len(all_docs),
            "planted_documents": len(planted_docs),
            "filler_documents": len(filler_docs),
            "sites": len(planted.SITES),
            "nodes": len(nodes),
            "edges": len(edges),
        },
    }

    (out_dir / "ground_truth.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=False, default=_json_default), encoding="utf-8"
    )
    if yaml is not None:
        (out_dir / "sharing.yaml").write_text(yaml.safe_dump(_build_sharing_plan(), sort_keys=False), encoding="utf-8")

    return manifest


def _json_default(obj: Any) -> Any:
    if isinstance(obj, date):
        return obj.isoformat()
    raise TypeError(f"not JSON serializable: {obj!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True, help="output directory")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="determinism seed (default: %(default)s)")
    parser.add_argument("--small", action="store_true", help="~30 docs, all planted elements, no filler (CI-speed)")
    parser.add_argument(
        "--n-docs", type=int, default=None, help="full-mode target doc count (default: %(default)s -> 1000)"
    )
    args = parser.parse_args(argv)

    manifest = generate(args.out, seed=args.seed, small=args.small, n_docs=args.n_docs)
    counts = manifest["counts"]
    print(
        f"wrote {counts['documents']} documents "
        f"({counts['planted_documents']} planted, {counts['filler_documents']} filler) "
        f"across {counts['sites']} sites to {args.out}",
        file=sys.stderr,
    )
    print(f"ground truth: {counts['nodes']} nodes, {counts['edges']} edges", file=sys.stderr)
    fmts = writers.available_formats()
    missing = [k for k, v in fmts.items() if not v]
    if missing:
        print(
            f"note: {', '.join(missing)} writer library unavailable -- those "
            "documents fell back to Markdown (see format_fallback in "
            "ground_truth.json)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
