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
    # The one-time bucket is restored alongside the seeds; before it was, a
    # reset left it missing until the next restart.
    assert [c["name"] for c in tree["tree"]] == [
        "Media", "Productivity", "Gaming", "One Time Subscriptions"]


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


# ── games ──────────────────────────────────────────────────────────────────
def make_game(client, **over):
    body = {"name": "Baldur's Gate 3", "source": "Steam",
            "price": 59.99, "purchased_on": "2024-08-03"}
    body.update(over)
    return client.post("/api/games", json=body)


def test_game_create_list_update_delete(client):
    r = make_game(client)
    assert r.status_code == 201
    gid = r.get_json()["id"]

    items = client.get("/api/games").get_json()["items"]
    assert [g["name"] for g in items] == ["Baldur's Gate 3"]
    assert items[0]["price"] == 59.99 and items[0]["source"] == "Steam"

    assert client.put(f"/api/games/{gid}", json={"price": 49.99}).status_code == 200
    assert client.get("/api/games").get_json()["items"][0]["price"] == 49.99

    assert client.delete(f"/api/games/{gid}").status_code == 200
    assert client.get("/api/games").get_json()["items"] == []
    assert client.delete(f"/api/games/{gid}").status_code == 404


def test_game_validation(client):
    assert make_game(client, name="  ").status_code == 400
    assert make_game(client, source="Epic").status_code == 400      # not an allowed source
    assert make_game(client, price="free").status_code == 400
    assert make_game(client, price=-5).status_code == 400
    # A blank source is allowed — not every purchase remembers where it came from.
    assert make_game(client, source="").status_code == 201


def test_game_without_a_date_still_counts(client):
    """An old purchase whose date is forgotten must not be silently dropped."""
    make_game(client, purchased_on="", price=20.0)
    make_game(client, name="Hades", purchased_on="2023-01-05", price=10.0)
    d = client.get("/api/gaming-overview").get_json()
    assert d["games_spent"] == 30.0 and d["game_count"] == 2
    # ...it just cannot appear in the by-year split.
    assert [y["year"] for y in d["by_year"]] == ["2023"]
    assert sum(y["spent"] for y in d["by_year"]) == 10.0


def test_gaming_overview_totals(client):
    make_game(client, name="BG3", source="Steam", price=59.99, purchased_on="2024-08-03")
    make_game(client, name="Hades", source="GOG", price=19.99, purchased_on="2023-05-01")
    make_game(client, name="RDR2", source="Rockstar", price=29.99, purchased_on="2024-02-10")

    d = client.get("/api/gaming-overview").get_json()
    assert d["game_count"] == 3
    assert d["games_spent"] == 109.97
    assert d["avg_price"] == 36.66
    assert [s["name"] for s in d["by_source"]] == ["Steam", "Rockstar", "GOG"]
    assert [y["year"] for y in d["by_year"]] == ["2023", "2024"]
    assert dict((y["year"], y["spent"]) for y in d["by_year"]) == {"2023": 19.99, "2024": 89.98}
    assert [g["name"] for g in d["recent"]][0] == "BG3"          # newest first


def test_gaming_overview_includes_gaming_subscriptions(client):
    """The point of the view is what gaming costs, not what games cost."""
    tree = client.get("/api/tree").get_json()
    gaming = next(c["id"] for c in tree["tree"] if c["name"] == "Gaming")
    make_sub(client, name="Game Pass", category_id=gaming, amount=12.99,
             start_date="2026-01-01", renew_date="2030-01-01")
    make_game(client, price=59.99)

    d = client.get("/api/gaming-overview").get_json()
    assert d["sub_count"] == 1 and d["sub_monthly"] == 12.99
    assert d["sub_spent"] > 0
    assert d["total_spent"] == round(d["games_spent"] + d["sub_spent"], 2)
    # A subscription in another category stays out of it.
    make_sub(client, name="Netflix", category_id=1)
    assert client.get("/api/gaming-overview").get_json()["sub_count"] == 1


def test_gaming_overview_empty(client):
    d = client.get("/api/gaming-overview").get_json()
    assert d["game_count"] == 0 and d["games_spent"] == 0 and d["avg_price"] == 0
    assert d["by_source"] == [] and d["by_year"] == []


def test_reset_clears_games_and_restores_one_time_category(client):
    make_game(client)
    client.post("/api/reset", json={})
    assert client.get("/api/games").get_json()["items"] == []
    # Re-seeding must put the one-time bucket back, not wait for a restart.
    names = [c["name"] for c in client.get("/api/tree").get_json()["tree"]]
    assert "One Time Subscriptions" in names


def test_export_includes_games(client):
    make_game(client)
    assert len(client.get("/api/export").get_json()["games"]) == 1


# ── browsing the game library ──────────────────────────────────────────────
def test_tree_groups_games_by_source(client):
    make_game(client, name="BG3", source="Steam", price=59.99)
    make_game(client, name="Hades", source="GOG", price=19.99)
    make_game(client, name="Extra", source="Steam", price=10.0)
    make_game(client, name="Mystery", source="", price=5.0)

    tree = client.get("/api/tree").get_json()
    assert tree["totals"]["games"] == 4
    by_name = {s["name"]: s for s in tree["game_sources"]}
    assert by_name["Steam"] == {"name": "Steam", "count": 2, "spent": 69.99}
    assert by_name["GOG"]["count"] == 1
    # A game with no store recorded is grouped rather than dropped.
    assert by_name["Unknown"]["count"] == 1
    # Ordered by spend, biggest first.
    assert [s["name"] for s in tree["game_sources"]][0] == "Steam"


def test_games_by_source(client):
    make_game(client, name="BG3", source="Steam", price=59.99)
    make_game(client, name="Hades", source="GOG", price=19.99)
    make_game(client, name="Mystery", source="", price=5.0)

    d = client.get("/api/games/by-source/Steam").get_json()
    assert [g["name"] for g in d["items"]] == ["BG3"]
    assert d["spent"] == 59.99

    # "Unknown" is the bucket for games with no store recorded.
    d = client.get("/api/games/by-source/Unknown").get_json()
    assert [g["name"] for g in d["items"]] == ["Mystery"]

    d = client.get("/api/games/by-source/Blizzard").get_json()
    assert d["items"] == [] and d["spent"] == 0


def test_games_list_reports_total(client):
    make_game(client, price=59.99)
    make_game(client, name="Hades", price=19.99)
    d = client.get("/api/games").get_json()
    assert d["spent"] == 79.98 and len(d["items"]) == 2


def test_tree_has_no_game_sources_when_empty(client):
    assert client.get("/api/tree").get_json()["game_sources"] == []


# ── household split ────────────────────────────────────────────────────────
def _make_household_cat(client, name="Monthly Home Expenses"):
    return client.post("/api/categories", json={"name": name, "household": True}).get_json()["id"]


def test_nothing_changes_until_a_category_is_flagged(client):
    """The split must be invisible until it is asked for."""
    make_sub(client, amount=10.0)
    d = client.get("/api/overview").get_json()
    assert d["household_count"] == 0
    assert d["household_monthly"] == 0 and d["household_spent"] == 0
    assert d["combined_monthly"] == d["monthly_total"] == 10.0


def test_household_totals_are_separate_from_subscriptions(client):
    hid = _make_household_cat(client)
    make_sub(client, name="Netflix", category_id=1, amount=10.0)
    make_sub(client, name="Mortgage", category_id=hid, amount=1200.0)
    make_sub(client, name="Energy", category_id=hid, amount=180.0)

    d = client.get("/api/overview").get_json()
    # The subscription headline is untouched by a 1380/mo household.
    assert d["monthly_total"] == 10.0
    assert d["yearly_total"] == 120.0
    assert d["household_monthly"] == 1380.0
    assert d["household_yearly"] == 16560.0
    assert d["household_count"] == 2
    assert d["combined_monthly"] == 1390.0
    assert d["combined_yearly"] == 16680.0


def test_household_spend_is_reported_apart(client):
    hid = _make_household_cat(client)
    make_sub(client, name="Netflix", category_id=1, amount=10.0,
             start_date="2026-01-01", renew_date="2030-01-01")
    make_sub(client, name="Energy", category_id=hid, amount=180.0,
             start_date="2026-01-01", renew_date="2030-01-01")
    d = client.get("/api/overview").get_json()
    assert d["total_spent"] > 0 and d["household_spent"] > 0
    # Household spend must not leak into the subscription figure.
    assert d["household_spent"] == round(d["total_spent"] * 18, 2)


def test_by_category_marks_household_entries(client):
    hid = _make_household_cat(client)
    make_sub(client, name="Netflix", category_id=1, amount=10.0)
    make_sub(client, name="Energy", category_id=hid, amount=180.0)
    cats = {c["name"]: c for c in client.get("/api/overview").get_json()["by_category"]}
    assert cats["Media"]["household"] is False
    assert cats["Monthly Home Expenses"]["household"] is True


def test_household_flag_round_trips(client):
    hid = _make_household_cat(client, "Bills")
    tree = client.get("/api/tree").get_json()
    cat = next(c for c in tree["tree"] if c["id"] == hid)
    assert cat["household"] is True
    client.put(f"/api/categories/{hid}", json={"name": "Bills", "household": False})
    tree = client.get("/api/tree").get_json()
    assert next(c for c in tree["tree"] if c["id"] == hid)["household"] is False


def test_tree_totals_exclude_household_from_the_subscription_figure(client):
    hid = _make_household_cat(client)
    make_sub(client, name="Netflix", category_id=1, amount=10.0)
    make_sub(client, name="Energy", category_id=hid, amount=180.0)
    t = client.get("/api/tree").get_json()["totals"]
    assert t["monthly_total"] == 10.0
    assert t["household_monthly"] == 180.0


# ── the three overviews ────────────────────────────────────────────────────
def test_household_overview(client):
    hid = _make_household_cat(client)
    make_sub(client, name="Netflix", category_id=1, amount=10.0)
    make_sub(client, name="Mortgage", category_id=hid, amount=1240.0,
             start_date="2026-01-01", renew_date="2030-01-01")
    make_sub(client, name="Energy", category_id=hid, amount=186.0,
             start_date="2026-01-01", renew_date="2030-01-01")

    d = client.get("/api/household-overview").get_json()
    assert d["active_count"] == 2 and d["total_count"] == 2
    assert d["monthly_total"] == 1426.0
    assert d["yearly_total"] == 17112.0
    # Ranked biggest first, and the subscription is nowhere in it.
    assert [b["name"] for b in d["bills"]] == ["Mortgage", "Energy"]
    assert d["biggest"] == "Mortgage"
    assert all(b["name"] != "Netflix" for b in d["bills"])


def test_household_overview_empty_when_nothing_flagged(client):
    make_sub(client, amount=10.0)
    d = client.get("/api/household-overview").get_json()
    assert d["total_count"] == 0 and d["monthly_total"] == 0
    assert d["bills"] == [] and d["biggest"] == ""


def test_household_overview_keeps_paused_bills_after_active_ones(client):
    hid = _make_household_cat(client)
    make_sub(client, name="Energy", category_id=hid, amount=186.0)
    r = make_sub(client, name="Old contract", category_id=hid, amount=999.0)
    client.put(f"/api/subscriptions/{r.get_json()['id']}", json={"active": False})
    d = client.get("/api/household-overview").get_json()
    assert [b["name"] for b in d["bills"]] == ["Energy", "Old contract"]
    # A paused bill must not inflate the monthly figure despite its size.
    assert d["monthly_total"] == 186.0
    assert d["active_count"] == 1 and d["total_count"] == 2


def test_tree_totals_feed_the_new_nav(client):
    hid = _make_household_cat(client)
    make_sub(client, name="Netflix", category_id=1, amount=10.0)
    make_sub(client, name="Energy", category_id=hid, amount=186.0)
    make_game(client)
    t = client.get("/api/tree").get_json()["totals"]
    assert t["subs"] == 1 and t["household_count"] == 1 and t["games"] == 1


def test_upcoming_spans_subscriptions_and_household(client):
    """Renewals are the one view that deliberately covers everything."""
    hid = _make_household_cat(client)
    make_sub(client, name="Netflix", category_id=1, amount=10.0,
             start_date="2026-01-01", renew_date="2026-09-01")
    make_sub(client, name="Energy", category_id=hid, amount=186.0,
             start_date="2026-01-01", renew_date="2026-09-01")
    names = [s["name"] for s in client.get("/api/subscriptions?scope=upcoming").get_json()["items"]]
    assert set(names) == {"Netflix", "Energy"}
    assert client.get("/api/tree").get_json()["totals"]["upcoming"] == 2


def test_overview_upcoming_marks_household(client):
    hid = _make_household_cat(client)
    make_sub(client, name="Energy", category_id=hid, amount=186.0,
             start_date="2026-01-01", renew_date="2026-09-01")
    up = client.get("/api/overview").get_json()["upcoming"]
    assert [u["household"] for u in up] == [True]
