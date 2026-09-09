

"""
This API module records the user's acceptance of the legal documents
(AGB, Datenschutzerklärung, Auftragsverarbeitungsvertrag) shown as a single
checkbox on the sign-up screen.

It is used by the SignUp.tsx page. The user has no session yet at that point
(e-mail confirmation is still pending), so the frontend cannot write to
public.users itself under RLS. This endpoint performs the write server-side
with the Supabase service_role key, the same way secure_user_update does.

The timestamp is generated here rather than taken from the request, so the
recorded acceptance time cannot be spoofed by the client. The version string
does come from the frontend, because that is what actually determines which
document text the user was shown.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
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


# --- API Endpoint ---
@router.post("/record-legal-consent", response_model=LegalConsentResponse)
def record_legal_consent(
    request: LegalConsentRequest,
    supabase: Client = Depends(get_supabase_service_client),
):
    """
    Stores legal_accepted_at / legal_accepted_version on the user's public.users row.

    Write-once: the update is scoped to rows where legal_accepted_at is still null,
    so an existing acceptance can never be overwritten or back-dated by a repeat call.
    """
    user_id = str(request.user_id)

    try:
        result = (
            supabase.from_("users")
            .update(
                {
                    "legal_accepted_at": datetime.now(timezone.utc).isoformat(),
                    "legal_accepted_version": request.version,
                }
            )
            .eq("id", user_id)
            .is_("legal_accepted_at", "null")
            .execute()
        )
    except Exception as e:
        print(f"Error recording legal consent for user {user_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while recording the legal consent.",
        )

    recorded = bool(result.data)
    if not recorded:
        # Either the row is not there yet or consent was already stored. Both are
        # non-fatal for the caller, but the first case would lose the record, so log it.
        print(
            f"Legal consent not written for user {user_id}: "
            "row missing or acceptance already recorded."
        )

    return LegalConsentResponse(
        message=(
            "Legal consent recorded."
            if recorded
            else "Legal consent was already recorded for this user."
        ),
        user_id=request.user_id,
        recorded=recorded,
    )
