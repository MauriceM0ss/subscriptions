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
    assert names == ["Media", "Productivity", "Gaming"]


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
