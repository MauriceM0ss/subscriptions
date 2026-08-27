"""Cost & renewal derivation — the single source of truth for the monthly,
yearly and total-spent figures shown across the app.

Only the billing cycle and per-cycle amount are stored; everything else is
computed from them (and the start date) so the numbers can never disagree.
"""
import calendar
from datetime import date, timedelta


def parse_date(s):
    """Parse an ISO date (YYYY-MM-DD); return None for empty/invalid input."""
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return None


def add_months(d, n):
    """d shifted by n months, clamping the day to the target month's length
    (e.g. Jan 31 + 1 month → Feb 28/29)."""
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def add_cycle(d, cycle, n=1):
    """d advanced by n billing cycles (monthly or yearly)."""
    return add_months(d, 12 * n if cycle == "yearly" else n)


def monthly_yearly(amount, cycle):
    """(monthly, yearly) equivalents of a per-cycle amount."""
    if cycle == "yearly":
        return round(amount / 12, 2), round(amount, 2)
    return round(amount, 2), round(amount * 12, 2)


def charges_elapsed(start, cycle, today):
    """How many times the subscription has been billed on or before `today`,
    counting the initial charge on the start date. 0 if not started yet."""
    if start is None or start > today:
        return 0
    if cycle == "yearly":
        k = today.year - start.year
    else:
        k = (today.year - start.year) * 12 + (today.month - start.month)
    if add_cycle(start, cycle, k) > today:      # overshot on the day-of-month
        k -= 1
    return k + 1


def next_renewal(renew, cycle, today):
    """The renewal date rolled forward to the first occurrence on/after today,
    so a date that has already passed still shows a sensible countdown."""
    if renew is None:
        return None
    d = renew
    guard = 0
    while d < today and guard < 4000:
        d = add_cycle(d, cycle)
        guard += 1
    return d


def price_on(prices, d, base):
    """The per-cycle amount in effect on date `d`.

    `prices` is a list of (date, amount) sorted ascending; `base` is the amount
    to assume for charges before the first recorded price."""
    amt = base
    for pd, pa in prices:
        if pd <= d:
            amt = pa
        else:
            break
    return amt


def periods_spend(periods, prices, cycle, amount, today):
    """Count real charges and total spend across on/off activation periods.

    A charge lands on the day a period opens and every billing cycle after,
    up to (but not including) the day it closes — and never past today. Each
    charge is valued at the price that was in effect on its date, so a price
    change part-way through is reflected in the historical total.

    Returns (charge_count, total_spent). The current billing cycle is used for
    the cadence; only the amount is treated as varying over time."""
    prices = sorted(prices)
    base = prices[0][1] if prices else amount
    cap = today + timedelta(days=1)          # count charges up to and including today
    count = 0
    total = 0.0
    for start, end in periods:
        if start is None:
            continue
        stop = min(end, cap) if end else cap
        d = start
        guard = 0
        while d < stop and guard < 6000:
            total += price_on(prices, d, base)
            count += 1
            d = add_cycle(d, cycle)
            guard += 1
    return count, round(total, 2)


def usage_spend(usages):
    """Count and total the logged months of a one-time subscription.

    `usages` is a list of amounts, one per month actually switched on. Each is
    stored at the price that applied when it was logged, so a later price
    change never rewrites what was already paid."""
    return len(usages), round(sum(float(a) for a in usages), 2)


def enrich(row, today, periods=None, prices=None, usages=None):
    """Turn a subscriptions row into a plain dict with the derived fields added.

    When `periods` (list of (started, ended|None) dates) and `prices` (list of
    (changed_on, amount) dates) are supplied, `total_spent` reflects the months
    the subscription was actually switched on and the price history. Without
    them it falls back to the simple "charges since start × current price"
    estimate.

    A one-time subscription (one whose category is flagged `one_time`) ignores
    all of that: it is not a recurring commitment, so it carries no monthly or
    yearly cost and no renewal countdown, and its total is simply the months
    logged against it in `usages`."""
    d = dict(row)
    cycle = (d.get("billing_cycle") or "monthly").lower()
    amount = float(d.get("amount") or 0)
    start = parse_date(d.get("start_date"))
    renew = parse_date(d.get("renew_date"))
    active = bool(d.get("active", 1))
    one_time = bool(d.get("one_time", 0))

    if one_time:
        # No recurring cost: these must not reach the per-month / per-year or
        # spent-so-far totals, which is what a zero here guarantees.
        monthly = yearly = 0.0
        charges, total_spent = usage_spend(usages or [])
        nr = None
    else:
        monthly, yearly = monthly_yearly(amount, cycle)
        if periods is None:
            charges = charges_elapsed(start, cycle, today)
            total_spent = round(charges * amount, 2)
        else:
            charges, total_spent = periods_spend(periods, prices or [], cycle, amount, today)
        # A countdown only makes sense while the subscription is switched on.
        nr = next_renewal(renew, cycle, today) if active else None

    d["billing_cycle"] = cycle
    d["amount"] = round(amount, 2)
    d["active"] = active
    d["one_time"] = one_time
    d["monthly_cost"] = monthly
    d["yearly_cost"] = yearly
    d["charges"] = charges
    d["total_spent"] = total_spent
    d["next_renewal"] = nr.isoformat() if nr else ""
    d["days_until_renew"] = (nr - today).days if nr else None
    return d
