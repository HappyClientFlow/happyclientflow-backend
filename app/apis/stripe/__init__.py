

"""
Stripe Subscription API

This API handles Stripe subscription management for Happy Client Flow:
- Create checkout sessions for new subscriptions
- Generate customer portal sessions for subscription management
- Check subscription status via database JOINs
- Process Stripe webhooks for subscription updates

Used by: Frontend subscription components, Stripe webhooks
"""

import os
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, HTTPException, Request, Header, Depends
from pydantic import BaseModel
import stripe
import databutton as db
import os
import json
from supabase import create_client, Client
from app.libs.auth import require_auth
from app.libs.pricing_config import (
    resolve_plan_from_lookup_key,
    is_extra_seat_lookup_key,
    get_plan_lookup_key,
    get_extra_seat_lookup_key,
    PLANS,
    SELECTABLE_PLANS,
    PRIMARY_PLAN,
)
from app.env import Mode, mode
import asyncio

router = APIRouter(prefix="/stripe")

# ---------------------
# Debug helpers
# ---------------------
def _secret_debug_info(value: Optional[str]) -> str:
    """
    Return safe info about a secret without exposing it.
    """
    if not value:
        return "missing"
    v = str(value)
    prefix = "whsec_" if v.startswith("whsec_") else ("sk_live_" if v.startswith("sk_live_") else ("sk_test_" if v.startswith("sk_test_") else "set"))
    return f"{prefix} (len={len(v)})"

# ---------------------
# Tax enforcement
# ---------------------
def _enforce_automatic_tax(subscription_id: str, customer_id: str) -> None:
    """
    Guarantee VAT is always applied to a subscription.

    Seat and plan changes RE-RATE the subscription (new line items / proration),
    and direct SubscriptionItem/Subscription mutations do not inherit the
    Checkout Session's automatic_tax setting. Without re-asserting it here, added
    seats or a changed plan could be billed VAT-free. This is the root cause of
    the "tax not applied" issues reported previously, so we enforce it on every
    server-side subscription mutation.

    We also require a billing address (country) on the customer, since Stripe can
    only compute tax with a valid jurisdiction — otherwise VAT would silently be 0.
    """
    try:
        customer = stripe.Customer.retrieve(customer_id)
    except stripe.StripeError as e:
        raise HTTPException(status_code=400, detail=f"Could not load Stripe customer for tax check: {e}") from e

    address = (customer.get("address") or {}) if isinstance(customer, dict) else (getattr(customer, "address", None) or {})
    country = address.get("country") if isinstance(address, dict) else getattr(address, "country", None)
    if not country:
        raise HTTPException(
            status_code=400,
            detail=(
                "A billing address (with country) is required so VAT can be calculated. "
                "Please update the billing details before changing the subscription."
            ),
        )

    # Idempotent: re-assert automatic tax on the subscription itself.
    stripe.Subscription.modify(subscription_id, automatic_tax={"enabled": True})

# Environment-based Stripe configuration
if mode == Mode.PROD:
    # Production: Use live Stripe keys
    stripe.api_key = db.secrets.get("STRIPE_SECRET_KEY_LIVE")
    STRIPE_WEBHOOK_SECRET = db.secrets.get("STRIPE_WEBHOOK_SECRET_LIVE")
else:
    # Development: Use test/sandbox Stripe keys (env var takes priority)
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY_TEST") or db.secrets.get("STRIPE_SECRET_KEY_TEST")
    STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET_TEST") or db.secrets.get("STRIPE_WEBHOOK_SECRET_TEST")

# Payment methods offered at checkout.
#
# Until now payment_method_types was never set, so Stripe decided per session from
# the dashboard's payment method configuration. That is why card could silently be
# absent for one amount and present for another (HCL-12) — nothing in this codebase
# varies by amount or interval. Listing the methods explicitly takes that decision
# away from Stripe's eligibility engine, which is what "cards must always be
# available" requires.
#
# Trade-off: an explicit list opts out of dynamic payment methods, so new methods
# have to be added here rather than appearing on their own. The list is therefore
# overridable without a deploy, via the STRIPE_PAYMENT_METHOD_TYPES env var or
# secret, as a comma-separated list (e.g. "card,paypal,sepa_debit").
#
# Every method named must be activated on the Stripe account and must support
# recurring payments, or session creation fails outright. create_checkout_session
# therefore drops whichever method Stripe complains about and retries, so one
# misconfigured method cannot cost us the card guarantee that this ticket is about.
DEFAULT_CHECKOUT_PAYMENT_METHOD_TYPES = ["card", "paypal", "klarna", "sepa_debit"]


def _resolve_billing_email(company: Dict[str, Any]) -> Optional[str]:
    """
    Billing e-mail for a company, taken from its owner in public.users.

    The previous code read company['contact_email'], but the companies table has
    no such column — so the value was always the '' default and every customer
    Stripe created here had no e-mail at all. Besides losing receipts, that made
    the Customer.list(email='') lookup below meaningless.
    """
    owner_id = company.get("owner_id")
    if not owner_id:
        return None
    try:
        res = supabase.table("users").select("email").eq("id", owner_id).single().execute()
        return ((res.data or {}).get("email") or None)
    except Exception as e:
        print(f"[STRIPE] Could not resolve billing e-mail for company {company.get('id')}: {e}")
        return None


def get_checkout_payment_method_types() -> List[str]:
    """Configured checkout payment methods, falling back to card + PayPal."""
    raw = os.environ.get("STRIPE_PAYMENT_METHOD_TYPES")
    if not raw:
        try:
            raw = db.secrets.get("STRIPE_PAYMENT_METHOD_TYPES")
        except Exception:
            raw = None
    if raw:
        types = [t.strip() for t in str(raw).split(",") if t.strip()]
        if types:
            return types
    return list(DEFAULT_CHECKOUT_PAYMENT_METHOD_TYPES)


# Startup debug (safe)
print("[STRIPE] Startup config")
print(f"[STRIPE] DATABUTTON_SERVICE_TYPE={os.environ.get('DATABUTTON_SERVICE_TYPE')!r} -> mode={mode}")
print(f"[STRIPE] stripe.api_key: {_secret_debug_info(stripe.api_key)}")
print(f"[STRIPE] STRIPE_WEBHOOK_SECRET: {_secret_debug_info(STRIPE_WEBHOOK_SECRET)}")
print(f"[STRIPE] checkout payment_method_types: {get_checkout_payment_method_types()}")

# Initialize Supabase
supabase_url = db.secrets.get("SUPABASE_URL")
supabase_service_key = db.secrets.get("SUPABASE_SERVICE_KEY")
supabase: Client = create_client(supabase_url, supabase_service_key)

# Pydantic Models
class CheckoutRequest(BaseModel):
    company_id: str
    success_url: str
    cancel_url: str
    plan_type: str = PRIMARY_PLAN  # single tier: "standard"
    billing_cycle: str  # "monthly" or "annual"
    extra_seats: int = 0  # additional seats beyond the plan's included users

class CheckoutResponse(BaseModel):
    checkout_url: str
    session_id: str

class PortalRequest(BaseModel):
    company_id: str
    return_url: str

class PortalResponse(BaseModel):
    portal_url: str

class SubscriptionStatus(BaseModel):
    has_active_subscription: bool
    subscription_id: Optional[str] = None
    status: Optional[str] = None
    current_period_end: Optional[datetime] = None
    product_name: Optional[str] = None

class UpdateSeatsRequest(BaseModel):
    company_id: str
    new_extra_seats: int

class ChangePlanRequest(BaseModel):
    company_id: str
    new_plan_type: str

def _create_session_dropping_bad_methods(checkout_params: Dict[str, Any]):
    """
    Create the Checkout Session, retiring individual payment methods Stripe refuses.

    A method that is not activated on the account, or that does not support
    subscriptions, makes the entire session fail — one bad entry would otherwise
    take checkout down. Stripe names the offending method in the error, so drop
    that one and try again. Only if nothing can be salvaged do we fall back to
    Stripe's dynamic methods, which is the case where the card guarantee is lost,
    so it is logged loudly.
    """
    params = dict(checkout_params)
    # At most one attempt per configured method, plus the dynamic fallback.
    for _ in range(len(params.get('payment_method_types') or []) + 1):
        try:
            return stripe.checkout.Session.create(**params)
        except stripe.InvalidRequestError as e:
            message = str(e)
            if 'payment_method_type' not in message:
                raise

            current = list(params.get('payment_method_types') or [])
            rejected = [m for m in current if f"'{m}'" in message or f'"{m}"' in message]

            if rejected and len(current) > len(rejected):
                remaining = [m for m in current if m not in rejected]
                print(
                    f"[STRIPE] Stripe refused payment method(s) {rejected}: {message} "
                    f"Retrying with {remaining}. Activate them in Stripe or drop them "
                    "from STRIPE_PAYMENT_METHOD_TYPES."
                )
                params['payment_method_types'] = remaining
                continue

            print(
                f"[STRIPE] Could not satisfy payment_method_types={current}: {message} "
                "Falling back to Stripe's dynamic payment methods — card is no longer "
                "guaranteed until STRIPE_PAYMENT_METHOD_TYPES is fixed."
            )
            params.pop('payment_method_types', None)

    # Loop exhausted without returning: try once more with whatever is left.
    return stripe.checkout.Session.create(**params)


@router.post("/create-checkout-session", response_model=CheckoutResponse)
async def create_checkout_session(request: CheckoutRequest, user_data: str = Depends(require_auth)):
    """
    Create a Stripe checkout session for subscribing to a Happy Client Flow plan.
    Supports Starter/Business plans with monthly/annual billing and extra seats.
    """
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")

    # Validate plan parameters. Only the current single tier may be purchased;
    # legacy plans remain resolvable elsewhere for existing subscriptions only.
    if request.plan_type not in SELECTABLE_PLANS:
        raise HTTPException(status_code=400, detail=f"Invalid plan_type: {request.plan_type}. Must be one of: {sorted(SELECTABLE_PLANS)}.")
    if request.billing_cycle not in ('monthly', 'annual'):
        raise HTTPException(status_code=400, detail=f"Invalid billing_cycle: {request.billing_cycle}. Must be 'monthly' or 'annual'.")
    if request.extra_seats < 0:
        raise HTTPException(status_code=400, detail="extra_seats cannot be negative.")

    try:
        # Get company info from database
        company_response = supabase.table('companies').select('*').eq('id', request.company_id).single().execute()

        if not company_response.data:
            raise HTTPException(status_code=404, detail="Company not found")

        company = company_response.data

        # Check if company already has an active subscription
        existing_sub = supabase.table('subscriptions').select('*').eq('company_id', request.company_id).eq('status', 'active').execute()

        if existing_sub.data:
            raise HTTPException(status_code=400, detail="Company already has an active subscription")

        # Create or retrieve Stripe customer
        customer_email = _resolve_billing_email(company)
        customer_name = company.get('name', '')
        existing_customer_id = company.get('stripe_customer_id')

        def _new_customer():
            # Omit the e-mail rather than sending '', so Stripe does not store a blank.
            fields = {'name': customer_name}
            if customer_email:
                fields['email'] = customer_email
            return stripe.Customer.create(**fields)

        if existing_customer_id:
            try:
                customer = stripe.Customer.retrieve(existing_customer_id)
            except stripe.InvalidRequestError:
                customer = _new_customer()
                supabase.table('companies').update({
                    'stripe_customer_id': customer.id
                }).eq('id', request.company_id).execute()
        else:
            customer = None
            # Only reuse by e-mail when we actually have one: listing on '' matches
            # by nothing meaningful and risks attaching another company's customer.
            if customer_email:
                customers = stripe.Customer.list(email=customer_email, limit=1)
                if customers.data:
                    customer = customers.data[0]
            if customer is None:
                customer = _new_customer()
            supabase.table('companies').update({
                'stripe_customer_id': customer.id
            }).eq('id', request.company_id).execute()

        # Resolve Stripe price IDs via lookup_keys
        plan_lookup_key = get_plan_lookup_key(request.plan_type, request.billing_cycle)
        base_prices = stripe.Price.list(lookup_keys=[plan_lookup_key], active=True)
        if not base_prices.data:
            raise HTTPException(status_code=500, detail=f"Stripe price not found for lookup_key: {plan_lookup_key}")

        line_items = [{'price': base_prices.data[0].id, 'quantity': 1}]

        # Add extra seat line item if needed
        if request.extra_seats > 0:
            seat_lookup_key = get_extra_seat_lookup_key(request.billing_cycle)
            seat_prices = stripe.Price.list(lookup_keys=[seat_lookup_key], active=True)
            if not seat_prices.data:
                raise HTTPException(status_code=500, detail=f"Stripe price not found for lookup_key: {seat_lookup_key}")
            line_items.append({'price': seat_prices.data[0].id, 'quantity': request.extra_seats})

        # Create checkout session with plan metadata.
        # Enable promo-code entry for both monthly and annual checkouts.
        checkout_params = {
            'customer': customer.id,
            'line_items': line_items,
            'mode': 'subscription',
            'success_url': request.success_url,
            'cancel_url': request.cancel_url,
            # Ensure VAT/sales tax is computed and added by Stripe at checkout.
            'automatic_tax': {'enabled': True},
            # Collect billing address to let Stripe determine tax jurisdiction.
            'billing_address_collection': 'required',
            # Let business customers provide VAT ID where applicable.
            'tax_id_collection': {'enabled': True},
            # Stripe requires name updates when tax_id_collection is on for existing customers.
            'customer_update': {'address': 'auto', 'name': 'auto'},
            'subscription_data': {
                'metadata': {
                    'company_id': request.company_id,
                    'plan_type': request.plan_type,
                    'billing_cycle': request.billing_cycle,
                    'extra_seats': str(request.extra_seats),
                }
            },
            'metadata': {
                'company_id': request.company_id,
                'plan_type': request.plan_type,
                'billing_cycle': request.billing_cycle,
                'extra_seats': str(request.extra_seats),
            },
            'allow_promotion_codes': True,
            # Name the methods explicitly so card cannot be dropped by Stripe's
            # per-session eligibility decisions (HCL-12).
            'payment_method_types': get_checkout_payment_method_types(),
        }

        session = _create_session_dropping_bad_methods(checkout_params)

        return CheckoutResponse(
            checkout_url=session.url,
            session_id=session.id
        )

    except HTTPException:
        raise
    except stripe.StripeError as e:
        raise HTTPException(status_code=400, detail=f"Stripe error: {str(e)}") from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}") from e

LEGACY_PRICE_ID = "price_1Ro10tFS4l6OGNWUaMYBCOmn"
NEW_PORTAL_CONFIG = "bpc_1TBJ7nFS4l6OGNWUdj7WyG7E"

@router.post("/create-portal-session", response_model=PortalResponse)
async def create_portal_session(request: PortalRequest, current_user: str = Depends(require_auth)):
    """
    Create a Stripe customer portal session for subscription management.
    Uses the new portal configuration for users on new pricing plans,
    and the default portal configuration for legacy users.
    """
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")
    
    print(f"[AUTH] Creating portal session for user: {current_user}")
    
    try:
        # Get subscription info including stripe_price_id to determine legacy vs new plan
        sub_response = supabase.table('subscriptions').select('stripe_customer_id, stripe_price_id').eq('company_id', request.company_id).execute()
        
        if not sub_response.data:
            raise HTTPException(status_code=404, detail="No subscription found for this company")
        
        customer_id = sub_response.data[0]['stripe_customer_id']
        stripe_price_id = sub_response.data[0].get('stripe_price_id')
        
        # Legacy users (old price) get the default portal; new plan users get the new portal config
        portal_params = {
            "customer": customer_id,
            "return_url": request.return_url,
        }
        if stripe_price_id != LEGACY_PRICE_ID:
            portal_params["configuration"] = NEW_PORTAL_CONFIG
        
        print(f"[STRIPE] Portal config: {'default (legacy)' if stripe_price_id == LEGACY_PRICE_ID else NEW_PORTAL_CONFIG} for price {stripe_price_id}")
        
        session = stripe.billing_portal.Session.create(**portal_params)
        
        return PortalResponse(portal_url=session.url)
        
    except stripe.StripeError as e:
        raise HTTPException(status_code=400, detail=f"Stripe error: {str(e)}") from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}") from e

@router.get("/subscription-status/{company_id}", response_model=SubscriptionStatus)
async def get_subscription_status(company_id: str, current_user: str = Depends(require_auth)):
    """
    Get current subscription status for a company using JOIN query
    """
    print(f"[AUTH] Getting subscription status for user: {current_user}")
    try:
        # Query with JOIN to get subscription details
        query = """
        SELECT 
            s.id as subscription_id,
            s.status,
            s.current_period_end,
            s.stripe_product_id
        FROM companies c
        LEFT JOIN subscriptions s ON c.id = s.company_id 
            AND s.status = 'active'
        WHERE c.id = %s
        """
        
        result = supabase.rpc('exec_sql', {'sql': query, 'params': [company_id]}).execute()
        
        if not result.data:
            return SubscriptionStatus(has_active_subscription=False)
        
        subscription_data = result.data[0] if result.data else {}
        
        has_active = bool(subscription_data.get('subscription_id'))
        
        return SubscriptionStatus(
            has_active_subscription=has_active,
            subscription_id=subscription_data.get('subscription_id'),
            status=subscription_data.get('status'),
            current_period_end=subscription_data.get('current_period_end'),
            product_name="Happy Client Flow Pro" if has_active else None
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error checking subscription status: {str(e)}") from e

@router.post("/update-seats")
async def update_seats(request: UpdateSeatsRequest, current_user: str = Depends(require_auth)):
    """
    Update extra seats on an existing subscription
    """
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")

    if request.new_extra_seats < 0:
        raise HTTPException(status_code=400, detail="Extra seats cannot be negative")

    try:
        # Get active subscription
        sub_response = supabase.table('subscriptions').select('*').eq('company_id', request.company_id).eq('status', 'active').execute()
        
        if not sub_response.data:
            raise HTTPException(status_code=404, detail="No active subscription found")
            
        subscription_record = sub_response.data[0]
        stripe_sub_id = subscription_record['stripe_subscription_id']
        stripe_customer_id = subscription_record['stripe_customer_id']
        billing_cycle = subscription_record.get('billing_cycle', 'monthly')

        # Get extra seat lookup key for this billing cycle
        seat_lookup_key = get_extra_seat_lookup_key(billing_cycle)
        seat_prices = stripe.Price.list(lookup_keys=[seat_lookup_key], active=True)
        if not seat_prices.data:
            raise HTTPException(status_code=500, detail=f"Stripe price not found for extra seats: {seat_lookup_key}")
        seat_price_id = seat_prices.data[0].id

        # Retrieve Stripe subscription
        stripe_sub = stripe.Subscription.retrieve(stripe_sub_id)
        
        # Find existing extra seat item
        extra_seat_item = None
        for item in stripe_sub['items']['data']:
            price_lookup = item.get('price', {}).get('lookup_key')
            if price_lookup and is_extra_seat_lookup_key(price_lookup):
                extra_seat_item = item
                break
                
        # Update subscription items
        if extra_seat_item:
            if request.new_extra_seats == 0:
                stripe.SubscriptionItem.delete(extra_seat_item.id)
            else:
                stripe.SubscriptionItem.modify(extra_seat_item.id, quantity=request.new_extra_seats)
        else:
            if request.new_extra_seats > 0:
                stripe.SubscriptionItem.create(
                    subscription=stripe_sub.id,
                    price=seat_price_id,
                    quantity=request.new_extra_seats
                )

        # CRITICAL: re-assert VAT on the re-rated subscription so added seats are
        # never billed tax-free.
        _enforce_automatic_tax(stripe_sub.id, stripe_customer_id)

        # Update local DB optimistically (webhook will also update)
        supabase.table('subscriptions').update({
            'extra_seats': request.new_extra_seats
        }).eq('id', subscription_record['id']).execute()
        
        return {"status": "success", "message": "Seats updated"}
                
    except stripe.StripeError as e:
        raise HTTPException(status_code=400, detail=f"Stripe error: {str(e)}") from e
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}") from e

@router.post("/change-plan")
async def change_plan(request: ChangePlanRequest, current_user: str = Depends(require_auth)):
    """
    Change the base plan on an existing subscription
    """
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")

    if request.new_plan_type not in PLANS:
        raise HTTPException(status_code=400, detail=f"Invalid plan_type: {request.new_plan_type}")

    try:
        # Get active subscription
        sub_response = supabase.table('subscriptions').select('*').eq('company_id', request.company_id).eq('status', 'active').execute()
        
        if not sub_response.data:
            raise HTTPException(status_code=404, detail="No active subscription found")
            
        subscription_record = sub_response.data[0]
        stripe_sub_id = subscription_record['stripe_subscription_id']
        billing_cycle = subscription_record.get('billing_cycle', 'monthly')
        
        # Get new plan lookup key for current billing cycle
        new_plan_lookup_key = get_plan_lookup_key(request.new_plan_type, billing_cycle)
        new_prices = stripe.Price.list(lookup_keys=[new_plan_lookup_key], active=True)
        if not new_prices.data:
            raise HTTPException(status_code=500, detail=f"Stripe price not found for plan: {new_plan_lookup_key}")
        new_price_id = new_prices.data[0].id

        # Retrieve Stripe subscription
        stripe_sub = stripe.Subscription.retrieve(stripe_sub_id)
        
        # Find existing base plan item
        plan_item = None
        for item in stripe_sub['items']['data']:
            price_lookup = item.get('price', {}).get('lookup_key', '')
            if resolve_plan_from_lookup_key(price_lookup):
                plan_item = item
                break
                
        if not plan_item:
            raise HTTPException(status_code=500, detail="Could not find base plan on subscription")
            
        # Modify the subscription
        stripe.Subscription.modify(
            stripe_sub.id,
            items=[{
                "id": plan_item.id,
                "price": new_price_id,
            }],
            proration_behavior="create_prorations"
        )

        # CRITICAL: re-assert VAT on the re-rated subscription so the changed plan
        # is never billed tax-free.
        _enforce_automatic_tax(stripe_sub.id, subscription_record['stripe_customer_id'])

        # Update local DB optimistically (webhook will also update)
        supabase.table('subscriptions').update({
            'plan_type': request.new_plan_type,
            'included_users': PLANS[request.new_plan_type]['included_users']
        }).eq('id', subscription_record['id']).execute()
        
        return {"status": "success", "message": "Plan changed"}
        
    except stripe.StripeError as e:
        raise HTTPException(status_code=400, detail=f"Stripe error: {str(e)}") from e
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}") from e

class InvoiceItem(BaseModel):
    id: str
    number: Optional[str] = None
    amount_due: int
    currency: str
    status: Optional[str] = None
    created: int
    invoice_pdf: Optional[str] = None
    hosted_invoice_url: Optional[str] = None

class InvoiceListResponse(BaseModel):
    invoices: List[InvoiceItem]
    has_more: bool

@router.get("/invoices/{company_id}")
async def get_invoices(company_id: str, current_user: str = Depends(require_auth)):
    """
    Get Stripe invoices for a company
    """
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")

    print(f"[STRIPE] Getting invoices for company {company_id}, user: {current_user}")

    try:
        # Look up stripe_customer_id from subscriptions table first
        sub_response = supabase.table('subscriptions').select('stripe_customer_id').eq('company_id', company_id).execute()

        customer_id = None
        if sub_response.data:
            customer_id = sub_response.data[0].get('stripe_customer_id')

        # Fallback: look up stripe_customer_id from companies table
        if not customer_id:
            company_response = supabase.table('companies').select('stripe_customer_id').eq('id', company_id).single().execute()
            if company_response.data:
                customer_id = company_response.data.get('stripe_customer_id')

        if not customer_id:
            # No Stripe customer found – return empty list (not an error)
            return InvoiceListResponse(invoices=[], has_more=False)

        # Fetch invoices from Stripe
        stripe_invoices = stripe.Invoice.list(
            customer=customer_id,
            limit=100,
        )

        invoices = []
        for inv in stripe_invoices.data:
            invoices.append(InvoiceItem(
                id=inv.id,
                number=inv.number,
                amount_due=inv.amount_due,
                currency=inv.currency,
                status=inv.status,
                created=inv.created,
                invoice_pdf=inv.invoice_pdf,
                hosted_invoice_url=inv.hosted_invoice_url,
            ))

        return InvoiceListResponse(
            invoices=invoices,
            has_more=stripe_invoices.has_more,
        )

    except stripe.StripeError as e:
        print(f"[STRIPE] Error fetching invoices: {str(e)}")
        raise HTTPException(status_code=400, detail=f"Stripe error: {str(e)}") from e
    except Exception as e:
        print(f"[STRIPE] Error fetching invoices: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}") from e

@router.post("/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    """
    Handle Stripe webhooks for subscription events
    """
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")
    
    body = await request.body()
    # Safe runtime debug
    print("[STRIPE] Webhook received")
    print(f"[STRIPE] mode={mode} DATABUTTON_SERVICE_TYPE={os.environ.get('DATABUTTON_SERVICE_TYPE')!r}")
    print(f"[STRIPE] stripe.api_key: {(stripe.api_key)}")
    print(f"[STRIPE] STRIPE_WEBHOOK_SECRET: {(STRIPE_WEBHOOK_SECRET)}")
    print(f"[STRIPE] stripe_signature header present: {bool(stripe_signature)}")
    if stripe_signature:
        # Print a short prefix only; full header is sensitive.
        print(f"[STRIPE] stripe_signature prefix: {stripe_signature[:24]}...")
    print(f"[STRIPE] raw body length: {len(body)} bytes")
    
    try:
        event = stripe.Webhook.construct_event(
            body, stripe_signature, STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except Exception as e:
        # Stripe library versions differ: exception may live under stripe._error
        sig_err_type = getattr(getattr(stripe, "_error", None), "SignatureVerificationError", None)
        if sig_err_type and isinstance(e, sig_err_type):
            # Helpful debug: inspect unverified payload for mode/type (do NOT trust it for business logic)
            try:
                payload = json.loads(body.decode("utf-8"))
                livemode = payload.get("livemode")
                event_type = payload.get("type")
                event_id = payload.get("id")
                print(f"[STRIPE] Unverified payload hints: livemode={livemode} type={event_type} id={event_id}")
            except Exception:
                print("[STRIPE] Could not parse payload JSON for livemode/type debug")
            print(f"[STRIPE] Signature verification failed: {str(e)}")
            raise HTTPException(status_code=400, detail="Invalid signature")
        # Fallback: re-raise unexpected errors
        print(f"[STRIPE] Unexpected error during signature verification: {type(e).__name__}: {e}")
        raise
    
    print(f"Received Stripe webhook: {event['type']}")
    
    try:
        # Handle checkout completion - this is where we capture the company mapping if this occurs before subscription.completed
        if event['type'] == 'checkout.session.completed':
            session = event['data']['object']
            customer_id = session.get('customer')
            
            # Try to get company_id from metadata first (most reliable)
            company_id = session.get('metadata', {}).get('company_id')

            # Fallback to client_reference_id if metadata is not available
            if not company_id:
                company_id = session.get('client_reference_id')

            if not company_id:
                print("Warning: No company reference found in checkout session")
                return {"status": "error", "message": "No company reference found"}

            if not customer_id:
                print("Warning: No customer ID found in checkout session")
                return {"status": "error", "message": "No customer ID found"}

            # Update company with stripe_customer_id
            supabase.table('companies').update({'stripe_customer_id': customer_id}).eq('id', company_id).execute()

            # Check for a floating subscription and update it
            # This may occur if customer.subscription.created occurs before this
            supabase.table('subscriptions').update({'company_id': company_id}).eq('stripe_customer_id', customer_id).execute()
            
            print(f"Successfully processed checkout for company {company_id} and customer {customer_id}")
            return {"status": "success"}

        # Handle subscription events - use the stored mapping
        elif event['type'] in ['customer.subscription.created', 'customer.subscription.updated',
                              'customer.subscription.deleted', 'invoice.payment_succeeded']:

            event_object = event['data']['object']
            customer_id = event_object.get('customer')

            # For invoice events, the customer is on the subscription, not the invoice
            if not customer_id and event_object.get('object') == 'invoice':
                subscription_id = event_object.get('subscription')
                if subscription_id:
                    try:
                        subscription = stripe.Subscription.retrieve(subscription_id)
                        customer_id = subscription.customer
                    except stripe.StripeError as e:
                        print(f"Error retrieving subscription {subscription_id} for invoice: {e}")

            if not customer_id:
                print(f"Could not determine customer from {event['type']} event")
                return {"status": "error", "message": "Could not determine customer"}

            # Look up company by the stored stripe_customer_id
            # If subscription event occurs first before session event, company will have no information, kept as None
            company_result = supabase.table('companies').select('id').eq('stripe_customer_id', customer_id).execute()
            company_id = company_result.data[0]['id'] if company_result.data else None

            # Handle specific subscription events
            if event['type'] in ['customer.subscription.created', 'customer.subscription.updated']:
                subscription = event_object
                subscription_data = {
                    'company_id': company_id,
                    'stripe_subscription_id': subscription['id'],
                    'stripe_customer_id': customer_id,
                    'stripe_product_id': subscription['items']['data'][0]['price']['product'] if subscription.get('items', {}).get('data') else None,
                    'stripe_price_id': subscription['items']['data'][0]['price']['id'] if subscription.get('items', {}).get('data') else None,
                    'status': subscription['status'],
                    'current_period_start': datetime.fromtimestamp(subscription['current_period_start'], tz=timezone.utc).isoformat(),
                    'current_period_end': datetime.fromtimestamp(subscription['current_period_end'], tz=timezone.utc).isoformat(),
                    'updated_at': datetime.now(timezone.utc).isoformat()
                }

                # Extract plan metadata from subscription items via lookup keys
                items = subscription.get('items', {}).get('data', [])
                plan_info = None
                extra_seats = 0

                for item in items:
                    price = item.get('price', {})
                    lookup_key = price.get('lookup_key', '') or ''

                    resolved = resolve_plan_from_lookup_key(lookup_key)
                    if resolved:
                        plan_info = resolved

                    if is_extra_seat_lookup_key(lookup_key):
                        extra_seats = item.get('quantity', 0)

                if plan_info:
                    subscription_data['plan_type'] = plan_info['plan_type']
                    subscription_data['billing_cycle'] = plan_info['billing_cycle']
                    subscription_data['included_users'] = plan_info['included_users']
                    subscription_data['extra_seats'] = extra_seats
                    print(f"[STRIPE] Plan resolved: {plan_info['plan_type']} ({plan_info['billing_cycle']}), extra_seats={extra_seats}")
                else:
                    # Fallback: check subscription metadata
                    checkout_meta = subscription.get('metadata', {})
                    if checkout_meta.get('plan_type'):
                        pt = checkout_meta['plan_type']
                        bc = checkout_meta.get('billing_cycle', 'monthly')
                        es = int(checkout_meta.get('extra_seats', '0'))
                        if pt in PLANS:
                            subscription_data['plan_type'] = pt
                            subscription_data['billing_cycle'] = bc
                            subscription_data['included_users'] = PLANS[pt]['included_users']
                            subscription_data['extra_seats'] = es
                            print(f"[STRIPE] Plan from metadata: {pt} ({bc}), extra_seats={es}")

                # Upsert subscription, allowing for floating subscriptions
                supabase.table('subscriptions').upsert(subscription_data, on_conflict='stripe_subscription_id').execute()
                print(f"Upserted subscription for company {company_id or 'unassigned'} with status {subscription['status']}")

            elif event['type'] == 'customer.subscription.deleted':
                subscription = event_object
                supabase.table('subscriptions').update({
                    'status': 'canceled',
                    'canceled_at': datetime.now(timezone.utc).isoformat(),
                    'updated_at': datetime.now(timezone.utc).isoformat()
                }).eq('stripe_subscription_id', subscription['id']).execute()
                print(f"Canceled subscription for company {company_id or 'unassigned'}")
            elif event['type'] == 'invoice.payment_succeeded':
                print(f"Payment succeeded for company {company_id}")
                # Could add logic here to update payment status if needed
        return {"status": "success"}
        
    except ValueError as e:
        print(f"Webhook signature verification failed: {str(e)}")
        raise HTTPException(status_code=400, detail="Invalid signature") from e
    except Exception as e:
        print(f"Webhook processing failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Webhook processing failed: {str(e)}") from e

@router.post("/webhook-v2")
async def stripe_webhook_v2(request: Request, stripe_signature: str = Header(None)):
    """
    A simple test webhook endpoint to log incoming requests from Stripe.
    """
    print("--- Received request on /webhook-v2 ---")
    
    # Log headers
    headers = dict(request.headers)
    print("Headers:")
    for key, value in headers.items():
        print(f"  {key}: {value}")
        
    # Log body
    body = await request.body()
    print("Body:")
    print(body.decode('utf-8'))

    try:
        event = stripe.Webhook.construct_event(
            body, stripe_signature, STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    
    print("--- End of request on /webhook-v2 ---")
    
    return {"status": "received", "event": event['type']}

# Utility functions for checkout session completion
async def handle_checkout_completed(session):
    """
    Handle checkout session completion
    """
    try:
        company_id = session.get('client_reference_id')
        customer_id = session['customer']
        
        if not company_id:
            print("Warning: No client_reference_id found in checkout session")
            return
            
        # Store the stripe_customer_id in the companies table
        supabase.table('companies').update({
            'stripe_customer_id': customer_id,
            'updated_at': datetime.now(timezone.utc).isoformat()
        }).eq('id', company_id).execute()
        
        print(f"Updated company {company_id} with Stripe customer ID {customer_id}")
        
    except Exception as e:
        print(f"Error handling checkout completed: {str(e)}")

async def find_company_with_retry(customer_id: str, max_retries: int = 10, delay: float = 3.0) -> Optional[str]:
    """
    Find company by stripe_customer_id with retry mechanism and fallback lookups
    
    Args:
        customer_id: Stripe customer ID
        max_retries: Maximum number of retry attempts (default: 10)
        delay: Delay between retries in seconds (default: 3.0)
    
    Returns:
        Company ID if found, None otherwise
    """
    for attempt in range(max_retries + 1):
        try:
            # Primary lookup: by stripe_customer_id
            company_result = supabase.table('companies').select('*').eq('stripe_customer_id', customer_id).execute()
            
            if company_result.data:
                company_id = company_result.data[0]['id']
                print(f"Found company {company_id} for customer {customer_id} on attempt {attempt + 1}")
                return company_id
            
            # Fallback lookup: by customer email if primary fails
            try:
                stripe_customer = stripe.Customer.retrieve(customer_id)
                if stripe_customer.email:
                    email_result = supabase.table('companies').select('*').eq('contact_email', stripe_customer.email).execute()
                    
                    if email_result.data:
                        company_id = email_result.data[0]['id']
                        print(f"Found company {company_id} by email fallback for customer {customer_id} on attempt {attempt + 1}")
                        
                        # Update the company with stripe_customer_id for future lookups
                        supabase.table('companies').update({
                            'stripe_customer_id': customer_id
                        }).eq('id', company_id).execute()
                        
                        return company_id
            except stripe.StripeError as e:
                print(f"Stripe API error during fallback lookup: {str(e)}")
            
            # If this is the last attempt, don't wait
            if attempt < max_retries:
                print(f"Company not found for customer {customer_id}, attempt {attempt + 1}/{max_retries + 1}. Retrying in {delay} seconds...")
                await asyncio.sleep(delay)
            else:
                print(f"Company not found for customer {customer_id} after {max_retries + 1} attempts")
                
        except Exception as e:
            print(f"Error during company lookup attempt {attempt + 1}: {str(e)}")
            if attempt < max_retries:
                await asyncio.sleep(delay)
    
    return None
