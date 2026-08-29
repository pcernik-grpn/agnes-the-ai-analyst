"""Telegram integration endpoints — verify, unlink, status."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import duckdb

from app.auth.dependencies import get_current_user, _get_db

from src.audit_helpers import log_safe
from src.repositories import (
    notifications_pending_code_repo,
    notifications_telegram_repo,
)

router = APIRouter(prefix="/api/telegram", tags=["telegram"])


class VerifyRequest(BaseModel):
    code: str


@router.post("/verify")
async def telegram_verify(
    request: VerifyRequest,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Verify a code to link Telegram account."""
    code_repo = notifications_pending_code_repo()
    code_data = code_repo.verify_code(request.code)
    if not code_data:
        raise HTTPException(status_code=400, detail="Invalid or expired code")

    tg_repo = notifications_telegram_repo()
    tg_repo.link_user(user["id"], chat_id=code_data["chat_id"])
    log_safe(user_id=user["id"], action="telegram.bind", params={"chat_id": code_data["chat_id"]})
    return {"status": "linked", "chat_id": code_data["chat_id"]}


@router.post("/unlink")
async def telegram_unlink(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Unlink Telegram account."""
    tg_repo = notifications_telegram_repo()
    tg_repo.unlink_user(user["id"])
    return {"status": "unlinked"}


@router.get("/status")
async def telegram_status(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get current Telegram link status."""
    tg_repo = notifications_telegram_repo()
    link = tg_repo.get_link(user["id"])
    if link:
        return {"linked": True, "chat_id": link["chat_id"], "linked_at": str(link.get("linked_at", ""))}
    return {"linked": False}
