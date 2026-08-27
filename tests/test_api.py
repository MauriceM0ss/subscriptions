"""End-to-end tests for the HTTP API."""
from conftest import make_sub


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.get_json()["status"] == "ok"


def test_index_serves_page(client):
    r = client.get("/")
    assert r.status_code == 200 and b"Subscriptions" in r.data


def test_seed_categories_present(client):
    tree = client.get("/api/tree").get_json()
    names = [c["name"] for c in tree["tree"]]
    # The one-time bucket is created alongside the seeds, and flagged.
    assert names == ["Media", "Productivity", "Gaming", "One Time Subscriptions"]
    one_time = [c for c in tree["tree"] if c["one_time"]]
    assert [c["name"] for c in one_time] == ["One Time Subscriptions"]


def test_create_and_get_subscription(client):
    r = make_sub(client)
    assert r.status_code == 201
    sid = r.get_json()["id"]
    got = client.get(f"/api/subscriptions/{sid}").get_json()
    assert got["name"] == "Netflix"
    assert got["monthly_cost"] == 12.99
    assert got["yearly_cost"] == 155.88


def test_create_requires_name(client):
    r = make_sub(client, name="")
    assert r.status_code == 400


def test_create_rejects_bad_cycle(client):
    r = make_sub(client, billing_cycle="weekly")
    assert r.status_code == 400


def test_create_rejects_negative_amount(client):
    r = make_sub(client, amount=-5)
    assert r.status_code == 400


def test_yearly_subscription_derives_monthly(client):
    sid = make_sub(client, billing_cycle="yearly", amount=120).get_json()["id"]
    got = client.get(f"/api/subscriptions/{sid}").get_json()
    assert got["yearly_cost"] == 120 and got["monthly_cost"] == 10.0


def test_renew_date_defaults_from_start(client):
    sid = make_sub(client, renew_date="").get_json()["id"]
    got = client.get(f"/api/subscriptions/{sid}").get_json()
    assert got["renew_date"] == "2023-02-01"     # start + one month


def test_update_subscription(client):
    sid = make_sub(client).get_json()["id"]
    r = client.put(f"/api/subscriptions/{sid}", json={"amount": 20, "active": False})
    assert r.status_code == 200
    got = client.get(f"/api/subscriptions/{sid}").get_json()
    assert got["monthly_cost"] == 20 and got["active"] is False


def test_delete_subscription(client):
    sid = make_sub(client).get_json()["id"]
    assert client.delete(f"/api/subscriptions/{sid}").status_code == 200
    assert client.get(f"/api/subscriptions/{sid}").status_code == 404


def test_overview_totals_only_active(client):
    make_sub(client, name="A", amount=10)
    make_sub(client, name="B", amount=5, active=False)
    ov = client.get("/api/overview").get_json()
    assert ov["active_count"] == 1
    assert ov["monthly_total"] == 10.0


def test_tree_totals_and_category_grouping(client):
    make_sub(client, name="A", amount=10, category_id=1)
    make_sub(client, name="B", amount=8, category_id=2)
    tree = client.get("/api/tree").get_json()
    assert tree["totals"]["active"] == 2
    assert tree["totals"]["monthly_total"] == 18.0
    media = next(c for c in tree["tree"] if c["name"] == "Media")
    assert media["count"] == 1


def test_delete_category_reassigns_to_uncategorized(client):
    cid = client.post("/api/categories", json={"name": "Music"}).get_json()["id"]
    sid = make_sub(client, name="Spotify", category_id=cid).get_json()["id"]
    assert client.delete(f"/api/categories/{cid}").status_code == 200
    # The subscription survives, moved into an 'Uncategorized' bucket.
    tree = client.get("/api/tree").get_json()
    assert tree["totals"]["all"] == 1
    names = [c["name"] for c in tree["tree"]]
    assert "Uncategorized" in names and "Music" not in names
    got = client.get(f"/api/subscriptions/{sid}").get_json()
    assert got["category_name"] == "Uncategorized"


def test_delete_uncategorized_removes_its_subs(client):
    # Create the bucket by deleting a category, then delete the bucket itself.
    cid = client.post("/api/categories", json={"name": "Music"}).get_json()["id"]
    make_sub(client, name="Spotify", category_id=cid)
    client.delete(f"/api/categories/{cid}")
    unc = next(c for c in client.get("/api/tree").get_json()["tree"] if c["name"] == "Uncategorized")
    assert client.delete(f"/api/categories/{unc['id']}").status_code == 200
    assert client.get("/api/tree").get_json()["totals"]["all"] == 0


def test_duplicate_category_rejected(client):
    assert client.post("/api/categories", json={"name": "Media"}).status_code == 400


def test_currency_setting_roundtrip(client):
    client.post("/api/settings", json={"currency": "$"})
    assert client.get("/api/settings").get_json()["currency"] == "$"
    assert client.get("/api/tree").get_json()["currency"] == "$"


def test_export_and_reset(client):
    make_sub(client)
    exp = client.get("/api/export")
    assert exp.status_code == 200 and b"Netflix" in exp.data
    client.post("/api/reset")
    tree = client.get("/api/tree").get_json()
    assert tree["totals"]["all"] == 0
    assert [c["name"] for c in tree["tree"]] == ["Media", "Productivity", "Gaming"]


def test_csrf_cross_origin_blocked(client):
    r = client.post("/api/categories", json={"name": "X"},
                    headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


# ── Payment method + necessity ────────────────────────────────────────────────
def test_payment_and_necessity_roundtrip(client):
    sid = make_sub(client, payment_method="PayPal", necessity="Critical").get_json()["id"]
    got = client.get(f"/api/subscriptions/{sid}").get_json()
    assert got["payment_method"] == "PayPal" and got["necessity"] == "Critical"


def test_necessity_defaults_to_important(client):
    sid = make_sub(client).get_json()["id"]
    assert client.get(f"/api/subscriptions/{sid}").get_json()["necessity"] == "Important"


def test_bad_payment_method_rejected(client):
    assert make_sub(client, payment_method="Bitcoin").status_code == 400


def test_bad_necessity_rejected(client):
    assert make_sub(client, necessity="Whatever").status_code == 400


# ── Price history (auto-logged) ───────────────────────────────────────────────
def test_price_history_seeded_on_create(client):
    sid = make_sub(client, amount=10).get_json()["id"]
    ph = client.get(f"/api/subscriptions/{sid}").get_json()["price_history"]
    assert len(ph) == 1 and ph[0]["amount"] == 10 and ph[0]["note"] == "created"


def test_price_change_appends_history(client):
    sid = make_sub(client, amount=10).get_json()["id"]
    client.put(f"/api/subscriptions/{sid}", json={"amount": 15})
    ph = client.get(f"/api/subscriptions/{sid}").get_json()["price_history"]
    assert [p["amount"] for p in ph] == [10, 15]


def test_unchanged_amount_does_not_log(client):
    sid = make_sub(client, amount=10).get_json()["id"]
    client.put(f"/api/subscriptions/{sid}", json={"name": "Renamed"})
    ph = client.get(f"/api/subscriptions/{sid}").get_json()["price_history"]
    assert len(ph) == 1


# ── Activation periods (seasonal on/off) ──────────────────────────────────────
def test_active_sub_has_open_period(client):
    sid = make_sub(client).get_json()["id"]
    periods = client.get(f"/api/subscriptions/{sid}").get_json()["periods"]
    assert len(periods) == 1 and periods[0]["ended_on"] is None


def test_disable_then_enable_tracks_periods(client):
    sid = make_sub(client).get_json()["id"]
    client.put(f"/api/subscriptions/{sid}", json={"active": False})
    got = client.get(f"/api/subscriptions/{sid}").get_json()
    assert got["active"] is False and got["periods"][0]["ended_on"] is not None
    assert got["next_renewal"] == "" and got["days_until_renew"] is None
    client.put(f"/api/subscriptions/{sid}", json={"active": True})
    periods = client.get(f"/api/subscriptions/{sid}").get_json()["periods"]
    assert len(periods) == 2 and periods[-1]["ended_on"] is None


def test_created_inactive_has_no_spend(client):
    sid = make_sub(client, active=False).get_json()["id"]
    got = client.get(f"/api/subscriptions/{sid}").get_json()
    assert got["periods"] == [] and got["total_spent"] == 0 and got["charges"] == 0


# ── Sidebar alphabetical ordering per category ────────────────────────────────
def test_sidebar_subs_sorted_alphabetically(client):
    make_sub(client, name="Banana", category_id=1)
    make_sub(client, name="apple", category_id=1)
    make_sub(client, name="Cherry", category_id=1)
    media = next(c for c in client.get("/api/tree").get_json()["tree"] if c["name"] == "Media")
    assert [s["name"] for s in media["subs"]] == ["apple", "Banana", "Cherry"]


# ── one-time subscriptions ─────────────────────────────────────────────────
def _one_time_cat_id(client):
    tree = client.get("/api/tree").get_json()
    return next(c["id"] for c in tree["tree"] if c["one_time"])


def make_one_time(client, **over):
    body = {"name": "NexusMods", "category_id": _one_time_cat_id(client), "amount": 10.0}
    body.update(over)
    return client.post("/api/subscriptions", json=body)


def test_one_time_starts_at_zero_and_tickers_up(client):
    sid = make_one_time(client).get_json()["id"]
    got = client.get(f"/api/subscriptions/{sid}").get_json()
    assert got["one_time"] is True
    assert got["total_spent"] == 0 and got["charges"] == 0

    # January: needed it once.
    assert client.post(f"/api/subscriptions/{sid}/usage", json={}).status_code == 200
    got = client.get(f"/api/subscriptions/{sid}").get_json()
    assert got["total_spent"] == 10.0 and got["charges"] == 1

    # June: needed it again — 10 becomes 20, not 60 for the months between.
    client.post(f"/api/subscriptions/{sid}/usage", json={})
    got = client.get(f"/api/subscriptions/{sid}").get_json()
    assert got["total_spent"] == 20.0 and got["charges"] == 2


def test_one_time_never_accrues_by_sitting_there(client):
    """The whole point: a one-time sub left alone costs nothing over time."""
    sid = make_one_time(client, start_date="2020-01-01", renew_date="2020-02-01").get_json()["id"]
    got = client.get(f"/api/subscriptions/{sid}").get_json()
    # Years have passed since that start date; a recurring sub would show many
    # charges. This one has none, because none were logged.
    assert got["total_spent"] == 0 and got["charges"] == 0
    assert got["monthly_cost"] == 0 and got["yearly_cost"] == 0
    assert got["next_renewal"] == "" and got["days_until_renew"] is None


def test_one_time_excluded_from_overview_totals(client):
    make_sub(client, amount=10.0)                       # recurring
    ot = make_one_time(client, amount=10.0).get_json()["id"]
    client.post(f"/api/subscriptions/{ot}/usage", json={})

    d = client.get("/api/overview").get_json()
    assert d["monthly_total"] == 10.0                   # one-time adds nothing
    assert d["yearly_total"] == 120.0
    assert d["one_time_spent"] == 10.0                  # reported separately
    assert d["one_time_charges"] == 1 and d["one_time_count"] == 1
    # ...and its 10.00 is not folded into the recurring spend figure.
    recurring_only = client.get(f"/api/subscriptions/1").get_json()["total_spent"]
    assert d["total_spent"] == recurring_only
    assert [c["name"] for c in d["by_category"]] == ["Media"]


def test_usage_records_the_price_at_the_time(client):
    sid = make_one_time(client, amount=10.0).get_json()["id"]
    client.post(f"/api/subscriptions/{sid}/usage", json={})
    client.put(f"/api/subscriptions/{sid}", json={"amount": 25.0})   # price rises
    client.post(f"/api/subscriptions/{sid}/usage", json={})
    got = client.get(f"/api/subscriptions/{sid}").get_json()
    # The first month stays at what was actually paid for it.
    assert got["total_spent"] == 35.0
    assert sorted(u["amount"] for u in got["usage"]) == [10.0, 25.0]


def test_usage_can_be_removed(client):
    sid = make_one_time(client).get_json()["id"]
    uid = client.post(f"/api/subscriptions/{sid}/usage", json={}).get_json()["id"]
    assert client.delete(f"/api/usage/{uid}").status_code == 200
    assert client.get(f"/api/subscriptions/{sid}").get_json()["total_spent"] == 0
    assert client.delete(f"/api/usage/{uid}").status_code == 404


def test_usage_rejected_for_recurring_subscriptions(client):
    sid = make_sub(client).get_json()["id"]
    r = client.post(f"/api/subscriptions/{sid}/usage", json={})
    assert r.status_code == 400
    assert client.post("/api/subscriptions/9999/usage", json={}).status_code == 404


def test_usage_accepts_an_explicit_date(client):
    sid = make_one_time(client).get_json()["id"]
    r = client.post(f"/api/subscriptions/{sid}/usage", json={"charged_on": "2026-01-15"})
    assert r.get_json()["charged_on"] == "2026-01-15"
    # A nonsense date falls back to today rather than being stored as-is.
    r = client.post(f"/api/subscriptions/{sid}/usage", json={"charged_on": "not-a-date"})
    assert r.get_json()["charged_on"] != "not-a-date"


def test_one_time_flag_survives_and_is_settable(client):
    r = client.post("/api/categories", json={"name": "Ad hoc", "one_time": True})
    cid = r.get_json()["id"]
    assert r.get_json()["one_time"] is True
    tree = client.get("/api/tree").get_json()
    assert next(c for c in tree["tree"] if c["id"] == cid)["one_time"] is True
    # And it can be turned back off.
    client.put(f"/api/categories/{cid}", json={"name": "Ad hoc", "one_time": False})
    tree = client.get("/api/tree").get_json()
    assert next(c for c in tree["tree"] if c["id"] == cid)["one_time"] is False


def test_export_includes_usage(client):
    sid = make_one_time(client).get_json()["id"]
    client.post(f"/api/subscriptions/{sid}/usage", json={})
    d = client.get("/api/export").get_json()
    assert len(d["usage_charges"]) == 1
