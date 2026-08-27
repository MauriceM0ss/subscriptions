"""Runtime configuration and small shared primitives.

Values that get reassigned under test (DB_PATH) live here and must be accessed
as ``config.NAME`` by other modules — never ``from config import NAME`` — so a
reassignment is seen everywhere.
"""
import os
from pathlib import Path
from datetime import datetime, timezone

DB_PATH = Path(os.environ.get("DB_PATH", "/data/subscriptions.db"))

# Categories the taxonomy starts with on first run; fully editable afterwards.
SEED_CATEGORIES = ("Media", "Productivity", "Gaming")

# Subscriptions in a "one-time" category are not recurring commitments: they are
# switched on for a single month whenever they are needed (a mod host used once
# a year, say). They are excluded from the per-month, per-year and spent-so-far
# totals, and instead accumulate one logged charge per month actually used.
ONE_TIME_CATEGORY = "One Time Subscriptions"

# Money shown throughout the UI. Just a display symbol — no conversion happens.
DEFAULT_CURRENCY = os.environ.get("CURRENCY", "€")   # €

BILLING_CYCLES = ("monthly", "yearly")

# How a subscription is paid. Fixed list; "" (unset) is always allowed too.
PAYMENT_METHODS = ("Credit Card", "PayPal", "iDeal")

# How much you'd miss the subscription — surfaced as a coloured chip.
NECESSITIES = ("Nice to Have", "Important", "Critical")
DEFAULT_NECESSITY = "Important"

# A renewal is "upcoming" (and surfaced in the sidebar / overview) within this
# many days of today.
UPCOMING_DAYS = max(1, int(os.environ.get("UPCOMING_DAYS", "30")))


def _envflag(name, default="1"):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


# Optional HTTP Basic auth. When both are set, every request must authenticate;
# when unset the app is open (the default for localhost / self-hosted use).
AUTH_USER = os.environ.get("AUTH_USER", "")
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def today():
    """Today's date (UTC), used for all cost/renewal maths."""
    return datetime.now(timezone.utc).date()
