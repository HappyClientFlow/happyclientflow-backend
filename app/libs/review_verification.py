"""
Heuristic matching of scraped public reviews to a feedback session (draft text + name hint + time).

Triggered after mark-external-review-clicked (background) and optionally via retry cron.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

import databutton as db
from supabase import Client, create_client


def _service_supabase() -> Client:
    url = db.secrets.get("SUPABASE_URL")
    key = db.secrets.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise RuntimeError("Supabase configuration missing for review verification.")
    return create_client(url, key)


def normalize_text(s: Optional[str]) -> str:
    if not s:
        return ""
    t = s.lower().strip()
    t = re.sub(r"\s+", " ", t)
    return t


def text_similarity(a: str, b: str) -> float:
    na, nb = normalize_text(a), normalize_text(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def parse_int_rating(val: Any) -> Optional[int]:
    if val is None:
        return None
    if isinstance(val, int):
        return val
    s = str(val).replace(",", ".")
    m = re.search(r"(\d+)", s)
    return int(m.group(1)) if m else None


def parse_trustpilot_datetime(val: str) -> Optional[datetime]:
    if not val or not isinstance(val, str):
        return None
    s = val.strip().replace("Z", "+00:00")
    if "T" not in s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def review_published_dt(profile_type: str, raw: Dict[str, Any]) -> Optional[datetime]:
    if profile_type == "google":
        ts = raw.get("time")
        if isinstance(ts, (int, float)) and ts > 0:
            return datetime.fromtimestamp(int(ts), tz=timezone.utc)
        return None
    if profile_type == "trustpilot":
        return parse_trustpilot_datetime(raw.get("date") or "")
    return None


@dataclass
class NormalizedReview:
    author: str
    text: str
    rating: Optional[int]
    published: Optional[datetime]
    raw: Dict[str, Any]


def normalize_google_review(r: Dict[str, Any]) -> NormalizedReview:
    return NormalizedReview(
        author=(r.get("author_name") or "").strip(),
        text=(r.get("text") or "").strip(),
        rating=r.get("rating") if isinstance(r.get("rating"), int) else parse_int_rating(r.get("rating")),
        published=review_published_dt("google", r),
        raw=r,
    )


def normalize_provenexpert_review(r: Dict[str, Any]) -> NormalizedReview:
    return NormalizedReview(
        author=(r.get("authorName") or "").strip(),
        text=(r.get("body") or "").strip(),
        rating=parse_int_rating(r.get("ratingStars")),
        published=None,
        raw=r,
    )


def normalize_trustpilot_review(r: Dict[str, Any]) -> NormalizedReview:
    return NormalizedReview(
        author=(r.get("author") or "").strip(),
        text=(r.get("content") or r.get("title") or "").strip(),
        rating=r.get("rating") if isinstance(r.get("rating"), int) else parse_int_rating(r.get("rating")),
        published=review_published_dt("trustpilot", r),
        raw=r,
    )


def normalize_anwalt_review(r: Dict[str, Any]) -> NormalizedReview:
    return NormalizedReview(
        author=(r.get("authorName") or "").strip(),
        text=(r.get("body") or r.get("title") or "").strip(),
        rating=parse_int_rating(r.get("ratingStars")),
        published=None,
        raw=r,
    )


def normalize_scraped_review(profile_type: str, r: Dict[str, Any]) -> NormalizedReview:
    if profile_type == "google":
        return normalize_google_review(r)
    if profile_type == "proven_expert":
        return normalize_provenexpert_review(r)
    if profile_type == "trustpilot":
        return normalize_trustpilot_review(r)
    if profile_type in ("anwalt", "anwalt_de"):
        return normalize_anwalt_review(r)
    return NormalizedReview("", "", None, None, r)


def score_candidate(
    *,
    draft: str,
    name_hint: str,
    feedback_satisfaction: int,
    review: NormalizedReview,
    click_at: Optional[datetime],
) -> Tuple[float, str]:
    """Return (confidence 0..1, reason label)."""
    text_sim = text_similarity(draft, review.text) if draft else 0.0
    name_sim = (
        text_similarity(name_hint, review.author) if name_hint else 0.0
    )

    time_boost = 0.0
    time_note = ""
    if click_at and review.published:
        delta = (review.published - click_at).total_seconds()
        if -600 <= delta <= 48 * 3600:
            if abs(delta) <= 30 * 60:
                time_boost = 0.12
                time_note = "time_tight"
            else:
                time_boost = 0.06
                time_note = "time_window"

    rating_ok = True
    if review.rating is not None and feedback_satisfaction >= 4:
        rating_ok = review.rating >= 4

    if text_sim >= 0.78:
        conf = 0.88 + min(0.07, name_sim * 0.07) + time_boost
        return min(1.0, conf), "text_strong+" + (time_note or "na")

    if text_sim >= 0.55 and name_sim >= 0.55:
        return min(1.0, 0.72 + time_boost), "text_name+" + (time_note or "na")

    if name_sim >= 0.82 and text_sim >= 0.28:
        return min(1.0, 0.68 + time_boost), "name_strong+" + (time_note or "na")

    if text_sim >= 0.42 and time_boost >= 0.06 and rating_ok:
        return min(1.0, 0.62 + time_boost * 0.5), "text_time"

    if time_boost >= 0.12 and rating_ok and (text_sim >= 0.2 or not draft):
        base = 0.52 + name_sim * 0.1
        return min(1.0, base), "time_rating_fallback"

    base = max(text_sim, name_sim * 0.85) * 0.55 + time_boost
    if not draft:
        base = max(base, name_sim * 0.5 + time_boost)
    return min(1.0, base), "weak"


VERIFY_THRESHOLD = 0.62
WEAK_THRESHOLD = 0.48


async def _fetch_google_reviews(place_id: str, force_refresh: bool) -> List[Dict[str, Any]]:
    from app.apis.google_places import get_place_details

    details = await get_place_details(place_id, force_refresh=force_refresh)
    return [r.model_dump() for r in details.reviews]


async def _fetch_proven_expert(url: str, force_refresh: bool) -> List[Dict[str, Any]]:
    from app.apis.profile_provenexpert import ScrapeRequest, profile_provenexpert

    resp = await profile_provenexpert(
        ScrapeRequest(url=url, page=1, force_refresh=force_refresh)
    )
    if not resp.data:
        return []
    revs = resp.data[0].get("reviews") or []
    return list(revs)


async def _fetch_trustpilot(url: str, force_refresh: bool) -> List[Dict[str, Any]]:
    from app.apis.profile_trustpilot import ScrapeRequest, profile_trustpilot

    resp = await profile_trustpilot(
        ScrapeRequest(url=url, page=1, force_refresh=force_refresh)
    )
    if not resp.data:
        return []
    sp = resp.data[0]
    if hasattr(sp, "model_dump"):
        d = sp.model_dump()
    else:
        d = dict(sp)
    reviews = d.get("reviews") or []
    out = []
    for rv in reviews:
        if hasattr(rv, "model_dump"):
            out.append(rv.model_dump())
        elif isinstance(rv, dict):
            out.append(rv)
        else:
            out.append(dict(rv))
    return out


async def _fetch_anwalt(url: str, force_refresh: bool) -> List[Dict[str, Any]]:
    from app.apis.profile_anwalt import ScrapeRequest, profile_anwalt

    resp = await profile_anwalt(
        ScrapeRequest(url=url, page=1, force_refresh=force_refresh)
    )
    if not resp.data:
        return []
    revs = resp.data[0].get("reviews") or []
    return list(revs)


async def fetch_reviews_for_target(
    profile_type: str, place_id: Optional[str], profile_url: Optional[str], force_refresh: bool
) -> List[Dict[str, Any]]:
    if profile_type == "google":
        if not place_id:
            return []
        return await _fetch_google_reviews(place_id, force_refresh)
    if not profile_url:
        return []
    if profile_type == "proven_expert":
        return await _fetch_proven_expert(profile_url, force_refresh)
    if profile_type == "trustpilot":
        return await _fetch_trustpilot(profile_url, force_refresh)
    if profile_type in ("anwalt", "anwalt_de"):
        return await _fetch_anwalt(profile_url, force_refresh)
    return []


def _map_profile_type_for_scraper(raw: str) -> str:
    if raw == "anwalt_de":
        return "anwalt"
    return raw


def build_targets_for_company(
    supabase: Client,
    company_id: str,
    clicked_profile_type: Optional[str],
) -> List[Dict[str, Any]]:
    prof_res = (
        supabase.from_("profiles")
        .select("id, profile_type, link, google_place_id")
        .eq("company_id", company_id)
        .execute()
    )
    profiles = getattr(prof_res, "data", None) or []

    comp_res = (
        supabase.from_("companies")
        .select("google_product_id")
        .eq("id", company_id)
        .limit(1)
        .execute()
    )
    comp_rows = getattr(comp_res, "data", None) or []
    company_place = (
        (comp_rows[0].get("google_product_id") or "").strip() if comp_rows else ""
    )

    targets: List[Dict[str, Any]] = []
    seen: set[str] = set()

    for p in profiles:
        pt_raw = (p.get("profile_type") or "").strip()
        pt = _map_profile_type_for_scraper(pt_raw)
        pid = str(p.get("id") or "")

        if pt == "google":
            place = (p.get("google_place_id") or "").strip() or company_place
            if not place:
                continue
            key = f"google:{place}"
            if key in seen:
                continue
            seen.add(key)
            targets.append(
                {
                    "profile_type": "google",
                    "profile_id": pid or None,
                    "place_id": place,
                    "profile_url": None,
                    "target_key": key,
                }
            )
        elif pt == "proven_expert" and (p.get("link") or "").strip():
            url = p["link"].strip()
            key = f"proven_expert:{hashlib.md5(url.encode()).hexdigest()[:20]}"
            if key in seen:
                continue
            seen.add(key)
            targets.append(
                {
                    "profile_type": "proven_expert",
                    "profile_id": pid or None,
                    "place_id": None,
                    "profile_url": url,
                    "target_key": key,
                }
            )
        elif pt == "trustpilot" and (p.get("link") or "").strip():
            url = p["link"].strip()
            key = f"trustpilot:{hashlib.md5(url.encode()).hexdigest()[:20]}"
            if key in seen:
                continue
            seen.add(key)
            targets.append(
                {
                    "profile_type": "trustpilot",
                    "profile_id": pid or None,
                    "place_id": None,
                    "profile_url": url,
                    "target_key": key,
                }
            )
        elif pt == "anwalt" and (p.get("link") or "").strip():
            url = p["link"].strip()
            key = f"anwalt:{hashlib.md5(url.encode()).hexdigest()[:20]}"
            if key in seen:
                continue
            seen.add(key)
            targets.append(
                {
                    "profile_type": "anwalt",
                    "profile_id": pid or None,
                    "place_id": None,
                    "profile_url": url,
                    "target_key": key,
                }
            )

    if clicked_profile_type:
        want = _map_profile_type_for_scraper(clicked_profile_type.strip().lower())
        targets = [x for x in targets if x["profile_type"] == want]

    return targets


def _apply_google_published_if_needed(supabase: Client, client_id: str, confidence: float) -> None:
    if confidence < VERIFY_THRESHOLD:
        return
    try:
        from app.libs.google_review_reminder_scheduling import (
            cancel_pending_google_review_followup_reminders,
        )

        now = datetime.now(timezone.utc)
        supabase.from_("clients").update(
            {
                "google_review_published": True,
                "google_review_published_at": now.isoformat(),
                "review_status": "ReviewComplete",
            }
        ).eq("id", client_id).execute()
        cancel_pending_google_review_followup_reminders(supabase, client_id)
    except Exception as e:
        print(f"review_verification: could not apply google_review_published: {e}")


async def verify_targets_for_feedback(
    supabase: Client,
    *,
    client_id: str,
    feedback_id: str,
    company_id: str,
    draft: str,
    name_hint: str,
    satisfaction: int,
    click_at: Optional[datetime],
    clicked_profile_type: Optional[str],
    force_refresh: bool = True,
) -> None:
    targets = build_targets_for_company(supabase, company_id, clicked_profile_type)
    if not targets:
        print(f"review_verification: no targets for company_id={company_id!r}")
        return

    for t in targets:
        row_check = (
            supabase.from_("external_review_verifications")
            .select("id, status")
            .eq("feedback_id", feedback_id)
            .eq("target_key", t["target_key"])
            .limit(1)
            .execute()
        )
        existing = (getattr(row_check, "data", None) or None) or []
        if existing and existing[0].get("status") == "verified":
            continue

        now_iso = datetime.now(timezone.utc).isoformat()
        pid = t.get("profile_id")
        upsert_payload = {
            "client_id": client_id,
            "feedback_id": feedback_id,
            "profile_type": t["profile_type"],
            "profile_id": pid if pid else None,
            "target_key": t["target_key"],
            "place_id": t.get("place_id"),
            "profile_url": t.get("profile_url"),
            "updated_at": now_iso,
        }
        supabase.from_("external_review_verifications").upsert(
            upsert_payload, on_conflict="feedback_id,target_key"
        ).execute()

        att_res = (
            supabase.from_("external_review_verifications")
            .select("verification_attempts")
            .eq("feedback_id", feedback_id)
            .eq("target_key", t["target_key"])
            .single()
            .execute()
        )
        prev_attempts = 0
        if att_res.data and isinstance(att_res.data.get("verification_attempts"), int):
            prev_attempts = att_res.data["verification_attempts"]

        try:
            raw_reviews = await fetch_reviews_for_target(
                t["profile_type"],
                t.get("place_id"),
                t.get("profile_url"),
                force_refresh,
            )
        except Exception as e:
            print(f"review_verification: scrape failed target={t!r}: {e}")
            supabase.from_("external_review_verifications").update(
                {
                    "status": "error",
                    "last_checked_at": now_iso,
                    "verification_attempts": prev_attempts + 1,
                    "updated_at": now_iso,
                }
            ).eq("feedback_id", feedback_id).eq("target_key", t["target_key"]).execute()
            continue

        best_conf = 0.0
        best_reason = ""
        best: Optional[NormalizedReview] = None

        for raw in raw_reviews:
            if not isinstance(raw, dict):
                continue
            nr = normalize_scraped_review(t["profile_type"], raw)
            conf, reason = score_candidate(
                draft=draft,
                name_hint=name_hint,
                feedback_satisfaction=satisfaction,
                review=nr,
                click_at=click_at,
            )
            if conf > best_conf:
                best_conf = conf
                best_reason = reason
                best = nr

        status = "pending"
        if best_conf >= VERIFY_THRESHOLD:
            status = "verified"
        elif best_conf >= WEAK_THRESHOLD:
            status = "inconclusive"
        else:
            status = "inconclusive"

        preview = (best.text[:500] if best else "") or None
        mauthor = best.author if best else None
        mtime = best.published.isoformat() if best and best.published else None

        supabase.from_("external_review_verifications").update(
            {
                "status": status,
                "confidence": round(best_conf, 4),
                "match_reason": best_reason,
                "matched_author": mauthor,
                "matched_text_preview": preview,
                "matched_review_time": mtime,
                "last_checked_at": now_iso,
                "verification_attempts": prev_attempts + 1,
                "updated_at": now_iso,
            }
        ).eq("feedback_id", feedback_id).eq("target_key", t["target_key"]).execute()

        if t["profile_type"] == "google" and status == "verified":
            _apply_google_published_if_needed(supabase, client_id, best_conf)


async def run_external_review_verification(
    client_id: str, clicked_profile_type: Optional[str] = None
) -> None:
    supabase = _service_supabase()
    cres = (
        supabase.from_("clients")
        .select("id, company_id")
        .eq("id", client_id)
        .single()
        .execute()
    )
    if not cres.data:
        print(f"review_verification: client not found {client_id!r}")
        return
    company_id = cres.data.get("company_id")
    if not company_id:
        return

    fres = (
        supabase.from_("feedback")
        .select("id, satisfaction, review_draft_text, reviewer_name_hint")
        .eq("client_id", client_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = getattr(fres, "data", None) or []
    if not rows:
        print(f"review_verification: no feedback for client_id={client_id!r}")
        return
    fb = rows[0]
    feedback_id = fb["id"]
    draft = (fb.get("review_draft_text") or "").strip()
    name_hint = (fb.get("reviewer_name_hint") or "").strip()
    satisfaction = int(fb.get("satisfaction") or 0)

    click_res = (
        supabase.from_("clients")
        .select("external_review_clicked_at")
        .eq("id", client_id)
        .single()
        .execute()
    )
    click_at = None
    raw_click = click_res.data.get("external_review_clicked_at") if click_res.data else None
    if raw_click:
        try:
            click_at = datetime.fromisoformat(str(raw_click).replace("Z", "+00:00"))
        except ValueError:
            click_at = None

    await verify_targets_for_feedback(
        supabase,
        client_id=client_id,
        feedback_id=feedback_id,
        company_id=company_id,
        draft=draft,
        name_hint=name_hint,
        satisfaction=satisfaction,
        click_at=click_at,
        clicked_profile_type=clicked_profile_type,
        force_refresh=True,
    )


def run_external_review_verification_sync(
    client_id: str, clicked_profile_type: Optional[str] = None
) -> None:
    asyncio.run(run_external_review_verification(client_id, clicked_profile_type))


def _pending_retry_client_ids(
    limit: int = 40, company_id: Optional[str] = None
) -> List[str]:
    """
    Distinct client ids a retry run would actually re-verify, in order.

    Split out of process_pending_verification_retries so the dashboard can say
    up front how much work it just started — previously the endpoint reported
    "started" whether or not there was anything to do.
    """
    supabase = _service_supabase()
    res = (
        supabase.from_("external_review_verifications")
        .select("client_id")
        .in_("status", ["pending", "inconclusive", "error"])
        .order("updated_at", desc=True)
        .limit(limit)
        .execute()
    )
    rows = getattr(res, "data", None) or []

    allowed_clients: Optional[set[str]] = None
    if company_id:
        cres = (
            supabase.from_("clients")
            .select("id")
            .eq("company_id", company_id)
            .execute()
        )
        crows = getattr(cres, "data", None) or []
        allowed_clients = {str(c.get("id")) for c in crows if c.get("id")}

    seen: set[str] = set()
    out: List[str] = []
    for r in rows:
        cid = r.get("client_id")
        if not cid or cid in seen:
            continue
        if allowed_clients is not None and cid not in allowed_clients:
            continue
        seen.add(cid)
        out.append(cid)
    return out


def count_pending_verification_retries(
    limit: int = 40, company_id: Optional[str] = None
) -> int:
    """How many clients a retry run would re-verify right now."""
    return len(_pending_retry_client_ids(limit=limit, company_id=company_id))


def process_pending_verification_retries(
    limit: int = 40, company_id: Optional[str] = None
) -> int:
    """
    Re-run verification for recent pending/inconclusive rows (cron).
    """
    rows = [{"client_id": cid} for cid in _pending_retry_client_ids(limit, company_id)]
    allowed_clients: Optional[set[str]] = None
    seen_client: set[str] = set()
    n = 0
    for r in rows:
        cid = r.get("client_id")
        if not cid or cid in seen_client:
            continue
        if allowed_clients is not None and cid not in allowed_clients:
            continue
        seen_client.add(cid)
        try:
            run_external_review_verification_sync(cid, None)
            n += 1
        except Exception as e:
            print(f"review_verification retry failed client={cid!r}: {e}")
    return n
