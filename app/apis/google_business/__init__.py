"""
Google Business Profile OAuth (refresh token) and posting replies to reviews via My Business API v4.

Requires Google Cloud OAuth client (web) with redirect URI pointing to this API's callback.
Enable APIs: My Business Account Management, My Business Business Information, My Business API (v4).
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.parse
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Optional

import databutton as db
import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from supabase import Client, create_client

router = APIRouter(prefix="/google-business", tags=["google_business"])

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
ACCOUNT_MGMT = "https://mybusinessaccountmanagement.googleapis.com/v1"
BUSINESS_INFO = "https://mybusinessbusinessinformation.googleapis.com/v1"
MYBUSINESS_V4 = "https://mybusiness.googleapis.com/v4"

# business.manage covers listing accounts/locations and posting replies (v4)
SCOPES = "https://www.googleapis.com/auth/business.manage"


def _read_secret(*keys: str) -> Optional[str]:
    """
    Read config: os.environ first (local .env via dotenv in main.py), then Databutton vault.
    db.secrets.get raises KeyError when a name is not registered in Databutton.
    """
    for key in keys:
        env_val = os.environ.get(key)
        if env_val and str(env_val).strip():
            return str(env_val).strip()
        try:
            val = db.secrets.get(key)
            if val and str(val).strip():
                return str(val).strip()
        except KeyError:
            pass
        except Exception:
            pass
    return None


def get_supabase() -> Client:
    url = _read_secret("SUPABASE_URL")
    key = _read_secret("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise HTTPException(status_code=500, detail="Supabase not configured")
    return create_client(url, key)


def _google_oauth_config() -> tuple[str, str, str]:
    client_id = _read_secret("GOOGLE_BUSINESS_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_ID")
    client_secret = _read_secret("GOOGLE_BUSINESS_CLIENT_SECRET", "GOOGLE_OAUTH_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise HTTPException(
            status_code=500,
            detail="Google OAuth not configured (GOOGLE_BUSINESS_CLIENT_ID / GOOGLE_BUSINESS_CLIENT_SECRET).",
        )
    backend_base = (
        _read_secret("BACKEND_PUBLIC_URL", "API_PUBLIC_URL")
        or "http://localhost:8000"
    )
    redirect_uri = f"{backend_base.rstrip('/')}/routes/google-business/oauth/callback"
    return client_id, client_secret, redirect_uri


def _refresh_access_token(refresh_token: str) -> dict[str, Any]:
    client_id, client_secret, _ = _google_oauth_config()
    r = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if not r.ok:
        raise HTTPException(status_code=502, detail=f"Google token refresh failed: {r.text}")
    return r.json()


def _exchange_code_for_tokens(code: str) -> dict[str, Any]:
    client_id, client_secret, redirect_uri = _google_oauth_config()
    r = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if not r.ok:
        raise HTTPException(status_code=502, detail=f"Google token exchange failed: {r.text}")
    return r.json()


def _star_enum_to_int(star: Optional[str]) -> int:
    if not star:
        return 0
    m = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}
    return m.get(star.upper(), 0)


def _parse_rfc3339(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None


# --- Review matching -------------------------------------------------------
# Posting a reply is a PUBLIC action, so matching must be robust (tolerate Google
# translation wrappers, minor edits, and short/abbreviated name forms) without ever
# replying to the wrong review. We score each candidate with fuzzy text + token-aware
# author + rating + time signals, then require a confident, unambiguous best match.
MATCH_THRESHOLD = 0.66
STRONG_TEXT_CONF = 0.90


def _norm_text(s: str) -> str:
    """Lowercase + collapse whitespace, and drop Google's translation wrapper."""
    t = s or ""
    low = t.lower()
    # Google returns "(Translated by Google) <x> (Original) <y>" — keep the original half.
    if "(original)" in low:
        t = t[low.rindex("(original)") + len("(original)"):]
    for marker in ("(translated by google)", "(übersetzt von google)"):
        i = t.lower().find(marker)
        if i != -1:
            t = t[:i] + t[i + len(marker):]
    return re.sub(r"\s+", " ", t.lower()).strip()


def _norm_name(s: str) -> str:
    """Normalize a display name to space-separated alphanumeric tokens (hyphens → spaces)."""
    t = re.sub(r"[^a-z0-9äöüß]+", " ", (s or "").lower())
    return re.sub(r"\s+", " ", t).strip()


def _text_sim(a: str, b: str) -> float:
    na, nb = _norm_text(a), _norm_text(b)
    if not na or not nb:
        return 0.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    # Containment covers truncation/edits where one text is a subset of the other.
    short, lng = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(short) >= 20 and short in lng:
        ratio = max(ratio, 0.9)
    return ratio


def _author_sim(a: str, b: str) -> float:
    na, nb = _norm_name(a), _norm_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta, tb = set(na.split()), set(nb.split())
    shared = ta & tb
    if shared:
        # A shared surname-length token (e.g. "haak" in "Bine Haak" vs "Sabine Thomas-Haak")
        # is a decent corroborating signal; scale by token overlap.
        strong = any(len(tok) >= 4 for tok in shared)
        overlap = len(shared) / min(len(ta), len(tb))
        return max(0.6 if strong else 0.4, overlap)
    return SequenceMatcher(None, na, nb).ratio()


def _match_confidence(
    rating_match: bool, text: float, author: float, time_close: bool
) -> tuple[float, str]:
    """Confidence 0..1 that a candidate is the same review, plus a reason label."""
    # 1) Body text is the most unique signal for these (often long) reviews.
    if text >= 0.72:
        c = 0.90
        c += 0.05 if rating_match else 0.0
        c += 0.03 if author >= 0.5 else 0.0
        c += 0.02 if time_close else 0.0
        return min(1.0, c), "text_strong"
    # 2) Partial body match (edited / translated) backed by rating + author/time.
    if text >= 0.45 and rating_match and (author >= 0.5 or time_close):
        c = 0.70 + (0.05 if author >= 0.5 else 0.0) + (0.03 if time_close else 0.0)
        return min(1.0, c), "text_partial"
    # 3) Rating-only review on Google (no usable body): need a strong author + rating + time.
    if text < 0.25 and rating_match and author >= 0.75 and time_close:
        return 0.67, "author_rating_time"
    # 4) Not confident.
    base = text * 0.6 + author * 0.2 + (0.08 if rating_match else 0.0) + (0.05 if time_close else 0.0)
    return round(base, 4), "weak"


def _find_matching_review(
    reviews_payload: dict,
    rating: int,
    author_name: str,
    review_text: str,
    target_ts: Optional[float],
) -> tuple[Optional[str], float, str]:
    """
    Return (review_resource_name | None, best_confidence, reason).

    None is returned when no candidate clears MATCH_THRESHOLD, or when the best two
    candidates are too close to tell apart (ambiguity guard) — better to fail than to
    post a public reply on the wrong review.
    """
    items = reviews_payload.get("reviews") or []

    best_name: Optional[str] = None
    best_conf = 0.0
    best_reason = "no_reviews"
    second_conf = 0.0

    for rev in items:
        name = rev.get("name") or ""
        if not name:
            continue
        st = _star_enum_to_int(rev.get("starRating"))
        reviewer = (rev.get("reviewer") or {}).get("displayName") or ""
        comment = (rev.get("comment") or "").strip()
        ct = _parse_rfc3339(rev.get("createTime"))
        ct_ts = ct.timestamp() if ct else None

        rating_match = rating > 0 and st == rating
        text = _text_sim(review_text, comment)
        author = _author_sim(author_name, reviewer)
        time_close = (
            target_ts is not None
            and ct_ts is not None
            and abs(ct_ts - target_ts) <= 3 * 86400
        )

        conf, reason = _match_confidence(rating_match, text, author, time_close)

        if conf > best_conf:
            second_conf = best_conf
            best_conf = conf
            best_name = name
            best_reason = reason
        elif conf > second_conf:
            second_conf = conf

    # Ambiguity guard: unless the body text itself is a strong match (near-unique),
    # require the winner to be clearly ahead of the runner-up before posting.
    ambiguous = (
        best_conf < STRONG_TEXT_CONF
        and second_conf > 0.0
        and (best_conf - second_conf) < 0.08
    )
    if best_name and best_conf >= MATCH_THRESHOLD and not ambiguous:
        return best_name, best_conf, best_reason
    if ambiguous:
        return None, best_conf, best_reason + "+ambiguous"
    return None, best_conf, best_reason


class CreateGoogleOAuthLinkRequest(BaseModel):
    company_id: str
    return_url: str = Field(..., description="Frontend URL to redirect after success/failure")


class CreateGoogleOAuthLinkResponse(BaseModel):
    oauth_url: str


@router.post("/oauth/create-link", response_model=CreateGoogleOAuthLinkResponse)
def create_google_oauth_link(body: CreateGoogleOAuthLinkRequest):
    supabase = get_supabase()
    c = supabase.table("companies").select("id").eq("id", body.company_id).single().execute()
    if c is None or not getattr(c, "data", None):
        raise HTTPException(status_code=404, detail="Company not found")

    client_id, _, redirect_uri = _google_oauth_config()
    state_raw = f"{body.company_id}:{base64.urlsafe_b64encode(body.return_url.encode()).decode()}"
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state_raw,
    }
    url = GOOGLE_AUTH_URL + "?" + urllib.parse.urlencode(params)
    return CreateGoogleOAuthLinkResponse(oauth_url=url)


@router.get("/oauth/callback")
async def google_oauth_callback(request: Request):
    params = dict(request.query_params)
    code = params.get("code")
    err = params.get("error")
    state = params.get("state") or ""

    def _fail_redirect(msg: str) -> RedirectResponse:
        if ":" in state:
            try:
                _cid, b64u = state.split(":", 1)
                ru = base64.urlsafe_b64decode(b64u.encode()).decode()
                sep = "&" if "?" in ru else "?"
                return RedirectResponse(url=f"{ru}{sep}google_business=error&reason={urllib.parse.quote(msg)}")
            except Exception:
                pass
        raise HTTPException(status_code=400, detail=msg)

    if err:
        desc = params.get("error_description") or err
        return _fail_redirect(desc)

    if not code or ":" not in state:
        raise HTTPException(status_code=400, detail="Missing code or state")

    company_id, b64_url = state.split(":", 1)
    try:
        return_url = base64.urlsafe_b64decode(b64_url.encode()).decode()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid state")

    try:
        tokens = _exchange_code_for_tokens(code)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    refresh = tokens.get("refresh_token")
    if not refresh:
        sep = "&" if "?" in return_url else "?"
        fail = f"{return_url}{sep}google_business=missing_refresh"
        return RedirectResponse(url=fail)

    supabase = get_supabase()
    supabase.table("company_google_business_oauth").upsert(
        {
            "company_id": company_id,
            "refresh_token": refresh,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    ).execute()

    sep = "&" if "?" in return_url else "?"
    ok = f"{return_url}{sep}google_business=connected"
    return RedirectResponse(url=ok)


class GoogleBusinessStatusResponse(BaseModel):
    connected: bool


@router.get("/status/{company_id}", response_model=GoogleBusinessStatusResponse)
def google_business_status(company_id: str):
    supabase = get_supabase()
    resp = (
        supabase.table("company_google_business_oauth")
        .select("company_id")
        .eq("company_id", company_id)
        .maybe_single()
        .execute()
    )
    if resp is None:
        return GoogleBusinessStatusResponse(connected=False)
    data = getattr(resp, "data", None)
    return GoogleBusinessStatusResponse(connected=bool(data))


class DisconnectRequest(BaseModel):
    company_id: str


@router.post("/disconnect")
def disconnect_google_business(body: DisconnectRequest):
    supabase = get_supabase()
    supabase.table("company_google_business_oauth").delete().eq("company_id", body.company_id).execute()
    return {"ok": True}


class PostGoogleReviewReplyRequest(BaseModel):
    company_id: str
    profile_id: str
    rating: int = Field(ge=1, le=5)
    author_name: str = ""
    review_text: str = ""
    """Customer review body as shown in HCF (for matching)."""
    review_unix_ts: Optional[int] = None
    """Seconds since epoch for the review (Google Places `time`)."""
    reply_text: str = Field(..., min_length=1)


def _list_accounts(access_token: str) -> list[dict]:
    r = requests.get(
        f"{ACCOUNT_MGMT}/accounts",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=60,
    )
    if not r.ok:
        raise HTTPException(status_code=502, detail=f"List accounts failed: {r.text}")
    return r.json().get("accounts") or []


def _list_locations(access_token: str, account_name: str) -> list[dict]:
    """account_name like accounts/123456789"""
    url = f"{BUSINESS_INFO}/{account_name}/locations"
    r = requests.get(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        params={"readMask": "name,title,metadata,storefrontAddress"},
        timeout=60,
    )
    if not r.ok:
        raise HTTPException(status_code=502, detail=f"List locations failed: {r.text}")
    return r.json().get("locations") or []


def _list_reviews_v4(access_token: str, parent_accounts_locations: str) -> dict:
    """parent_accounts_locations: accounts/{aid}/locations/{lid} — paginated."""
    url = f"{MYBUSINESS_V4}/{parent_accounts_locations}/reviews"
    all_reviews: list = []
    page_token: Optional[str] = None
    for _ in range(20):
        params: dict[str, Any] = {"pageSize": 100}
        if page_token:
            params["pageToken"] = page_token
        r = requests.get(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
            timeout=60,
        )
        if not r.ok:
            return {"reviews": [], "_error": r.text}
        data = r.json()
        all_reviews.extend(data.get("reviews") or [])
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return {"reviews": all_reviews}


def _put_reply_v4(access_token: str, review_name: str, comment: str) -> None:
    url = f"{MYBUSINESS_V4}/{review_name}/reply"
    r = requests.put(
        url,
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={"comment": comment},
        timeout=60,
    )
    if not r.ok:
        raise HTTPException(status_code=502, detail=f"Post reply failed: {r.text}")


@router.post("/post-reply")
def post_google_review_reply(body: PostGoogleReviewReplyRequest):
    """
    Match the review in Google Business Profile and post the reply.
    Matching uses rating, author, text prefix, and optional unix timestamp.
    """
    if len(body.reply_text) > 4000:
        raise HTTPException(status_code=400, detail="Reply too long.")

    supabase = get_supabase()
    oauth = (
        supabase.table("company_google_business_oauth")
        .select("refresh_token")
        .eq("company_id", body.company_id)
        .maybe_single()
        .execute()
    )
    oauth_data = getattr(oauth, "data", None) if oauth is not None else None
    if not oauth_data:
        raise HTTPException(status_code=400, detail="Google Business not connected for this company.")

    prof = (
        supabase.table("profiles")
        .select("id, company_id, profile_type, google_place_id")
        .eq("id", body.profile_id)
        .single()
        .execute()
    )
    prof_data = getattr(prof, "data", None) if prof is not None else None
    if not prof_data or prof_data.get("company_id") != body.company_id:
        raise HTTPException(status_code=404, detail="Profile not found for company.")
    if prof_data.get("profile_type") != "google":
        raise HTTPException(status_code=400, detail="In-app reply is only supported for Google profiles in v2.")

    place_hint = (prof_data.get("google_place_id") or "").strip()

    tok = _refresh_access_token(oauth_data["refresh_token"])
    access = tok["access_token"]

    accounts = _list_accounts(access)
    if not accounts:
        raise HTTPException(status_code=400, detail="No Google Business accounts returned for this login.")

    target_ts = float(body.review_unix_ts) if body.review_unix_ts else None

    # Diagnostics so a failure tells us WHY (no API access vs. no reviews vs. weak score).
    best_conf = 0.0
    best_reason = "no_reviews"
    locations_scanned = 0
    total_reviews_seen = 0
    last_v4_error: Optional[str] = None

    for acc in accounts:
        acc_name = acc.get("name")
        if not acc_name:
            continue
        locations = _list_locations(access, acc_name)
        if not locations:
            continue

        # Prefer a single-location account; else try place id substring match in JSON blob
        loc_candidates = locations
        if place_hint and len(locations) > 1:
            filtered = []
            for loc in locations:
                blob = json.dumps(loc).lower()
                if place_hint.lower() in blob:
                    filtered.append(loc)
            if filtered:
                loc_candidates = filtered

        for loc in loc_candidates:
            loc_name = loc.get("name")
            if not loc_name:
                continue
            payload = _list_reviews_v4(access, loc_name)
            if payload.get("_error"):
                # Don't swallow silently — remember it so we can report an access issue.
                last_v4_error = payload["_error"]
                print(
                    f"[google_business] reviews read error loc={loc_name!r}: "
                    f"{str(payload['_error'])[:300]}"
                )
                continue
            locations_scanned += 1
            total_reviews_seen += len(payload.get("reviews") or [])
            match, conf, reason = _find_matching_review(
                payload,
                body.rating,
                body.author_name,
                body.review_text,
                target_ts,
            )
            if conf > best_conf:
                best_conf = conf
                best_reason = reason
            if match:
                _put_reply_v4(access, match, body.reply_text.strip())
                print(
                    f"[google_business] post-reply matched company={body.company_id!r} "
                    f"profile={body.profile_id!r} conf={conf:.2f} reason={reason!r} "
                    f"review={match!r}"
                )
                return {
                    "ok": True,
                    "review_name": match,
                    "match_confidence": round(conf, 3),
                }

    # Nothing matched — log the shape of the failure before returning.
    print(
        f"[google_business] post-reply NO MATCH company={body.company_id!r} "
        f"profile={body.profile_id!r} accounts={len(accounts)} "
        f"locations_scanned={locations_scanned} reviews_seen={total_reviews_seen} "
        f"best_conf={best_conf:.2f} best_reason={best_reason!r} "
        f"v4_error={(last_v4_error or '')[:300]!r}"
    )

    # No reviews came back at all → this is an access/permission problem, not a match miss.
    if total_reviews_seen == 0:
        if last_v4_error:
            raise HTTPException(
                status_code=502,
                detail=(
                    "Google returned an error while reading reviews for this location. "
                    "This usually means the Google Business Profile (My Business v4) API "
                    "is not enabled/approved for the project, or the connected account "
                    "lacks review permission. Please reply manually for now."
                ),
            )
        raise HTTPException(
            status_code=404,
            detail=(
                "No reviews were returned for the connected Google Business location. "
                "Check that the connected Google account manages this exact location, "
                "or reply manually."
            ),
        )

    # Reviews were readable but none matched confidently → surface how close we got.
    raise HTTPException(
        status_code=404,
        detail=(
            "Could not confidently match this review in Google Business Profile "
            f"(closest match {best_conf:.0%}). Please verify the review details or reply manually."
        ),
    )


class PostPlatformReplyRequest(BaseModel):
    """v3+ placeholder: extend per platform when APIs exist."""
    source: str
    company_id: str


@router.post("/post-reply-platform")
def post_platform_reply_placeholder(body: PostPlatformReplyRequest):
    if body.source == "google":
        raise HTTPException(status_code=400, detail="Use /post-reply for Google.")
    raise HTTPException(
        status_code=501,
        detail=f"In-app posting for source '{body.source}' is not available yet.",
    )
