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
        documents = load_documents(source)
        report = import_documents(
            {
                **source,
                # Provenance: the registered source id is the prune boundary, so
                # two git sources can never delete each other's models even when
                # they carry the same model names.
                "source": f"ossie_{source.get('kind')}",
                "source_ref": source_id,
            },
            documents,
        )
    except Exception as exc:
        repo.record_sync(source_id, status="error", error=str(exc))
        raise

    repo.record_sync(source_id, status="ok", error=None)
    return report
