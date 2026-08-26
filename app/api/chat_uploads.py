"""Chat workspace file upload endpoint.

POST /api/chat/uploads
  Auth: any user with chat access (require_chat_access — the same
  ResourceType.CHAT gate the rest of the chat API uses; Admin short-circuits).
  Accepts a multipart file upload plus metadata fields:
    - kind: "data" | "image" | "document"
    - register_as_table: optional "true"/"false" (data files only)
    - table_name: optional name for the registered table

  Writes the file into the caller's per-user workspace under an ``uploads/``
  subdirectory so it reaches their chat sandbox on next spawn (this
  endpoint enforces a per-file size cap).

  When register_as_table=true on a data file (CSV/parquet/XLSX), the file is
  also registered as a workspace-local queryable table by writing/refreshing
  an extract.duckdb under the workspace uploads area following the connector
  _meta contract.  The server-side admin table_registry is NOT mutated — the
  admin gate on /api/admin/register-table is unchanged.
"""

from __future__ import annotations

import logging
import re
import tempfile
from enum import Enum
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, UploadFile, File
from pydantic import BaseModel

from app.auth.access import require_resource_access
from app.chat.workdir import _safe_email_dir
from app.corpus_ingest import create_single_file_artefact
from app.resource_types import ResourceType
from app.utils import get_data_dir
from src.sql_ident import quote_ident

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])

# Same resource gate as the rest of the chat API (app/api/chat.py): the caller
# must have the "Cloud chat" feature grant (or be an Admin).  A user denied
# chat access must not be able to write into a chat workspace via uploads.
require_chat_access = require_resource_access(ResourceType.CHAT, "chat")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Per-file cap for chat uploads.  Set well under the 100 MB workspace sync
# ceiling so a single upload cannot saturate the sync budget.
MAX_CHAT_UPLOAD_BYTES: int = 20 * 1024 * 1024  # 20 MB

_CHUNK_SIZE = 64 * 1024  # 64 KB read chunks

# Allowed content types per kind.
_ALLOWED_CONTENT_TYPES: dict[str, frozenset[str]] = {
    "data": frozenset(
        {
            "text/csv",
            "text/plain",
            "application/vnd.ms-excel",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/octet-stream",  # parquet is binary
            "application/parquet",
        }
    ),
    "image": frozenset(
        {
            "image/png",
            "image/jpeg",
            "image/gif",
            "image/webp",
            "image/svg+xml",
        }
    ),
    "document": frozenset(
        {
            "application/pdf",
            "text/plain",
            "text/markdown",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }
    ),
}

# Extensions that qualify as data files for table registration.
_DATA_EXTENSIONS: frozenset[str] = frozenset({".csv", ".parquet", ".xlsx", ".xls"})

# Safe filename: alphanumeric, dot, dash, underscore.  No path separators,
# no leading dots (hidden files), no double-dots (traversal).
_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,199}$")


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class UploadKind(str, Enum):
    data = "data"
    image = "image"
    document = "document"


class ChatUploadResponse(BaseModel):
    filename: str
    workspace_path: str
    size_bytes: int
    kind: str
    table_name: Optional[str] = None
    # Slug of the single-file artefact this upload was also saved as, when the
    # file is a document/image (data files stay workspace-only). None when no
    # artefact was created. Reachable at /library/{artefact_slug}.
    artefact_slug: Optional[str] = None
    hint: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user_uploads_dir(email: str) -> Path:
    """Return (and ensure) the uploads sub-directory in the user workspace."""
    slug = _safe_email_dir(email)
    uploads = get_data_dir() / "users" / slug / "workspace" / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    return uploads


def _validate_filename(raw: str | None) -> str:
    """Sanitize and validate an uploaded filename.

    Raises HTTPException(400) if the name is missing, empty, contains path
    separators, traversal sequences, or double-dots.
    """
    if not raw:
        raise HTTPException(status_code=400, detail="filename is required and must not be empty")
    # Strip any directory components supplied by the browser.
    name = Path(raw).name
    if not name or name != raw.replace("/", "").replace("\\", ""):
        # The browser-supplied name had directory components — likely traversal.
        raise HTTPException(
            status_code=400,
            detail=(
                f"filename '{raw}' contains path separators or directory components. "
                "Upload a plain filename without directory prefixes."
            ),
        )
    if ".." in name:
        raise HTTPException(
            status_code=400,
            detail=(f"filename '{name}' contains '..'. Choose a filename without traversal sequences."),
        )
    if not _SAFE_FILENAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail=(
                f"filename '{name}' contains disallowed characters. "
                "Use only letters, digits, dots, dashes, and underscores."
            ),
        )
    return name


def _derive_table_name(stem: str) -> str:
    """Derive a safe DuckDB table name from a filename stem."""
    # Replace any non-identifier character with underscore.
    clean = re.sub(r"[^A-Za-z0-9_]", "_", stem)
    # Collapse consecutive underscores.
    clean = re.sub(r"_+", "_", clean).strip("_")
    # Ensure starts with a letter or underscore (not a digit).
    if clean and clean[0].isdigit():
        clean = "t_" + clean
    return clean or "upload"


async def _stream_to_temp(file: UploadFile) -> tuple[Path, int]:
    """Stream upload into a temp file enforcing the per-file cap.

    Returns (temp_path, total_bytes).  Raises HTTPException(413) if the file
    exceeds MAX_CHAT_UPLOAD_BYTES.  The caller is responsible for unlinking
    the temp path.
    """
    with tempfile.NamedTemporaryFile(delete=False, suffix=".tmp") as tmp:
        tmp_path = Path(tmp.name)
        total = 0
        try:
            while True:
                chunk = await file.read(_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_CHAT_UPLOAD_BYTES:
                    tmp_path.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"File exceeds the {MAX_CHAT_UPLOAD_BYTES // 1024 // 1024} MB "
                            "per-file chat upload limit. "
                            "For large files use `agnes pull` / the data sync pipeline."
                        ),
                    )
                tmp.write(chunk)
            tmp.flush()
        except HTTPException:
            raise
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
    return tmp_path, total


def _register_workspace_table(
    uploads_dir: Path,
    file_path: Path,
    table_name: str,
) -> None:
    """Register a data file as a workspace-local queryable table.

    Writes/refreshes an ``extract.duckdb`` under ``uploads_dir`` following
    the connector _meta contract so `agnes query` sees it in-session.  The
    data is **materialized** into a real DuckDB table (not a view over the
    source file) so the ``extract.duckdb`` is self-contained: when the
    workspace syncs into the chat sandbox, the table travels with it and
    stays queryable regardless of where the source file lands (a view over
    an absolute file path would dangle after the sync).  Chat uploads are
    capped at 20 MB, so materializing is cheap.

    This intentionally does NOT mutate the server-side admin table_registry.
    """
    import time

    from src.duckdb_conn import _open_duckdb

    extract_db_path = uploads_dir / "extract.duckdb"
    # Route through _open_duckdb so the session timezone is pinned to UTC
    # (enforced by tests/test_duckdb_session_tz.py — no bare duckdb.connect).
    conn = _open_duckdb(str(extract_db_path))
    try:
        # Ensure _meta table exists (connector _meta contract).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS _meta (
                table_name VARCHAR,
                description VARCHAR,
                rows BIGINT,
                size_bytes BIGINT,
                extracted_at VARCHAR,
                query_mode VARCHAR
            )
            """
        )
        # Materialize the file into a self-contained table (see docstring).
        suffix = file_path.suffix.lower()
        if suffix == ".csv":
            read_expr = f"read_csv_auto('{file_path}')"
        elif suffix == ".parquet":
            read_expr = f"read_parquet('{file_path}')"
        elif suffix in (".xlsx", ".xls"):
            # read_xlsx needs the DuckDB excel extension, which is not always
            # auto-loaded (e.g. offline/air-gapped deployments). Load it first
            # with an actionable error — matching src/ingest/tabular.py.
            try:
                conn.execute("INSTALL excel; LOAD excel;")
            except Exception as exc:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Reading Excel files requires the DuckDB 'excel' extension, "
                        f"which is unavailable on this server ({exc}). "
                        "Upload the data as CSV or Parquet instead."
                    ),
                ) from exc
            read_expr = f"read_xlsx('{file_path}')"
        else:
            read_expr = f"read_csv_auto('{file_path}')"

        # Drop any prior object of the same name first. CREATE OR REPLACE TABLE
        # refuses to replace an object of a different type — e.g. a pre-fix
        # extract.duckdb where this name was a VIEW — so clear both types.
        conn.execute(f"DROP VIEW IF EXISTS {quote_ident(table_name)}")
        conn.execute(f"DROP TABLE IF EXISTS {quote_ident(table_name)}")
        conn.execute(f"CREATE TABLE {quote_ident(table_name)} AS SELECT * FROM {read_expr}")

        # Count rows for _meta (best-effort; zero on error).
        try:
            row_count = conn.execute(f"SELECT COUNT(*) FROM {quote_ident(table_name)}").fetchone()[0]
        except Exception:
            row_count = 0

        size_bytes = file_path.stat().st_size
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # Upsert into _meta.
        conn.execute("DELETE FROM _meta WHERE table_name = ?", [table_name])
        conn.execute(
            "INSERT INTO _meta VALUES (?, ?, ?, ?, ?, ?)",
            [table_name, f"Uploaded file: {file_path.name}", row_count, size_bytes, now_iso, "local"],
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/uploads", response_model=ChatUploadResponse)
async def chat_upload(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    kind: UploadKind = Form(...),
    register_as_table: bool = Form(False),
    table_name: Optional[str] = Form(None),
    user: dict = Depends(require_chat_access),
) -> ChatUploadResponse:
    """Upload a file into your chat workspace.

    The file lands under ``uploads/`` in your per-user workspace so it is
    available in your next chat sandbox session (synced on spawn).

    Args (multipart/form-data):
        file: The file to upload.
        kind: One of ``data``, ``image``, ``document``.
        register_as_table: When ``true`` (data files only), register the file
            as a workspace-local queryable table so ``agnes query`` can reach
            it in-session.  Does NOT mutate the server admin table_registry.
        table_name: Optional name for the registered table.  Defaults to the
            filename stem (sanitized).

    Returns:
        workspace_path: Workspace-relative path of the uploaded file.
        filename: Sanitized filename on disk.
        size_bytes: Number of bytes written.
        kind: Echoed kind.
        table_name: Name of the registered table (null when not registered).
        hint: Next-step hint pointing to ``agnes query`` or the chat sandbox.
    """
    # --- reject co-session principals --------------------------------------
    # require_chat_access can return a SessionPrincipal (co-drive runner token)
    # rather than a user dict; those have no personal workspace and no
    # ``["email"]`` key, so uploads (which write into a per-user workspace) are
    # not meaningful for them. Fail closed with 403 instead of 500ing on the
    # dict subscript below.
    from app.auth.session_principal import SessionPrincipal

    if isinstance(user, SessionPrincipal) or not isinstance(user, dict) or not user.get("email"):
        raise HTTPException(
            status_code=403,
            detail="Chat uploads require a personal user workspace.",
        )

    # --- validate filename --------------------------------------------------
    safe_name = _validate_filename(file.filename)

    # --- validate content type ---------------------------------------------
    ct = (file.content_type or "").split(";")[0].strip().lower()
    allowed_ct = _ALLOWED_CONTENT_TYPES[kind.value]
    if ct and ct not in allowed_ct:
        raise HTTPException(
            status_code=415,
            detail=(
                f"Content type '{ct}' is not allowed for kind='{kind.value}'. "
                f"Allowed: {sorted(allowed_ct)}. "
                "Rename the file or choose the correct kind."
            ),
        )

    # --- validate register_as_table semantics ------------------------------
    if register_as_table and kind != UploadKind.data:
        raise HTTPException(
            status_code=400,
            detail=(
                f"register_as_table is only supported for kind='data' (CSV/parquet/XLSX). Received kind='{kind.value}'."
            ),
        )

    # --- stream file to temp ------------------------------------------------
    tmp_path, size_bytes = await _stream_to_temp(file)

    dest: Optional[Path] = None  # set once the temp file is moved into place
    try:
        # --- write to workspace uploads dir --------------------------------
        email: str = user["email"]
        uploads_dir = _user_uploads_dir(email)
        dest = uploads_dir / safe_name
        # Atomic: move from temp (avoids partial reads of a partially-written file).
        import shutil

        shutil.move(str(tmp_path), str(dest))
        tmp_path = dest  # update reference so finally-block knows the move succeeded

        # --- optional table registration ------------------------------------
        resolved_table_name: Optional[str] = None
        registration_error: Optional[str] = None
        if register_as_table:
            if table_name:
                candidate_table_name = _derive_table_name(table_name)
            else:
                candidate_table_name = _derive_table_name(Path(safe_name).stem)
            try:
                _register_workspace_table(uploads_dir, dest, candidate_table_name)
                resolved_table_name = candidate_table_name
            except HTTPException as exc:
                # The file uploaded fine; only table registration failed (e.g.
                # the DuckDB 'excel' extension is unavailable on this server).
                # Keep the file in the workspace and surface the failure in the
                # response instead of returning an error for a file that did
                # land on disk — mirroring the library upload flow, which keeps
                # the collection and reports the per-file rejection reason
                # rather than discarding a successful upload.
                registration_error = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
                logger.warning(
                    "chat_upload: table registration failed for user=%s file=%s: %s",
                    email,
                    safe_name,
                    registration_error,
                )

        # --- also persist docs/images as a single-file artefact -------------
        # A document/image dropped in chat shouldn't vanish with the session:
        # persist + index it as a private single-file artefact (it then shows
        # in Artefacts and is searchable). Data files stay workspace-only —
        # their job is to become a queryable table, not searchable prose.
        # Best-effort: a failure here never fails the workspace upload the user
        # actually asked for.
        artefact_slug: Optional[str] = None
        if kind in (UploadKind.image, UploadKind.document) and user.get("id"):
            try:
                created = create_single_file_artefact(
                    owner_id=user["id"],
                    filename=safe_name,
                    data=dest.read_bytes(),
                )
                if created:
                    artefact_slug = (created.get("collection") or {}).get("slug")
                    from src.ingest.runner import ingest_file

                    background_tasks.add_task(ingest_file, created["file_id"])
            except Exception:
                logger.warning(
                    "chat_upload: could not save artefact for user=%s file=%s",
                    email,
                    safe_name,
                    exc_info=True,
                )

        # --- workspace-relative path ----------------------------------------
        slug = _safe_email_dir(email)
        ws_root = get_data_dir() / "users" / slug / "workspace"
        try:
            rel_path = str(dest.relative_to(ws_root))
        except ValueError:
            rel_path = safe_name

        # --- hint -----------------------------------------------------------
        if resolved_table_name:
            hint = (
                f"File registered as table '{resolved_table_name}'. "
                f'Query it with: agnes query "SELECT * FROM {resolved_table_name} LIMIT 10"'
            )
        elif registration_error:
            hint = (
                f"File '{safe_name}' was uploaded to your workspace, but "
                f"registering it as a table failed: {registration_error} "
                "The file is still available in your next chat sandbox session — "
                "query it there directly, or re-upload as CSV/Parquet to register it."
            )
        else:
            hint = (
                f"File '{safe_name}' is in your workspace uploads folder. "
                "It will be available in your next chat sandbox session."
            )
        if artefact_slug:
            hint += " Saved to your Artefacts."

        logger.info(
            "chat_upload: user=%s kind=%s file=%s size=%d table=%s",
            email,
            kind.value,
            safe_name,
            size_bytes,
            resolved_table_name,
        )

        return ChatUploadResponse(
            filename=safe_name,
            workspace_path=rel_path,
            size_bytes=size_bytes,
            kind=kind.value,
            table_name=resolved_table_name,
            artefact_slug=artefact_slug,
            hint=hint,
        )

    except HTTPException:
        raise
    except Exception:
        # Clean up temp if move failed.
        try:
            if tmp_path and tmp_path.exists() and tmp_path != dest:
                tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        logger.exception("chat_upload: unexpected error for user=%s file=%s", user.get("email"), safe_name)
        raise
