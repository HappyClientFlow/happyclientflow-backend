"""
Pricing Configuration

Centralized pricing constants for the user-based pricing model.
Used by checkout session creation, webhook handler, and user limit enforcement.

Plans:
  - Standard: €199/mo net, 3 users included (annual = 2 months free → €1,990/yr)
  - Extra seats: €49/mo net each (annual = 2 months free → €490/yr)

Legacy (kept resolvable until all subscribers are migrated off them — see migration plan):
  - Starter: 3 users included
  - Business: 10 users included
"""

from typing import Optional

# Active plan offered to new customers. Single tier.
PRIMARY_PLAN = "standard"

PLANS = {
    "standard": {
        "included_users": 3,
        "monthly_lookup_key": "standard_monthly",
        "annual_lookup_key": "standard_annual",
    },
    # --- Legacy plans (no longer sold) -------------------------------------
    # Retained ONLY so existing subscriptions still resolve during migration.
    # Remove once no live subscription references these lookup keys.
    "starter": {
        "included_users": 3,
        "monthly_lookup_key": "starter_monthly",
        "annual_lookup_key": "starter_annual",
    },
    "business": {
        "included_users": 10,
        "monthly_lookup_key": "business_monthly",
        "annual_lookup_key": "business_annual",
    },
}

# Plans that can be selected at checkout / used for new subscriptions.
SELECTABLE_PLANS = {"standard"}

EXTRA_SEAT = {
    "monthly_lookup_key": "extra_seat_monthly",
    "annual_lookup_key": "extra_seat_annual",
}

# All valid lookup keys for quick membership checks
ALL_PLAN_LOOKUP_KEYS = set()
for plan_config in PLANS.values():
    ALL_PLAN_LOOKUP_KEYS.add(plan_config["monthly_lookup_key"])
    ALL_PLAN_LOOKUP_KEYS.add(plan_config["annual_lookup_key"])

EXTRA_SEAT_LOOKUP_KEYS = {
    EXTRA_SEAT["monthly_lookup_key"],
    EXTRA_SEAT["annual_lookup_key"],
}


def resolve_plan_from_lookup_key(lookup_key: str) -> Optional[dict]:
    """
    Given a Stripe price lookup_key, return the plan info dict or None.

    Returns:
        {
            "plan_type": "starter" | "business",
            "billing_cycle": "monthly" | "annual",
            "included_users": int,
        }
    """
    if not lookup_key:
        return None

    for plan_name, plan_config in PLANS.items():
        if lookup_key == plan_config["monthly_lookup_key"]:
            return {
                "plan_type": plan_name,
                "billing_cycle": "monthly",
                "included_users": plan_config["included_users"],
            }
        if lookup_key == plan_config["annual_lookup_key"]:
            return {
                "plan_type": plan_name,
                "billing_cycle": "annual",
                "included_users": plan_config["included_users"],
            }

    return None


def is_extra_seat_lookup_key(lookup_key: str) -> bool:
    """Check if a lookup_key corresponds to an extra seat price."""
    return lookup_key in EXTRA_SEAT_LOOKUP_KEYS


def get_extra_seat_lookup_key(billing_cycle: str) -> str:
    """Get the extra seat lookup key for a given billing cycle."""
    return EXTRA_SEAT[f"{billing_cycle}_lookup_key"]


def get_plan_lookup_key(plan_type: str, billing_cycle: str) -> str:
    """Get the plan lookup key for a given plan type and billing cycle."""
    plan = PLANS.get(plan_type)
    if not plan:
        raise ValueError(f"Unknown plan type: {plan_type}")
    return plan[f"{billing_cycle}_lookup_key"]
