"""
This API endpoint provides a secure method for creating feedback entries.
It is designed to be called from the frontend when an anonymous or unauthenticated user
submits feedback. This endpoint uses a service-level Supabase client to bypass
Row Level Security (RLS) policies, ensuring that feedback can be created by any user.

Reminder lifecycle: /v1/reminders/process; /mark-external-review-clicked cancels survey
reminders. After internal feedback is saved, /create-feedback cancels pending *survey*
reminders (1./2. Erinnerung chain), then may schedule Google-review follow-ups — so clients
are not left with four stacked reminders.

4-star internal follow-up:
  POST /submit-internal-feedback  — saves optional callback note/request and
  sends a SendGrid notification e-mail to the company owner.
"""

import threading

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List, Optional, Tuple
from supabase import Client, create_client
import databutton as db
import datetime

from app.libs.reminder_scheduling import (
    cancel_pending_reminders_for_client,
    cancel_pending_survey_reminders_for_client,
    cancel_pending_survey_reminders_with_retries,
)
from app.libs.google_review_reminder_scheduling import (
    schedule_google_review_followup_reminders_after_feedback,
    cancel_pending_google_review_followup_reminders,
)
from app.libs.email_builder import build_sendgrid_mail

router = APIRouter()


class FeedbackContent(BaseModel):
    products: Optional[List[str]] = None
    employee: Optional[str] = None
    other_product: Optional[str] = None
    other_employee: Optional[str] = None
    collaboration_feeling: Optional[str] = None
    highlight: Optional[str] = None
    improvements: Optional[str] = None


class CreateFeedbackRequest(BaseModel):
    client_id: str
    satisfaction: int
    recommendation: str
    content: FeedbackContent
    reviewer_name_hint: Optional[str] = None


class AttachReviewDraftRequest(BaseModel):
    feedback_id: str
    client_id: str
    review_draft_text: str
    reviewer_name_hint: Optional[str] = None


class MarkGoogleReviewPublishedRequest(BaseModel):
    client_id: str


class MarkGoogleReviewClickedRequest(BaseModel):
    client_id: str


class MarkExternalReviewClickedRequest(BaseModel):
    client_id: str
    profile_type: Optional[str] = None


class SubmitInternalFeedbackRequest(BaseModel):
    """
    Payload for the 4-star internal follow-up screen.
    Both fields are optional — the client may submit a note, request a callback,
    or do neither (skip).  Even a skip is recorded so we have a clear paper-trail.
    """
    client_id: str
    feedback_id: str
    callback_requested: Optional[bool] = False
    callback_note: Optional[str] = None


class SubmitInternalFeedbackResponse(BaseModel):
    ok: bool
    message: str


def get_supabase_service_client():
    """Initializes and returns a Supabase client with the service role key."""
    supabase_url = db.secrets.get("SUPABASE_URL")
    service_key = db.secrets.get("SUPABASE_SERVICE_KEY")
    if not supabase_url or not service_key:
        raise HTTPException(status_code=500, detail="Supabase configuration missing.")
    return create_client(supabase_url, service_key)


# review_status is a forward-only funnel. Ranked lowest→highest so an external-review
# click can never drag a record backwards: a late/duplicate click (e.g. the customer
# re-opens the feedback link after the review was already verified as ReviewComplete)
# must NOT revert "Abgeschlossen" back to "Externe Bewertung gestartet".
# Unknown/NULL statuses rank 0 → a click still advances them to ExternalReviewStarted.
_REVIEW_STATUS_RANK = {
    "ReviewSent": 1,
    "ReviewStarted": 2,
    "FeedbackSubmitted": 3,
    "ExternalReviewStarted": 4,
    "ReviewComplete": 5,
}
_EXTERNAL_REVIEW_STARTED_RANK = _REVIEW_STATUS_RANK["ExternalReviewStarted"]


def _record_external_review_platform_click(supabase: Client, client_id: str) -> dict:
    """
    Cancels pending *survey* reminders, sets clicked_google_link (any external review CTA),
    and advances review_status to ExternalReviewStarted (off-site review opened, not verified).

    review_status is monotonic: if the client has already reached a state at or beyond
    ExternalReviewStarted (notably ReviewComplete, set when a public review is verified),
    the status is PRESERVED — a repeat/late click never downgrades it. clicked_google_link
    and external_review_clicked_at are still refreshed.
    Use ReviewComplete when publication is confirmed (e.g. mark_google_review_published).
    Google-review follow-up nudges stay pending until google_review_published is set.
    """
    n = cancel_pending_survey_reminders_for_client(supabase, client_id)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # Read current status first so we never write a status lower than the current one.
    current_res = (
        supabase.from_("clients")
        .select("review_status")
        .eq("id", client_id)
        .limit(1)
        .execute()
    )
    current_rows = getattr(current_res, "data", None) or []
    current_status = current_rows[0].get("review_status") if current_rows else None
    already_advanced = (
        _REVIEW_STATUS_RANK.get(current_status or "", 0) >= _EXTERNAL_REVIEW_STARTED_RANK
    )

    update_payload = {
        "clicked_google_link": True,
        "external_review_clicked_at": now,
    }
    if already_advanced:
        # Preserve ExternalReviewStarted/ReviewComplete — do not downgrade.
        print(
            f"mark_external_review_clicked: preserving review_status={current_status!r} "
            f"for client_id={client_id!r} (no downgrade on repeat click)"
        )
    else:
        update_payload["review_status"] = "ExternalReviewStarted"

    supabase.from_("clients").update(update_payload).eq("id", client_id).execute()

    verify = (
        supabase.from_("clients")
        .select("id, clicked_google_link, review_status")
        .eq("id", client_id)
        .limit(1)
        .execute()
    )
    vdata = getattr(verify, "data", None) or []
    row = vdata[0] if vdata else None
    if not row:
        raise HTTPException(
            status_code=404,
            detail="Client not found after update.",
        )
    final_status = row.get("review_status")
    # Success = click recorded AND status is at least ExternalReviewStarted
    # (either freshly advanced, or a preserved higher state like ReviewComplete).
    if (
        not row.get("clicked_google_link")
        or _REVIEW_STATUS_RANK.get(final_status or "", 0) < _EXTERNAL_REVIEW_STARTED_RANK
    ):
        print(
            f"mark_external_review_clicked: update did not persist for client_id={client_id!r} "
            f"row={row!r}"
        )
        raise HTTPException(
            status_code=404,
            detail="Could not save external review click (column missing or RLS?).",
        )

    return {
        "message": "OK",
        "reminders_cancelled": n,
        "clicked_google_link": row.get("clicked_google_link"),
        "review_status": final_status,
    }


@router.post("/create-feedback")
def create_feedback(
    request: CreateFeedbackRequest,
    supabase: Client = Depends(get_supabase_service_client),
):
    """
    Creates a new feedback entry in the database.
    """
    try:
        feedback_insert_data = {
            "client_id": request.client_id,
            "satisfaction": request.satisfaction,
            "recommendation": request.recommendation,
            "content": request.content.model_dump(),
        }
        hint = (request.reviewer_name_hint or "").strip()
        if hint:
            feedback_insert_data["reviewer_name_hint"] = hint

        response = supabase.from_("feedback").insert(feedback_insert_data).execute()

        if not response.data:
            raise HTTPException(status_code=500, detail="Failed to create feedback entry.")

        # Anonymous feedback page cannot update clients via browser Supabase (RLS).
        # Service client sets survey-complete status here.
        cid = (request.client_id or "").strip()
        if cid:
            try:
                supabase.from_("clients").update(
                    {"review_status": "FeedbackSubmitted"}
                ).eq("id", cid).execute()
            except Exception as upd_err:
                # Do not fail the request: feedback row is already stored; client may retry and duplicate.
                print(
                    f"create_feedback: WARNING could not set FeedbackSubmitted for client_id={cid!r}: {upd_err}"
                )

            cancel_result = cancel_pending_survey_reminders_with_retries(
                supabase, cid
            )
            if cancel_result.ok:
                if cancel_result.cancelled:
                    print(
                        f"create_feedback: cancelled {cancel_result.cancelled} "
                        f"pending survey reminder(s) for client_id={cid!r}"
                    )
            else:
                print(
                    f"create_feedback: REMINDER_CANCEL_FAILED client_id={cid!r} "
                    f"cancelled={cancel_result.cancelled} "
                    f"pending_survey_after={cancel_result.pending_survey_after} "
                    f"error={cancel_result.last_error!r}; "
                    "skipping Google follow-up schedule to avoid stacked reminders"
                )

            if cancel_result.ok:
                try:
                    schedule_google_review_followup_reminders_after_feedback(
                        supabase,
                        client_id=cid,
                        satisfaction=request.satisfaction,
                    )
                except Exception as sched_err:
                    print(
                        f"create_feedback: WARNING could not schedule Google review reminders "
                        f"for client_id={cid!r}: {sched_err}"
                    )

        return response.data[0]

    except HTTPException:
        raise
    except Exception as e:
        print(f"Error creating feedback: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/attach-review-draft")
def attach_review_draft(
    body: AttachReviewDraftRequest,
    supabase: Client = Depends(get_supabase_service_client),
):
    """Persist AI-generated review text for scraper-based verification (after /create-feedback)."""
    draft = (body.review_draft_text or "").strip()
    if not draft:
        raise HTTPException(status_code=400, detail="review_draft_text is required.")
    fid = (body.feedback_id or "").strip()
    cid = (body.client_id or "").strip()
    if not fid or not cid:
        raise HTTPException(status_code=400, detail="feedback_id and client_id are required.")
    chk = (
        supabase.from_("feedback")
        .select("id")
        .eq("id", fid)
        .eq("client_id", cid)
        .limit(1)
        .execute()
    )
    if not (getattr(chk, "data", None) or []):
        raise HTTPException(status_code=404, detail="Feedback not found for this client.")
    upd: dict = {"review_draft_text": draft}
    if body.reviewer_name_hint is not None:
        upd["reviewer_name_hint"] = (body.reviewer_name_hint or "").strip() or None
    supabase.from_("feedback").update(upd).eq("id", fid).execute()
    return {"ok": True}


@router.post("/mark-external-review-clicked")
def mark_external_review_clicked(
    body: MarkExternalReviewClickedRequest,
    supabase: Client = Depends(get_supabase_service_client),
):
    """
    Cancels pending reminders and sets clicked_google_link when the user opens any
    external review CTA (Google, Anwalt.de, Trustpilot, etc.).
    """
    try:
        client_id = (body.client_id or "").strip()
        if not client_id:
            raise HTTPException(status_code=400, detail="client_id is required.")

        if body.profile_type:
            print(
                f"mark_external_review_clicked: client_id={client_id!r} "
                f"profile_type={body.profile_type!r}"
            )

        result = _record_external_review_platform_click(supabase, client_id)
        pt = (body.profile_type or "").strip() or None

        def _run_verify_bg() -> None:
            try:
                from app.libs.review_verification import (
                    run_external_review_verification_sync,
                )

                run_external_review_verification_sync(client_id, pt)
            except Exception as e:
                print(
                    f"mark_external_review_clicked: review_verification background error: {e}"
                )

        threading.Thread(target=_run_verify_bg, daemon=True).start()
        return result
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in mark_external_review_clicked for {body.client_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/mark-google-review-published")
def mark_google_review_published(
    body: MarkGoogleReviewPublishedRequest,
    supabase: Client = Depends(get_supabase_service_client),
):
    """
    Marks that the client has (been confirmed to have) published a Google review.
    Cancels pending google_review_followup reminders.

    Not exposed on the SPA Brain client — call from backend jobs, admin tools, or
    future server-side integrations only.
    """
    client_id = (body.client_id or "").strip()
    if not client_id:
        raise HTTPException(status_code=400, detail="client_id is required.")
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        supabase.from_("clients").update(
            {
                "google_review_published": True,
                "google_review_published_at": now.isoformat(),
                "review_status": "ReviewComplete",
            }
        ).eq("id", client_id).execute()
        n = cancel_pending_google_review_followup_reminders(supabase, client_id)
        return {
            "message": "OK",
            "pending_google_review_reminders_cancelled": n,
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in mark_google_review_published for {body.client_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/mark-google-review-clicked")
def mark_google_review_clicked(
    body: MarkGoogleReviewClickedRequest,
    supabase: Client = Depends(get_supabase_service_client),
):
    """
    Legacy alias for mark-external-review-clicked (Google-only clients).
    """
    try:
        client_id = (body.client_id or "").strip()
        if not client_id:
            raise HTTPException(status_code=400, detail="client_id is required.")

        return _record_external_review_platform_click(supabase, client_id)
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in mark_google_review_clicked for {body.client_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Helpers for the 4-star internal follow-up
# ---------------------------------------------------------------------------

def _get_company_owner_email_and_language(
    supabase: Client, company_id: str
) -> Tuple[Optional[str], str]:
    """
    Returns (owner_email, language) for the company owner.
    `language` is the user's `users.language` (typically 'de' or 'en'); defaults to 'de'.
    """
    try:
        company_res = supabase.from_("companies").select("owner_id, name").eq("id", company_id).single().execute()
        if not company_res.data:
            return None, "de"
        owner_id = company_res.data.get("owner_id")
        if not owner_id:
            return None, "de"
        user_res = supabase.from_("users").select("email, language").eq("id", owner_id).single().execute()
        data = user_res.data or {}
        email = data.get("email")
        lang = (data.get("language") or "de").strip().lower()
        if lang not in ("de", "en"):
            lang = "de"
        return email, lang
    except Exception as exc:
        print(f"_get_company_owner_email_and_language: could not look up owner for company {company_id}: {exc}")
        return None, "de"


def _send_low_rating_notification(
    owner_email: str,
    company_name: str,
    client_id: str,
    satisfaction: int,
    callback_requested: Optional[bool],
    callback_note: Optional[str],
    is_update: bool = False,
    language: str = "de",
) -> bool:
    """
    Sends a SendGrid e-mail to the company owner for low-rating feedback.
    Includes callback status and note when available.
    `language`: 'de' or 'en' (from company owner's users.language).
    Non-critical: returns False on failure instead of raising.
    """
    try:
        from sendgrid import SendGridAPIClient

        api_key = db.secrets.get("SENDGRID_API_KEY")
        if not api_key:
            print("_send_low_rating_notification: SENDGRID_API_KEY not configured.")
            return False

        lang = (language or "de").strip().lower()
        if lang not in ("de", "en"):
            lang = "de"

        if lang == "de":
            callback_label = (
                "Ja"
                if callback_requested is True
                else "Nein"
                if callback_requested is False
                else "Noch keine Angabe"
            )
            note_empty = "<p style='color:#94a3b8'><em>(keine zusätzliche Nachricht)</em></p>"
        else:
            callback_label = (
                "Requested"
                if callback_requested is True
                else "Not requested"
                if callback_requested is False
                else "Not answered yet"
            )
            note_empty = "<p style='color:#94a3b8'><em>(no additional note)</em></p>"

        note_html = (
            f"<blockquote style='border-left:4px solid #3b82f6;padding:8px 16px;color:#334155;background:#f8fafc'>"
            f"{callback_note.replace(chr(10), '<br>')}"
            f"</blockquote>"
            if callback_note
            else note_empty
        )

        ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

        if lang == "de":
            html_body = f"""
        <html><body style="font-family:Inter,sans-serif;color:#0f172a">
          <h2 style="color:#2563eb">🚨 Hinweis: niedrige Bewertung</h2>
          <p>Ein Kunde hat eine Bewertung mit <strong>{satisfaction} von 5 Sternen</strong> über
             <strong>Happy Client Flow</strong> abgegeben.</p>
          <table style="border-collapse:collapse;width:100%;max-width:500px">
            <tr><td style="padding:6px 12px;font-weight:600;width:140px">Unternehmen</td>
                <td style="padding:6px 12px">{company_name}</td></tr>
            <tr style="background:#f8fafc">
                <td style="padding:6px 12px;font-weight:600">Kunden-ID</td>
                <td style="padding:6px 12px;font-family:monospace">{client_id}</td></tr>
            <tr><td style="padding:6px 12px;font-weight:600">Bewertung</td>
                <td style="padding:6px 12px">{satisfaction} / 5</td></tr>
            <tr style="background:#f8fafc">
                <td style="padding:6px 12px;font-weight:600">Rückruf gewünscht</td>
                <td style="padding:6px 12px">{callback_label}</td></tr>
            <tr><td style="padding:6px 12px;font-weight:600">Eingegangen</td>
                <td style="padding:6px 12px">{ts}</td></tr>
          </table>
          <h3 style="margin-top:20px">Nachricht des Kunden:</h3>
          {note_html}
          <hr style="border:none;border-top:1px solid #e2e8f0;margin:24px 0">
          <p style="color:#64748b;font-size:12px">
            Diese Benachrichtigung wurde automatisch von Happy Client Flow gesendet.
          </p>
        </body></html>
        """
            subject = f"[HCF] Niedrige Bewertung ({satisfaction}/5) — {company_name}"
        else:
            html_body = f"""
        <html><body style="font-family:Inter,sans-serif;color:#0f172a">
          <h2 style="color:#2563eb">🚨 Low-rating feedback alert</h2>
          <p>A client submitted a <strong>{satisfaction}-star</strong> feedback on
             <strong>Happy Client Flow</strong>.</p>
          <table style="border-collapse:collapse;width:100%;max-width:500px">
            <tr><td style="padding:6px 12px;font-weight:600;width:140px">Company</td>
                <td style="padding:6px 12px">{company_name}</td></tr>
            <tr style="background:#f8fafc">
                <td style="padding:6px 12px;font-weight:600">Client ID</td>
                <td style="padding:6px 12px;font-family:monospace">{client_id}</td></tr>
            <tr><td style="padding:6px 12px;font-weight:600">Rating</td>
                <td style="padding:6px 12px">{satisfaction} / 5</td></tr>
            <tr style="background:#f8fafc">
                <td style="padding:6px 12px;font-weight:600">Callback requested</td>
                <td style="padding:6px 12px">{callback_label}</td></tr>
            <tr><td style="padding:6px 12px;font-weight:600">Submitted</td>
                <td style="padding:6px 12px">{ts}</td></tr>
          </table>
          <h3 style="margin-top:20px">Client note:</h3>
          {note_html}
          <hr style="border:none;border-top:1px solid #e2e8f0;margin:24px 0">
          <p style="color:#64748b;font-size:12px">
            This notification was sent automatically by Happy Client Flow.
          </p>
        </body></html>
        """
            subject = f"[HCF] Low-rating alert ({satisfaction}/5) — {company_name}"

        if lang == "de":
            plain_text_content = (
                f"Hinweis: niedrige Bewertung\n\n"
                f"Ein Kunde hat {satisfaction} von 5 Sternen abgegeben.\n"
                f"Unternehmen: {company_name}\n"
                f"Kunden-ID: {client_id}\n"
                f"Rückruf gewünscht: {callback_label}\n"
                f"Eingegangen: {ts}\n\n"
                f"Nachricht des Kunden:\n{(callback_note or '(keine zusätzliche Nachricht)')}"
            )
        else:
            plain_text_content = (
                f"Low-rating feedback alert\n\n"
                f"A client submitted {satisfaction} out of 5 stars.\n"
                f"Company: {company_name}\n"
                f"Client ID: {client_id}\n"
                f"Callback requested: {callback_label}\n"
                f"Submitted: {ts}\n\n"
                f"Client note:\n{(callback_note or '(no additional note)')}"
            )

        message = build_sendgrid_mail(
            from_email="noreply@happyclientflow.de",
            from_name="Happy Client Flow",
            to_emails=owner_email,
            subject=subject,
            html_content=html_body,
            plain_text_content=plain_text_content,
        )
        sg = SendGridAPIClient(api_key)
        response = sg.send(message)
        print(
            f"_send_low_rating_notification: sent to {owner_email}, "
            f"status={response.status_code}"
        )
        return response.status_code in (200, 202)
    except Exception as exc:
        print(f"_send_low_rating_notification: failed → {exc}")
        return False


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/submit-internal-feedback", response_model=SubmitInternalFeedbackResponse)
def submit_internal_feedback(
    body: SubmitInternalFeedbackRequest,
    supabase: Client = Depends(get_supabase_service_client),
):
    """
    Called by the <=4-star internal follow-up screen after the main feedback was
    already submitted via /create-feedback.

    - Persists callback_requested and callback_note on the feedback row.
    - Sends an updated SendGrid notification e-mail to the company owner with
      callback-request status and note.

    All errors are non-fatal: the feedback row already exists; this is bonus data.
    """
    client_id = (body.client_id or "").strip()
    feedback_id = (body.feedback_id or "").strip()

    if not client_id or not feedback_id:
        raise HTTPException(status_code=400, detail="client_id and feedback_id are required.")

    # 1. Persist callback fields on the feedback row
    try:
        supabase.from_("feedback").update(
            {
                "callback_requested": bool(body.callback_requested),
                "callback_note": (body.callback_note or "").strip() or None,
            }
        ).eq("id", feedback_id).execute()
    except Exception as exc:
        print(f"submit_internal_feedback: could not update feedback row {feedback_id}: {exc}")
        # Non-fatal — continue to try notification

    # 2. Notify company owner with updated callback status
    try:
        client_res = supabase.from_("clients").select("company_id").eq("id", client_id).single().execute()
        company_id = (client_res.data or {}).get("company_id")

        if company_id:
            company_res = supabase.from_("companies").select("name").eq("id", company_id).single().execute()
            company_name = (company_res.data or {}).get("name", "Your company")
            owner_email, owner_language = _get_company_owner_email_and_language(supabase, company_id)
            feedback_res = supabase.from_("feedback").select("satisfaction").eq("id", feedback_id).single().execute()
            satisfaction = int((feedback_res.data or {}).get("satisfaction") or 0)

            if owner_email and satisfaction <= 4:
                _send_low_rating_notification(
                    owner_email=owner_email,
                    company_name=company_name,
                    client_id=client_id,
                    satisfaction=satisfaction,
                    callback_requested=bool(body.callback_requested),
                    callback_note=body.callback_note,
                    is_update=True,
                    language=owner_language,
                )
            elif not owner_email:
                print(f"submit_internal_feedback: no owner email found for company {company_id}")
        else:
            print(f"submit_internal_feedback: no company_id on client {client_id}")
    except Exception as exc:
        print(f"submit_internal_feedback: notification error: {exc}")

    return SubmitInternalFeedbackResponse(ok=True, message="Internal feedback recorded.")
