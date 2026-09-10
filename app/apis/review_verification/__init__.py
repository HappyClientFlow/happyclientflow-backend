"""
Cron / ops: re-process pending external review verifications (scraper + heuristics).
"""

import threading

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

import databutton as db

from app.libs.review_verification import (
    count_pending_verification_retries,
    process_pending_verification_retries,
)

router = APIRouter()


class TriggerReviewVerificationRequest(BaseModel):
    company_id: str
    limit: int = 40


@router.post("/process-review-verification-retries")
def process_review_verification_retries(
    authorization: str | None = Header(None, alias="Authorization"),
):
    """
    Re-runs verification for clients with pending/inconclusive rows.
    Set secret REVIEW_VERIFICATION_CRON_SECRET; call with header:
    Authorization: Bearer <secret>
    If the secret is not configured, the endpoint is open (not recommended for production).
    """
    secret = db.secrets.get("REVIEW_VERIFICATION_CRON_SECRET")
    if secret:
        expected = f"Bearer {secret}"
        if (authorization or "").strip() != expected:
            raise HTTPException(status_code=401, detail="Unauthorized")

    n = process_pending_verification_retries()
    return {"processed_clients": n}


@router.post("/trigger-review-verification-recheck")
def trigger_review_verification_recheck(body: TriggerReviewVerificationRequest):
    """
    Dashboard helper: trigger async re-check for one company.
    No cron required; returns immediately.
    """
    company_id = (body.company_id or "").strip()
    if not company_id:
        raise HTTPException(status_code=400, detail="company_id is required")
    limit = max(1, min(int(body.limit or 40), 200))

    # Count first so the caller can say what actually happened. The work itself
    # runs in a thread and its result never reaches the UI, so reporting "started"
    # unconditionally told users something had begun even when there was nothing
    # pending and no fetch would occur.
    pending = count_pending_verification_retries(limit=limit, company_id=company_id)

    if not pending:
        return {
            "ok": True,
            "status": "nothing_pending",
            "company_id": company_id,
            "limit": limit,
            "pending": 0,
        }

    def _run() -> None:
        try:
            process_pending_verification_retries(limit=limit, company_id=company_id)
        except Exception as e:
            print(
                "trigger_review_verification_recheck: background error "
                f"company_id={company_id!r}: {e}"
            )

    threading.Thread(target=_run, daemon=True).start()
    return {
        "ok": True,
        "status": "started",
        "company_id": company_id,
        "limit": limit,
        "pending": pending,
    }
