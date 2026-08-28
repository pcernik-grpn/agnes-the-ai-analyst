"""Turn a planted-corpus ground-truth manifest into producer ship inputs.

The Run P proving run (design spec §15.5) uploads the planted corpus through
the producer's own ship client, which expects a crawler-style out dir:
``documents.jsonl`` (make_row rows) + ``nodes.jsonl``/``edges.jsonl`` (wire
rows). The corpus generator's ``ground_truth.json`` already carries all
three in manifest form; this script materializes them, with two deliberate
adjustments:

- ``site`` becomes ``"<site>/<drive>"`` — the ship client maps scope →
  collection per ``site``, and the sharing plan (``sharing.yaml``) is
  per-library, so the composite key gives each library its own collection.
- ``extracted_path`` is set to the document's own relative ``path`` — the
  generated corpus IS its own extraction (markdown), there is no separate
  artifact tree. A document with no extractable text (the planted scan)
  keeps ``extracted_path=None`` and is skipped by the ship client; the run
  report must count it, not hide it.

Usage:
    python scripts/eval/run_p_shipdir.py <corpus_dir>

Writes ``documents.jsonl``, ``nodes.jsonl``, ``edges.jsonl`` and a
``corpus_map.template.json`` (site/library keys -> null, to be filled with
live collection ids) into ``<corpus_dir>``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def build_shipdir(corpus_dir: Path) -> dict:
    manifest = json.loads((corpus_dir / "ground_truth.json").read_text())

    scopes: set[str] = set()
    skipped_no_text = 0
    with (corpus_dir / "documents.jsonl").open("w") as fh:
        for row in manifest["documents"]:
            out = dict(row)
            scope = f"{row['site']}/{row['drive']}"
            out["site"] = scope
            scopes.add(scope)
            if row.get("extracted_path") is None:
                path = row["path"]
                if path.endswith((".md", ".txt")) and (corpus_dir / path).exists():
                    out["extracted_path"] = path
                else:
                    skipped_no_text += 1
            fh.write(json.dumps(out) + "\n")

    for name in ("nodes", "edges"):
        with (corpus_dir / f"{name}.jsonl").open("w") as fh:
            for row in manifest[name]:
                fh.write(json.dumps(row) + "\n")

    template = {scope: None for scope in sorted(scopes)}
    (corpus_dir / "corpus_map.template.json").write_text(json.dumps(template, indent=2) + "\n")
    return {
        "documents": len(manifest["documents"]),
        "nodes": len(manifest["nodes"]),
        "edges": len(manifest["edges"]),
        "scopes": len(scopes),
        "skipped_no_text": skipped_no_text,
    }


def main() -> None:
    corpus_dir = Path(sys.argv[1])
    summary = build_shipdir(corpus_dir)
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
