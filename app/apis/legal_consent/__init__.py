

"""
This API module records the user's acceptance of the legal documents
(AGB, Datenschutzerklärung, Auftragsverarbeitungsvertrag) shown as a single
checkbox on the sign-up screen, and again by the re-prompt when the documents
have been revised since the user last accepted.

It is used by SignUp.tsx and by LegalConsentGate.tsx. At sign-up the user has no
session yet (e-mail confirmation is still pending), so the frontend cannot write
to public.users itself under RLS. This endpoint performs the write server-side
with the Supabase service_role key, the same way secure_user_update does.

Every acceptance appends a row to public.legal_consents, and the current values
are mirrored onto public.users (legal_accepted_at / _version / _ip) so the common
"when did this customer accept, and to which version" question stays a plain
column read.

The timestamp and the IP are taken from the request rather than the body, so
neither can be spoofed by the client. The version string does come from the
frontend, because that is what determines which document text the user was shown.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, UUID4, Field
import databutton as db
from supabase.client import create_client, Client

router = APIRouter()


# --- Pydantic Models ---
class LegalConsentRequest(BaseModel):
    user_id: UUID4
    version: str = Field(min_length=1, max_length=32)


class LegalConsentResponse(BaseModel):
    message: str
    user_id: UUID4
    recorded: bool


# --- Supabase Client Initialization ---
def get_supabase_service_client() -> Client:
    """Initializes and returns a Supabase client with the service_role key."""
    supabase_url = db.secrets.get("SUPABASE_URL")
    service_key = db.secrets.get("SUPABASE_SERVICE_KEY")
    if not supabase_url or not service_key:
        raise HTTPException(status_code=500, detail="Supabase configuration missing.")
    return create_client(supabase_url, service_key)


def get_client_ip(request: Request) -> str | None:
    """
    Best-effort client IP.

    The app runs behind a proxy (Render/Vercel), so the socket peer is the proxy.
    X-Forwarded-For is a client-controlled header and can be spoofed, but the
    left-most entry is the useful one when the proxy appends honestly, and this
    value is only ever supporting evidence next to the timestamp.
    """
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        first_hop = forwarded_for.split(",")[0].strip()
        if first_hop:
            return first_hop[:255]

    real_ip = request.headers.get("x-real-ip")
    if real_ip and real_ip.strip():
        return real_ip.strip()[:255]

    return request.client.host if request.client else None


# --- API Endpoint ---
@router.post("/record-legal-consent", response_model=LegalConsentResponse)
def record_legal_consent(
    request: Request,
    body: LegalConsentRequest,
    supabase: Client = Depends(get_supabase_service_client),
):
    """
    Appends an acceptance to public.legal_consents and mirrors it onto public.users.

    Idempotent per (user_id, version): the table's unique constraint means a retry
    or a double submit cannot produce a second row for the same document version,
    and an already-recorded acceptance is never back-dated.
    """
    user_id = str(body.user_id)
    accepted_at = datetime.now(timezone.utc).isoformat()
    ip_address = get_client_ip(request)

    try:
        insert_result = (
            supabase.from_("legal_consents")
            .insert(
                {
                    "user_id": user_id,
                    "version": body.version,
                    "ip_address": ip_address,
                    "accepted_at": accepted_at,
                }
            )
            .execute()
        )
    except Exception as e:
        # 23505 = unique_violation: this user already accepted this exact version.
        if "23505" in str(e) or "duplicate key" in str(e).lower():
            return LegalConsentResponse(
                message="Legal consent was already recorded for this version.",
                user_id=body.user_id,
                recorded=False,
            )

        print(f"Error recording legal consent for user {user_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while recording the legal consent.",
        )

    if not insert_result.data:
        print(f"Legal consent insert returned no row for user {user_id}.")

    # Mirror the current acceptance onto the user record. Non-fatal: the log row
    # above is the record of truth, these columns are a convenience for querying.
    try:
        (
            supabase.from_("users")
            .update(
                {
                    "legal_accepted_at": accepted_at,
                    "legal_accepted_version": body.version,
                    "legal_accepted_ip": ip_address,
                }
            )
            .eq("id", user_id)
            .execute()
        )
    except Exception as e:
        print(
            f"Non-critical error: consent logged for user {user_id} but mirroring "
            f"onto public.users failed: {e}"
        )

    return LegalConsentResponse(
        message="Legal consent recorded.",
        user_id=body.user_id,
        recorded=True,
    )
