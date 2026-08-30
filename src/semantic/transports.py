"""Fetching half of the import pipeline: get documents, then hand them to
``import_documents``.

Fetching is deliberately separate from importing. An empty document list means
"upstream dropped everything", which the prune pass acts on — so a transport
that fails must raise rather than return ``[]``, and a caller must never be
able to confuse "unreachable" with "empty". ``import_source`` records the
failure against the source row and re-raises without importing anything.

Git credentials are NOT handled here. ``src.marketplace._run_git`` already
supplies a PAT through a host-scoped credential helper in the environment,
never on argv, and redacts it out of error text; this module reuses it rather
than growing a second, subtly different implementation of the same rule.

Provenance override (``config.provenance``)
-------------------------------------------
A source normally publishes under ``source='ossie_<kind>'`` /
``source_ref=<source id>``: the registered source id IS the prune boundary,
so two sources can never delete each other's models. A source migrated off a
legacy, connector-owned scheduled refresh (#1707 Block 3 step 3 — Keboola's
Metastore sync) may carry ``config.provenance = {"source": ...,
"source_ref": ...}`` to keep stamping the label that path already used.

Why, precisely: ``semantic_models``, ``metric_definitions``, ``glossary_terms``
and ``column_metadata`` rows are OWNED by their ``(source, source_ref)`` pair.
Every prune in this pipeline is scoped to that pair, every projected row id is
derived from it (``src.semantic.projection._scoped_id``), and the projector
even dispatches its table binding on it (``source == 'keboola_metastore'``
resolves a Keboola tableId through the registry). Importing the same upstream
under a *new* label would therefore neither update nor prune the existing rows
— it would write a second, parallel set beside them and leave the originals
orphaned forever, exactly the silent duplication the migration sequencing
exists to avoid. Continuity of the prune scope is the whole point.

The override is restricted three ways, because ``config`` reaches the
database from more than one writer and a claim is a licence to DELETE:

1. the LABEL must be one of :data:`_LEGACY_PROVENANCE_ADAPTERS` — one entry
   per legacy writer whose scheduled trigger moved onto the generic sweep;
2. the source must RUN that label's adapter — an ``upload``/``native`` source
   has no upstream a legacy label could describe, so it may never carry one;
3. the ``source_ref`` must be one the row can justify from its OWN config —
   its ``connection_id``, or (for the legacy env-credential row) the pair
   that path has ever stamped. The label alone is not the boundary: the ref
   is what selects whose rows a prune reaches, so checking only the label
   would let any source name connection A's ref and wipe A's models,
   metrics, glossary terms and column descriptions on its next sync.

``POST``/``PUT /api/admin/semantic-sources`` refuses ``config.provenance``
outright (``app/api/semantic_models.py``), so today the only writer is the
auto-migration itself, through the repository. These checks are the second
half of that story rather than a duplicate of it: they hold for a row written
by any future path, and they are what makes the CRUD refusal a defence in
depth instead of the only wall.

``config.safe_prune`` is the second knob the same migration needs: the
full-wipe guard the Keboola sync has always passed to ``project_document``
(an upstream that answers 200 with nothing usable must not delete an
installation's whole metric registry in one pass). Off by default, because a
git source emptying a model IS a real delete signal.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.marketplace import _redact, _run_git
from src.repositories import semantic_source_repo
from src.semantic.adapters import get_adapter
from src.semantic.importer import ImportReport, import_documents

_DEFAULT_GLOB = "**/*.yaml"

#: Provenance labels a source may claim through ``config.provenance``, mapped
#: to the adapter a claiming source MUST run — one entry per legacy writer
#: whose scheduled trigger moved onto the generic sweep. See the module
#: docstring for why this is an allowlist and not a free field.
_LEGACY_PROVENANCE_ADAPTERS = {"keboola_metastore": "keboola_metastore"}


def resolve_provenance(source: Dict[str, Any]) -> tuple[str, Optional[str]]:
    """The ``(source, source_ref)`` this source's rows are written and pruned
    under — its own id unless ``config.provenance`` claims a legacy label.

    Raises ``ValueError`` for a malformed or non-allowlisted override rather
    than falling back to the generic label: a source that asked to own one
    scope and silently wrote into another is the failure mode this whole
    mechanism exists to prevent, and ``import_source`` records the error on
    the row instead of importing anything.
    """
    source_id = source["id"]
    default = (f"ossie_{source.get('kind')}", source_id)

    override = (source.get("config") or {}).get("provenance")
    if override is None:
        return default
    if not isinstance(override, dict):
        raise ValueError(
            f"semantic source {source_id!r}: config.provenance must be an object, "
            f"got {type(override).__name__}"
        )

    label = override.get("source")
    if not isinstance(label, str) or not label.strip():
        raise ValueError(f"semantic source {source_id!r}: config.provenance.source must be a non-empty string")
    label = label.strip()
    if label not in _LEGACY_PROVENANCE_ADAPTERS:
        raise ValueError(
            f"semantic source {source_id!r}: config.provenance.source {label!r} is not a migrated legacy "
            f"provenance label (allowed: {', '.join(sorted(_LEGACY_PROVENANCE_ADAPTERS))}). A source may not "
            "claim another writer's prune scope."
        )

    required_adapter = _LEGACY_PROVENANCE_ADAPTERS[label]
    adapter = (source.get("adapter") or "").strip()
    if adapter != required_adapter:
        raise ValueError(
            f"semantic source {source_id!r}: config.provenance.source {label!r} may only be claimed by a "
            f"source running the {required_adapter!r} adapter, not {adapter or 'native'!r}. A source that "
            "does not read that upstream cannot own — or prune — the rows it writes."
        )

    # An explicit `"source_ref": null` is meaningful — it is what the legacy
    # Keboola env-credential path stamps — so "absent" and "present but None"
    # must not collapse into the same branch.
    if "source_ref" in override:
        ref = override["source_ref"]
        if ref is not None and not isinstance(ref, str):
            raise ValueError(f"semantic source {source_id!r}: config.provenance.source_ref must be a string or null")
    else:
        ref = source_id
    _assert_ref_is_the_sources_own(source_id=source_id, label=label, config=source.get("config") or {}, ref=ref)
    return label, ref


def _assert_ref_is_the_sources_own(*, source_id: str, label: str, config: Dict[str, Any], ref: Optional[str]) -> None:
    """Refuse a ``source_ref`` this row cannot justify from its own config.

    Connector knowledge in an otherwise generic module, deliberately and
    minimally: the label allowlist above already names one connector, and the
    question "is this ref yours?" can only be answered by the writer that owns
    the scope. Imported lazily, like every other connector reach-in on this
    path (``src/semantic/legacy_migration.py`` does the same).
    """
    if label != "keboola_metastore":  # pragma: no cover - the mapping above is the gate
        raise ValueError(f"semantic source {source_id!r}: no ownership rule for provenance label {label!r}")

    connection_id = str(config.get("connection_id") or "").strip()
    if connection_id:
        if ref != connection_id:
            raise ValueError(
                f"semantic source {source_id!r}: config.provenance.source_ref {ref!r} is not this source's "
                f"own connection ({connection_id!r}). A source may only own — and prune — the scope of the "
                "connection it reads."
            )
        return

    if config.get("legacy_credentials"):
        # The env-credential path stamps NULL, or the default connection's id
        # when the credentials actually resolved from that connection; both,
        # and nothing else, are its own scope.
        from connectors.keboola.semantic_layer import legacy_credentials_prune_scope

        allowed = legacy_credentials_prune_scope()
        if ref not in allowed:
            raise ValueError(
                f"semantic source {source_id!r}: config.provenance.source_ref {ref!r} is outside the legacy "
                "Keboola credential path's own scope (null, or the default connection's id)."
            )
        return

    raise ValueError(
        f"semantic source {source_id!r}: a source claiming the {label!r} provenance must pin the connection "
        "it reads (config.connection_id) or declare config.legacy_credentials."
    )


def _clone(*, repo_url: str, ref: Optional[str], token_env: Optional[str], dest: Path) -> Path:
    """Shallow-clone ``repo_url`` into ``dest`` and return the clone root."""
    token = os.environ.get(token_env, "") if token_env else ""
    args = ["clone", "--depth", "1"]
    if ref:
        args += ["--branch", ref]
    args += [repo_url, str(dest)]
    try:
        _run_git(args, url=repo_url, token=token)
    except subprocess.CalledProcessError as exc:
        detail = _redact(exc.stderr or "", token).strip() or f"git exited {exc.returncode}"
        raise RuntimeError(f"clone failed: {detail}") from None
    return dest


def _documents_from_clone(root: Path, glob: str) -> List[str]:
    """Read every file the glob matches, dropping anything outside the clone.

    A cloned repository is untrusted input — whoever can push to it chooses the
    filenames, and a symlink is a filename. Each match is resolved and kept only
    if the resolved path is still inside the resolved clone root, so a link to
    an absolute path elsewhere on the host reads as nothing at all.
    """
    root = root.resolve()
    documents: List[str] = []
    for path in sorted(root.glob(glob)):
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            continue
        if not resolved.is_file():
            continue
        documents.append(resolved.read_text())
    return documents


def load_documents(source: Dict[str, Any]) -> List[str]:
    """Fetch this source's payload and run it through its adapter."""
    kind = (source.get("kind") or "").strip()
    config = source.get("config") or {}
    adapter = get_adapter(source.get("adapter") or "native")

    if kind == "upload":
        payload: Dict[str, Any] = {"documents": list(config.get("documents") or [])}
    elif kind == "git":
        repo_url = (config.get("repo_url") or "").strip()
        if not repo_url:
            raise ValueError("git semantic source requires config.repo_url")
        with tempfile.TemporaryDirectory(prefix="agnes-semantic-") as tmp:
            root = _clone(
                repo_url=repo_url,
                ref=(config.get("ref") or "").strip() or None,
                token_env=(config.get("token_env") or "").strip() or None,
                dest=Path(tmp) / "clone",
            )
            payload = {"documents": _documents_from_clone(Path(root), config.get("glob") or _DEFAULT_GLOB)}
    elif kind == "connection":
        # The adapter owns the fetch for connection-backed sources; it gets the
        # connection config verbatim and returns documents.
        payload = dict(config)
    else:
        raise ValueError(f"unknown semantic source kind {kind!r} (expected git, upload or connection)")

    return adapter.extract(payload)


def import_source(source_id: str) -> ImportReport:
    """Fetch and import one registered source, recording the outcome on it."""
    repo = semantic_source_repo()
    source = repo.get(source_id)
    if source is None:
        raise LookupError(
            f"semantic source {source_id!r} not found — list them with `agnes admin semantic source list`"
        )

    try:
        # Resolved BEFORE the fetch: a source whose provenance override is
        # malformed must fail without making an upstream call, and certainly
        # without importing anything under the wrong scope.
        src_name, src_ref = resolve_provenance(source)
        documents = load_documents(source)
        report = import_documents(
            {
                **source,
                # Provenance: the registered source id is the prune boundary, so
                # two git sources can never delete each other's models even when
                # they carry the same model names. A migrated legacy source
                # overrides it to keep owning the rows it already wrote — see
                # the module docstring.
                "source": src_name,
                "source_ref": src_ref,
                "safe_prune": bool((source.get("config") or {}).get("safe_prune")),
            },
            documents,
        )
    except Exception as exc:
        repo.record_sync(source_id, status="error", error=str(exc))
        raise

    repo.record_sync(source_id, status="ok", error=None)
    return report
