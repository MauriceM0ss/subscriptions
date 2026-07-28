"""Unit tests for the cost / renewal derivation."""
import importlib
from datetime import date

import subscriptions
importlib.reload(subscriptions)


def test_monthly_yearly_from_monthly():
    assert subscriptions.monthly_yearly(12.99, "monthly") == (12.99, 155.88)


def test_monthly_yearly_from_yearly():
    m, y = subscriptions.monthly_yearly(120, "yearly")
    assert y == 120 and m == 10.0


def test_add_months_clamps_day():
    assert subscriptions.add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)  # leap year


def test_charges_counts_initial_charge():
    # Monthly, started exactly 3 months ago → charges at month 0,1,2,3 = 4.
    start = date(2024, 1, 1)
    today = date(2024, 4, 1)
    assert subscriptions.charges_elapsed(start, "monthly", today) == 4


def test_charges_respects_day_of_month():
    start = date(2024, 1, 15)
    today = date(2024, 4, 10)          # 14th hasn't come round yet in April
    assert subscriptions.charges_elapsed(start, "monthly", today) == 3


def test_charges_future_start_is_zero():
    assert subscriptions.charges_elapsed(date(2999, 1, 1), "monthly", date(2024, 1, 1)) == 0


def test_yearly_charges():
    assert subscriptions.charges_elapsed(date(2020, 6, 1), "yearly", date(2024, 6, 1)) == 5


def test_next_renewal_rolls_past_dates_forward():
    nr = subscriptions.next_renewal(date(2024, 1, 1), "monthly", date(2024, 3, 15))
    assert nr == date(2024, 4, 1)


def test_next_renewal_keeps_future_date():
    nr = subscriptions.next_renewal(date(2030, 1, 1), "monthly", date(2024, 1, 1))
    assert nr == date(2030, 1, 1)


def test_enrich_shapes_all_fields():
    row = {"name": "X", "billing_cycle": "monthly", "amount": 10,
           "start_date": "2024-01-01", "renew_date": "2024-02-01", "active": 1}
    d = subscriptions.enrich(row, date(2024, 4, 1))
    assert d["monthly_cost"] == 10 and d["yearly_cost"] == 120
    assert d["charges"] == 4 and d["total_spent"] == 40
    # renew 2024-02-01 rolls forward to the first occurrence on/after today.
    assert d["next_renewal"] == "2024-04-01" and d["active"] is True
    assert d["days_until_renew"] == 0


def test_enrich_inactive_has_no_countdown():
    row = {"name": "X", "billing_cycle": "monthly", "amount": 10,
           "start_date": "2024-01-01", "renew_date": "2024-02-01", "active": 0}
    d = subscriptions.enrich(row, date(2024, 4, 1))
    assert d["next_renewal"] == "" and d["days_until_renew"] is None


def test_periods_spend_single_on_month():
    # Switched on for exactly one month → one monthly charge.
    periods = [(date(2024, 3, 1), date(2024, 4, 1))]
    count, total = subscriptions.periods_spend(periods, [], "monthly", 6, date(2024, 6, 1))
    assert count == 1 and total == 6


def test_periods_spend_open_period_counts_to_today():
    periods = [(date(2024, 1, 1), None)]
    count, total = subscriptions.periods_spend(periods, [], "monthly", 10, date(2024, 4, 1))
    assert count == 4 and total == 40          # Jan, Feb, Mar, Apr


def test_periods_spend_two_spells_sum():
    periods = [(date(2024, 1, 1), date(2024, 2, 1)), (date(2024, 5, 1), date(2024, 6, 1))]
    count, total = subscriptions.periods_spend(periods, [], "monthly", 6, date(2024, 12, 1))
    assert count == 2 and total == 12


def test_periods_spend_honours_price_history():
    periods = [(date(2024, 1, 1), None)]
    prices = [(date(2024, 1, 1), 10), (date(2024, 3, 1), 20)]  # price rose on Mar 1
    count, total = subscriptions.periods_spend(periods, prices, "monthly", 20, date(2024, 4, 1))
    assert count == 4 and total == 60          # 10 + 10 + 20 + 20
