"""Subscriptions — a self-hosted personal subscription tracker.

Add the services you pay for, group them by category in the sidebar, and see
what they cost per month / per year and how much you've spent so far.

Domain logic is split across focused modules:

    config         env-derived settings + shared primitives (now_iso, today)
    database       SQLite connection, schema/migrations, key/value settings
    subscriptions  cost & renewal derivation (monthly/yearly/total-spent)
"""
import io
import json
import hmac
import time
import logging
import sqlite3
from urllib.parse import urlparse
from pathlib import Path

from flask import Flask, render_template, request, jsonify, send_file, Response

import config
import database
import subscriptions

# ── Back-compat / test facade: expose the most-used names at module scope. ─────
from config import now_iso
from database import db, init_db, get_setting, set_setting, get_currency

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("subscriptions")

_STATIC = Path(__file__).parent / "static"
_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


@app.route("/healthz")
def healthz():
    """Liveness/readiness probe for the container healthcheck. No auth required."""
    try:
        with db() as conn:
            conn.execute("SELECT 1")
        return jsonify({"status": "ok"})
    except Exception as e:                                    # noqa: BLE001
        log.error("Healthcheck DB error: %s", e)
        return jsonify({"status": "error"}), 503


@app.before_request
def _security_gate():
    """Optional Basic auth + a lightweight CSRF (same-origin) check.

    Auth is off unless AUTH_USER/AUTH_PASSWORD are set, preserving the open
    localhost default. The CSRF check rejects state-changing requests whose
    Origin/Referer is a different site — a browser always sends one on a
    cross-site request, while non-browser clients (curl) send neither.
    """
    if request.path == "/healthz":       # probe must work without creds/origin
        return None
    request._started = time.monotonic()

    if config.AUTH_USER and config.AUTH_PASSWORD:
        auth = request.authorization
        ok = (auth and auth.type == "basic"
              and hmac.compare_digest(auth.username or "", config.AUTH_USER)
              and hmac.compare_digest(auth.password or "", config.AUTH_PASSWORD))
        if not ok:
            return Response("Authentication required.", 401,
                            {"WWW-Authenticate": 'Basic realm="Subscriptions"'})

    if request.method in _MUTATING:
        source = request.headers.get("Origin") or request.headers.get("Referer")
        if source and urlparse(source).netloc != request.host:
            return jsonify({"error": "Cross-origin request blocked."}), 403


@app.after_request
def _access_log(resp):
    if request.path == "/healthz":
        return resp
    started = getattr(request, "_started", None)
    ms = f"{(time.monotonic() - started) * 1000:.0f}ms" if started else "-"
    log.info("%s %s -> %s (%s)", request.method, request.full_path.rstrip("?"),
             resp.status_code, ms)
    return resp


@app.errorhandler(Exception)
def _log_unhandled(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    log.exception("Unhandled error on %s %s", request.method, request.path)
    return jsonify({"error": "Internal server error."}), 500


# ── Pages ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("app.html")


@app.route("/manifest.webmanifest")
def web_manifest():
    return send_file(_STATIC / "manifest.webmanifest", mimetype="application/manifest+json")


@app.route("/sw.js")
def service_worker():
    resp = send_file(_STATIC / "sw.js", mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# ── Helpers ───────────────────────────────────────────────────────────────────
def _periods_prices(conn):
    """All activation periods and price points, grouped by subscription id and
    parsed into the (date, …) tuples that subscriptions.enrich expects."""
    periods, prices = {}, {}
    for r in conn.execute("SELECT sub_id, started_on, ended_on FROM activation_periods"):
        periods.setdefault(r["sub_id"], []).append(
            (subscriptions.parse_date(r["started_on"]), subscriptions.parse_date(r["ended_on"])))
    for r in conn.execute("SELECT sub_id, amount, changed_on FROM price_history ORDER BY changed_on"):
        prices.setdefault(r["sub_id"], []).append(
            (subscriptions.parse_date(r["changed_on"]), float(r["amount"])))
    usages = {}
    for r in conn.execute("SELECT sub_id, amount FROM usage_charges ORDER BY charged_on, id"):
        usages.setdefault(r["sub_id"], []).append(float(r["amount"]))
    return periods, prices, usages


def _all_enriched(conn, today, where="", params=()):
    rows = conn.execute(
        "SELECT sub.*, c.name AS category_name, c.one_time AS one_time, "
        "c.household AS household FROM subscriptions sub "
        "LEFT JOIN categories c ON c.id = sub.category_id "
        + where + " ORDER BY sub.sort_order, sub.name COLLATE NOCASE", params).fetchall()
    periods, prices, usages = _periods_prices(conn)
    return [subscriptions.enrich(r, today, periods.get(r["id"], []), prices.get(r["id"], []),
                                 usages.get(r["id"], []))
            for r in rows]


def _clean_payload(d, partial):
    """Validate an incoming subscription body. Returns (cleaned, error)."""
    out = {}
    if "name" in d or not partial:
        name = (d.get("name") or "").strip()
        if not name:
            return None, "Name is required."
        out["name"] = name
    if "category_id" in d or not partial:
        try:
            out["category_id"] = int(d["category_id"])
        except (KeyError, TypeError, ValueError):
            return None, "Pick a category."
    if "billing_cycle" in d or not partial:
        cyc = (d.get("billing_cycle") or "monthly").strip().lower()
        if cyc not in config.BILLING_CYCLES:
            return None, "Billing cycle must be monthly or yearly."
        out["billing_cycle"] = cyc
    if "amount" in d or not partial:
        try:
            amt = float(d.get("amount") or 0)
        except (TypeError, ValueError):
            return None, "Amount must be a number."
        if amt < 0:
            return None, "Amount can't be negative."
        out["amount"] = round(amt, 2)
    for field in ("start_date", "renew_date"):
        if field in d:
            v = (d.get(field) or "").strip()
            if v and subscriptions.parse_date(v) is None:
                return None, f"Invalid {field.replace('_', ' ')} (use YYYY-MM-DD)."
            out[field] = v
    if "payment_method" in d or not partial:
        pm = (d.get("payment_method") or "").strip()
        if pm and pm not in config.PAYMENT_METHODS:
            return None, "Unknown payment method."
        out["payment_method"] = pm
    if "necessity" in d or not partial:
        nec = (d.get("necessity") or "").strip() or config.DEFAULT_NECESSITY
        if nec not in config.NECESSITIES:
            return None, "Unknown necessity."
        out["necessity"] = nec
    if "active" in d:
        out["active"] = 1 if d.get("active") else 0
    if "notes" in d:
        out["notes"] = (d.get("notes") or "").strip()
    return out, None


# ── API: sidebar tree ─────────────────────────────────────────────────────────
@app.route("/api/tree")
def api_tree():
    today = config.today()
    with db() as conn:
        cats = conn.execute("SELECT * FROM categories ORDER BY sort_order, name").fetchall()
        subs = _all_enriched(conn, today)
        game_rows = conn.execute("SELECT source, price FROM games").fetchall()
        game_count = len(game_rows)

    by_cat = {}
    for s in subs:
        by_cat.setdefault(s["category_id"], []).append({
            "id": s["id"], "name": s["name"],
            "category_id": s["category_id"], "active": s["active"],
            "one_time": s["one_time"], "household": s["household"],
            "total_spent": s["total_spent"],
            "monthly_cost": s["monthly_cost"], "days_until_renew": s["days_until_renew"],
        })
    tree = []
    for c in cats:
        csubs = sorted(by_cat.get(c["id"], []), key=lambda x: (x["name"] or "").lower())
        one_time = bool(c["one_time"])
        tree.append({
            "id": c["id"], "name": c["name"], "subs": csubs, "count": len(csubs),
            "one_time": one_time, "household": bool(c["household"]),
            # A one-time category has no monthly commitment; show what it has
            # actually cost instead, so the row is not a permanent 0.00/mo.
            "monthly": 0 if one_time else round(
                sum(x["monthly_cost"] for x in csubs if x["active"]), 2),
            "spent": round(sum(x["total_spent"] for x in csubs), 2) if one_time else None,
        })

    # One-time subscriptions are excluded here for the same reason as in the
    # overview: they are not a recurring commitment being carried.
    active = [s for s in subs if s["active"] and not s["one_time"]]
    upcoming = [s for s in subs if s["active"] and not s["one_time"]
                and s["days_until_renew"] is not None
                and 0 <= s["days_until_renew"] <= config.UPCOMING_DAYS]
    # Games grouped by where they were bought, so the sidebar can offer the
    # same drill-down for a store that it does for a category.
    by_source = {}
    for r in game_rows:
        key = r["source"] or "Unknown"
        agg = by_source.setdefault(key, {"name": key, "count": 0, "spent": 0.0})
        agg["count"] += 1
        agg["spent"] += float(r["price"] or 0)
    game_sources = sorted(
        [{**v, "spent": round(v["spent"], 2)} for v in by_source.values()],
        key=lambda x: -x["spent"])

    return jsonify({
        "tree": tree,
        "game_sources": game_sources,
        "currency": get_currency(),
        "totals": {
            "all": len(subs),
            "active": len(active),
            "subs": len([s for s in subs if not s["household"] and not s["one_time"]]),
            "household_count": len([s for s in subs if s["household"]]),
            "upcoming": len(upcoming),
            "monthly_total": round(sum(s["monthly_cost"] for s in active
                                       if not s["household"]), 2),
            "yearly_total": round(sum(s["yearly_cost"] for s in active
                                      if not s["household"]), 2),
            "household_monthly": round(sum(s["monthly_cost"] for s in active
                                           if s["household"]), 2),
            "games": game_count,
        },
    })


# ── API: overview dashboard ───────────────────────────────────────────────────
@app.route("/api/overview")
def api_overview():
    today = config.today()
    with db() as conn:
        subs = _all_enriched(conn, today)
    # One-time subscriptions are not recurring commitments, so they stay out of
    # every aggregate here and are reported on their own instead.
    recurring = [s for s in subs if not s["one_time"]]
    one_time = [s for s in subs if s["one_time"]]
    active = [s for s in recurring if s["active"]]

    # Household bills dwarf discretionary subscriptions, so they are totalled
    # separately rather than swallowing them in one figure. With nothing
    # flagged household, every household_* value is 0 and the subscription
    # figures are exactly what they always were.
    house_active = [s for s in active if s["household"]]
    subs_active = [s for s in active if not s["household"]]

    by_cat = {}
    for s in active:
        key = s.get("category_name") or "Uncategorized"
        entry = by_cat.setdefault(key, {"name": key, "monthly": 0.0,
                                        "household": bool(s["household"])})
        entry["monthly"] = round(entry["monthly"] + s["monthly_cost"], 2)

    upcoming = sorted(
        [s for s in active if s["days_until_renew"] is not None],
        key=lambda s: s["days_until_renew"])[:12]
    return jsonify({
        "currency": get_currency(),
        # monthly_total / yearly_total / total_spent stay subscription-only, so
        # the headline keeps answering the question it always answered.
        "monthly_total": round(sum(s["monthly_cost"] for s in subs_active), 2),
        "yearly_total": round(sum(s["yearly_cost"] for s in subs_active), 2),
        "total_spent": round(sum(s["total_spent"] for s in recurring
                                 if not s["household"]), 2),
        "household_monthly": round(sum(s["monthly_cost"] for s in house_active), 2),
        "household_yearly": round(sum(s["yearly_cost"] for s in house_active), 2),
        "household_spent": round(sum(s["total_spent"] for s in recurring
                                     if s["household"]), 2),
        "household_count": len(house_active),
        "combined_monthly": round(sum(s["monthly_cost"] for s in active), 2),
        "combined_yearly": round(sum(s["yearly_cost"] for s in active), 2),
        "one_time_spent": round(sum(s["total_spent"] for s in one_time), 2),
        "one_time_charges": sum(s["charges"] for s in one_time),
        "one_time_count": len(one_time),
        "active_count": len(active),
        "total_count": len(subs),
        "by_category": sorted(by_cat.values(), key=lambda x: -x["monthly"]),
        "upcoming": [{
            "id": s["id"], "name": s["name"],
            "category_name": s.get("category_name"), "household": s["household"],
            "amount": s["amount"], "billing_cycle": s["billing_cycle"],
            "next_renewal": s["next_renewal"], "days_until_renew": s["days_until_renew"],
        } for s in upcoming],
    })


# ── API: subscriptions ────────────────────────────────────────────────────────
@app.route("/api/subscriptions")
def api_subs_list():
    scope = request.args.get("scope", "all")
    sid = request.args.get("id")
    today = config.today()
    where, params = "", ()
    if scope == "category":
        where, params = "WHERE sub.category_id = ?", (sid,)
    with db() as conn:
        items = _all_enriched(conn, today, where, params)
    if scope == "upcoming":
        items = [s for s in items if s["days_until_renew"] is not None
                 and 0 <= s["days_until_renew"] <= config.UPCOMING_DAYS]
        items.sort(key=lambda s: s["days_until_renew"])
    return jsonify({"items": items, "currency": get_currency()})


@app.route("/api/subscriptions/<int:sid>")
def api_sub_get(sid):
    with db() as conn:
        r = conn.execute(
            "SELECT sub.*, c.name AS category_name, c.one_time AS one_time, "
            "c.household AS household FROM subscriptions sub "
            "LEFT JOIN categories c ON c.id = sub.category_id WHERE sub.id=?", (sid,)).fetchone()
        if not r:
            return jsonify({"error": "Subscription not found."}), 404
        usage_rows = conn.execute(
            "SELECT id, charged_on, amount, note FROM usage_charges "
            "WHERE sub_id=? ORDER BY charged_on DESC, id DESC", (sid,)).fetchall()
        price_rows = conn.execute(
            "SELECT amount, billing_cycle, changed_on, note FROM price_history "
            "WHERE sub_id=? ORDER BY changed_on, id", (sid,)).fetchall()
        period_rows = conn.execute(
            "SELECT started_on, ended_on FROM activation_periods "
            "WHERE sub_id=? ORDER BY started_on, id", (sid,)).fetchall()
    periods = [(subscriptions.parse_date(p["started_on"]), subscriptions.parse_date(p["ended_on"]))
               for p in period_rows]
    prices = [(subscriptions.parse_date(p["changed_on"]), float(p["amount"])) for p in price_rows]
    out = subscriptions.enrich(r, config.today(), periods, prices,
                               [row["amount"] for row in usage_rows])
    out["currency"] = get_currency()
    out["price_history"] = [dict(p) for p in price_rows]
    out["periods"] = [dict(p) for p in period_rows]
    out["usage"] = [dict(u) for u in usage_rows]
    return jsonify(out)


# ── API: one-time usage ticker ────────────────────────────────────────────────
@app.route("/api/subscriptions/<int:sid>/usage", methods=["POST"])
def api_usage_log(sid):
    """Log one month of use, at today's date and the price in effect now.

    The amount is copied rather than referenced, so editing the price later
    leaves everything already logged untouched."""
    payload = request.get_json(silent=True) or {}
    with db() as conn:
        row = conn.execute(
            "SELECT sub.amount, c.one_time FROM subscriptions sub "
            "LEFT JOIN categories c ON c.id = sub.category_id WHERE sub.id=?", (sid,)).fetchone()
        if not row:
            return jsonify({"error": "Subscription not found."}), 404
        if not row["one_time"]:
            return jsonify({"error": "Only one-time subscriptions log months of use."}), 400
        charged_on = str(payload.get("charged_on") or "")[:10]
        if not subscriptions.parse_date(charged_on):
            charged_on = config.today().isoformat()
        cur = conn.execute(
            "INSERT INTO usage_charges (sub_id, charged_on, amount, note) VALUES (?, ?, ?, ?)",
            (sid, charged_on, float(row["amount"] or 0), str(payload.get("note") or "")[:200]))
        new_id = cur.lastrowid
    return jsonify({"ok": True, "id": new_id, "charged_on": charged_on})


@app.route("/api/usage/<int:uid>", methods=["DELETE"])
def api_usage_delete(uid):
    with db() as conn:
        cur = conn.execute("DELETE FROM usage_charges WHERE id=?", (uid,))
        if not cur.rowcount:
            return jsonify({"error": "Entry not found."}), 404
    return jsonify({"ok": True})


@app.route("/api/subscriptions", methods=["POST"])
def api_sub_create():
    d = request.get_json(force=True)
    clean, err = _clean_payload(d, partial=False)
    if err:
        return jsonify({"error": err}), 400
    with db() as conn:
        if not conn.execute("SELECT 1 FROM categories WHERE id=?", (clean["category_id"],)).fetchone():
            return jsonify({"error": "That category no longer exists."}), 400
        # Default the renewal date to start + one cycle when it's left blank.
        if not clean.get("renew_date") and clean.get("start_date"):
            sd = subscriptions.parse_date(clean["start_date"])
            if sd:
                clean["renew_date"] = subscriptions.add_cycle(sd, clean["billing_cycle"]).isoformat()
        cur = conn.execute(
            "INSERT INTO subscriptions (category_id, name, billing_cycle, amount, "
            "start_date, renew_date, active, payment_method, necessity, notes, "
            "created_at, sort_order) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "        (SELECT COALESCE(MAX(sort_order),0)+1 FROM subscriptions))",
            (clean["category_id"], clean["name"],
             clean["billing_cycle"], clean["amount"], clean.get("start_date", ""),
             clean.get("renew_date", ""), clean.get("active", 1),
             clean.get("payment_method", ""), clean.get("necessity", config.DEFAULT_NECESSITY),
             clean.get("notes", ""), now_iso()))
        sid = cur.lastrowid
        # Seed the starting price and (if active) open the first activation period.
        anchor = clean.get("start_date") or config.today().isoformat()
        conn.execute(
            "INSERT INTO price_history (sub_id, amount, billing_cycle, changed_on, note) "
            "VALUES (?, ?, ?, ?, 'created')",
            (sid, clean["amount"], clean["billing_cycle"], anchor))
        if clean.get("active", 1):
            conn.execute(
                "INSERT INTO activation_periods (sub_id, started_on, ended_on) VALUES (?, ?, NULL)",
                (sid, anchor))
    return jsonify({"id": sid}), 201


@app.route("/api/subscriptions/<int:sid>", methods=["PUT"])
def api_sub_update(sid):
    d = request.get_json(force=True)
    clean, err = _clean_payload(d, partial=True)
    if err:
        return jsonify({"error": err}), 400
    if not clean:
        return jsonify({"error": "Nothing to update."}), 400
    with db() as conn:
        old = conn.execute(
            "SELECT amount, billing_cycle, active FROM subscriptions WHERE id=?", (sid,)).fetchone()
        if not old:
            return jsonify({"error": "Subscription not found."}), 404
        if "category_id" in clean and not conn.execute(
                "SELECT 1 FROM categories WHERE id=?", (clean["category_id"],)).fetchone():
            return jsonify({"error": "That category no longer exists."}), 400
        cols = ", ".join(f"{k}=?" for k in clean)
        conn.execute(f"UPDATE subscriptions SET {cols} WHERE id=?", list(clean.values()) + [sid])

        today_iso = config.today().isoformat()
        # Auto-log a price change whenever the amount or the billing cycle moves.
        new_amt = clean.get("amount", old["amount"])
        new_cyc = clean.get("billing_cycle", old["billing_cycle"])
        if new_amt != old["amount"] or new_cyc != old["billing_cycle"]:
            conn.execute(
                "INSERT INTO price_history (sub_id, amount, billing_cycle, changed_on, note) "
                "VALUES (?, ?, ?, ?, '')", (sid, new_amt, new_cyc, today_iso))
        # Toggling active opens a new on-period or closes the current one.
        if "active" in clean and clean["active"] != old["active"]:
            if clean["active"]:
                open_period = conn.execute(
                    "SELECT 1 FROM activation_periods WHERE sub_id=? AND ended_on IS NULL",
                    (sid,)).fetchone()
                if not open_period:
                    conn.execute(
                        "INSERT INTO activation_periods (sub_id, started_on, ended_on) "
                        "VALUES (?, ?, NULL)", (sid, today_iso))
            else:
                conn.execute(
                    "UPDATE activation_periods SET ended_on=? "
                    "WHERE sub_id=? AND ended_on IS NULL", (today_iso, sid))
    return jsonify({"ok": True})


@app.route("/api/subscriptions/<int:sid>", methods=["DELETE"])
def api_sub_delete(sid):
    with db() as conn:
        conn.execute("DELETE FROM subscriptions WHERE id=?", (sid,))
    return jsonify({"ok": True})


# ── API: categories ───────────────────────────────────────────────────────────
@app.route("/api/categories", methods=["POST"])
def api_category_create():
    payload = request.get_json(force=True)
    name = (payload.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name required."}), 400
    one_time = 1 if payload.get("one_time") else 0
    household = 1 if payload.get("household") else 0
    with db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO categories (name, sort_order, one_time, household) VALUES "
                "(?, (SELECT COALESCE(MAX(sort_order),0)+1 FROM categories), ?, ?)",
                (name, one_time, household))
        except sqlite3.IntegrityError:
            return jsonify({"error": "That category already exists."}), 400
    return jsonify({"id": cur.lastrowid, "name": name,
                    "one_time": bool(one_time), "household": bool(household)})


@app.route("/api/categories/<int:cid>", methods=["PUT"])
def api_category_update(cid):
    payload = request.get_json(force=True)
    name = (payload.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name required."}), 400
    with db() as conn:
        try:
            conn.execute("UPDATE categories SET name=? WHERE id=?", (name, cid))
        except sqlite3.IntegrityError:
            return jsonify({"error": "That category already exists."}), 400
        if "one_time" in payload:
            conn.execute("UPDATE categories SET one_time=? WHERE id=?",
                         (1 if payload["one_time"] else 0, cid))
        if "household" in payload:
            conn.execute("UPDATE categories SET household=? WHERE id=?",
                         (1 if payload["household"] else 0, cid))
    return jsonify({"ok": True})


UNCATEGORIZED = "Uncategorized"


def _uncategorized_id(conn):
    """Id of the 'Uncategorized' bucket, created on demand."""
    r = conn.execute("SELECT id FROM categories WHERE name=?", (UNCATEGORIZED,)).fetchone()
    if r:
        return r["id"]
    cur = conn.execute(
        "INSERT INTO categories (name, sort_order) VALUES "
        "(?, (SELECT COALESCE(MAX(sort_order),0)+1 FROM categories))", (UNCATEGORIZED,))
    return cur.lastrowid


@app.route("/api/categories/<int:cid>", methods=["DELETE"])
def api_category_delete(cid):
    with db() as conn:
        row = conn.execute("SELECT name FROM categories WHERE id=?", (cid,)).fetchone()
        if not row:
            return jsonify({"ok": True})
        # Move this category's subscriptions into the Uncategorized bucket rather
        # than deleting them with the category. (Deleting the bucket itself just
        # removes it and its subscriptions — there's nowhere else to move them.)
        if row["name"] != UNCATEGORIZED:
            unc_id = _uncategorized_id(conn)
            conn.execute("UPDATE subscriptions SET category_id=? WHERE category_id=?", (unc_id, cid))
        conn.execute("DELETE FROM categories WHERE id=?", (cid,))
    return jsonify({"ok": True})


# ── API: games ────────────────────────────────────────────────────────────────
def _clean_game(payload, partial=False):
    """Validate and normalise a game payload. Returns (fields, error)."""
    out = {}
    if "name" in payload or not partial:
        name = str(payload.get("name") or "").strip()
        if not name:
            return None, "Name required."
        out["name"] = name[:200]
    if "source" in payload or not partial:
        source = str(payload.get("source") or "").strip()
        if source and source not in config.GAME_SOURCES:
            return None, "Unknown source."
        out["source"] = source
    if "price" in payload or not partial:
        try:
            price = round(float(payload.get("price") or 0), 2)
        except (TypeError, ValueError):
            return None, "Price must be a number."
        if price < 0:
            return None, "Price cannot be negative."
        out["price"] = price
    if "purchased_on" in payload or not partial:
        raw = str(payload.get("purchased_on") or "")[:10]
        # A blank date is allowed — an old purchase whose date is long forgotten
        # should still be countable. It simply drops out of the by-year split.
        out["purchased_on"] = raw if subscriptions.parse_date(raw) else ""
    if "notes" in payload or not partial:
        out["notes"] = str(payload.get("notes") or "").strip()[:500]
    return out, None


@app.route("/api/games")
def api_games_list():
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM games ORDER BY "
            "CASE WHEN purchased_on = '' THEN 1 ELSE 0 END, purchased_on DESC, id DESC").fetchall()
    items = [dict(r) for r in rows]
    return jsonify({"items": items, "currency": get_currency(),
                    "sources": list(config.GAME_SOURCES),
                    "spent": round(sum(float(i["price"] or 0) for i in items), 2)})


@app.route("/api/games/by-source/<path:source>")
def api_games_by_source(source):
    """Games from one store. 'Unknown' collects those with no store recorded."""
    where, params = ("source = ?", (source,)) if source != "Unknown" else ("source = ''", ())
    with db() as conn:
        rows = conn.execute(
            f"SELECT * FROM games WHERE {where} ORDER BY "
            "CASE WHEN purchased_on = '' THEN 1 ELSE 0 END, purchased_on DESC, id DESC",
            params).fetchall()
    items = [dict(r) for r in rows]
    return jsonify({"items": items, "currency": get_currency(),
                    "source": source,
                    "spent": round(sum(float(i["price"] or 0) for i in items), 2)})


@app.route("/api/games", methods=["POST"])
def api_game_create():
    fields, err = _clean_game(request.get_json(force=True) or {})
    if err:
        return jsonify({"error": err}), 400
    fields["created_at"] = config.now_iso()
    cols = ", ".join(fields)
    with db() as conn:
        cur = conn.execute(f"INSERT INTO games ({cols}) VALUES ({', '.join('?' * len(fields))})",
                           tuple(fields.values()))
    return jsonify({"id": cur.lastrowid, **fields}), 201


@app.route("/api/games/<int:gid>", methods=["PUT"])
def api_game_update(gid):
    fields, err = _clean_game(request.get_json(force=True) or {}, partial=True)
    if err:
        return jsonify({"error": err}), 400
    if not fields:
        return jsonify({"error": "Nothing to update."}), 400
    with db() as conn:
        cur = conn.execute(
            f"UPDATE games SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
            (*fields.values(), gid))
        if not cur.rowcount:
            return jsonify({"error": "Game not found."}), 404
    return jsonify({"ok": True})


@app.route("/api/games/<int:gid>", methods=["DELETE"])
def api_game_delete(gid):
    with db() as conn:
        cur = conn.execute("DELETE FROM games WHERE id=?", (gid,))
        if not cur.rowcount:
            return jsonify({"error": "Game not found."}), 404
    return jsonify({"ok": True})


@app.route("/api/gaming-overview")
def api_gaming_overview():
    """Everything gaming costs: one-off game purchases plus the subscriptions
    filed under the gaming category, which are otherwise reported separately."""
    today = config.today()
    with db() as conn:
        games = [dict(r) for r in conn.execute("SELECT * FROM games").fetchall()]
        subs = [s for s in _all_enriched(conn, today)
                if (s.get("category_name") or "") == config.GAMING_CATEGORY]

    spent = round(sum(float(g["price"]) for g in games), 2)

    by_source, by_year = {}, {}
    for g in games:
        src = g["source"] or "Unknown"
        by_source[src] = round(by_source.get(src, 0) + float(g["price"]), 2)
        year = (g["purchased_on"] or "")[:4]
        if year:
            by_year[year] = round(by_year.get(year, 0) + float(g["price"]), 2)

    recurring = [s for s in subs if not s["one_time"]]
    sub_monthly = round(sum(s["monthly_cost"] for s in recurring if s["active"]), 2)
    sub_spent = round(sum(s["total_spent"] for s in subs), 2)

    recent = sorted(games, key=lambda g: (g["purchased_on"] or "", g["id"]), reverse=True)[:10]
    return jsonify({
        "currency": get_currency(),
        "game_count": len(games),
        "games_spent": spent,
        "avg_price": round(spent / len(games), 2) if games else 0,
        "sub_monthly": sub_monthly,
        "sub_spent": sub_spent,
        "sub_count": len(subs),
        # The headline: one-off purchases and gaming subscriptions together.
        "total_spent": round(spent + sub_spent, 2),
        "by_source": sorted([{"name": k, "spent": v} for k, v in by_source.items()],
                            key=lambda x: -x["spent"]),
        "by_year": sorted([{"year": k, "spent": v} for k, v in by_year.items()],
                          key=lambda x: x["year"]),
        "recent": recent,
        "subs": [{"id": s["id"], "name": s["name"], "one_time": s["one_time"],
                  "monthly_cost": s["monthly_cost"], "total_spent": s["total_spent"],
                  "active": s["active"]} for s in subs],
    })


def _bill(b):
    """The subset of an enriched row the household view needs."""
    return {
        "id": b["id"], "name": b["name"], "category_name": b.get("category_name"),
        "amount": b["amount"], "billing_cycle": b["billing_cycle"],
        "monthly_cost": b["monthly_cost"], "yearly_cost": b["yearly_cost"],
        "total_spent": b["total_spent"], "active": b["active"],
        "next_renewal": b["next_renewal"], "days_until_renew": b["days_until_renew"],
    }


@app.route("/api/household-overview")
def api_household_overview():
    """What the home costs: every bill in a category flagged household."""
    today = config.today()
    with db() as conn:
        bills = [s for s in _all_enriched(conn, today) if s["household"]]

    active = [b for b in bills if b["active"]]
    by_cat = {}
    for b in active:
        key = b.get("category_name") or "Uncategorized"
        by_cat[key] = round(by_cat.get(key, 0) + b["monthly_cost"], 2)

    upcoming = sorted([b for b in active if b["days_until_renew"] is not None],
                      key=lambda b: b["days_until_renew"])[:12]
    # Biggest bills first: with household costs the ranking is the useful view,
    # not the renewal order.
    ranked = sorted(active, key=lambda b: -b["monthly_cost"])
    return jsonify({
        "currency": get_currency(),
        "monthly_total": round(sum(b["monthly_cost"] for b in active), 2),
        "yearly_total": round(sum(b["yearly_cost"] for b in active), 2),
        "total_spent": round(sum(b["total_spent"] for b in bills), 2),
        "active_count": len(active),
        "total_count": len(bills),
        "biggest": ranked[0]["name"] if ranked else "",
        "by_category": sorted([{"name": k, "monthly": v} for k, v in by_cat.items()],
                              key=lambda x: -x["monthly"]),
        # Active bills biggest-first, then the paused ones after them.
        "bills": [_bill(b) for b in ranked] +
                 [_bill(b) for b in bills if not b["active"]],
        "upcoming": [{
            "id": b["id"], "name": b["name"], "category_name": b.get("category_name"),
            "amount": b["amount"], "billing_cycle": b["billing_cycle"],
            "next_renewal": b["next_renewal"], "days_until_renew": b["days_until_renew"],
        } for b in upcoming],
    })


# ── API: settings ─────────────────────────────────────────────────────────────
@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    return jsonify({"currency": get_currency(), "upcoming_days": config.UPCOMING_DAYS})


@app.route("/api/settings", methods=["POST"])
def api_settings_set():
    d = request.get_json(force=True)
    if "currency" in d:
        cur = (str(d.get("currency") or "")).strip()[:4]
        if not cur:
            return jsonify({"error": "Currency symbol can't be empty."}), 400
        set_setting("currency", cur)
    return jsonify({"ok": True})


# ── API: export + reset ───────────────────────────────────────────────────────
@app.route("/api/export")
def api_export():
    today = config.today()
    with db() as conn:
        cats = [dict(r) for r in conn.execute(
            "SELECT * FROM categories ORDER BY sort_order").fetchall()]
        subs = _all_enriched(conn, today)
        price_history = [dict(r) for r in conn.execute(
            "SELECT * FROM price_history ORDER BY sub_id, changed_on, id").fetchall()]
        periods = [dict(r) for r in conn.execute(
            "SELECT * FROM activation_periods ORDER BY sub_id, started_on, id").fetchall()]
        usage = [dict(r) for r in conn.execute(
            "SELECT * FROM usage_charges ORDER BY sub_id, charged_on, id").fetchall()]
        games = [dict(r) for r in conn.execute(
            "SELECT * FROM games ORDER BY purchased_on, id").fetchall()]
    payload = json.dumps({
        "exported_at": now_iso(),
        "currency": get_currency(),
        "categories": cats,
        "subscriptions": subs,
        "price_history": price_history,
        "activation_periods": periods,
        "usage_charges": usage,
        "games": games,
    }, indent=2)
    return send_file(io.BytesIO(payload.encode("utf-8")), as_attachment=True,
                     download_name="subscriptions-export.json", mimetype="application/json")


@app.route("/api/reset", methods=["POST"])
def api_reset():
    with db() as conn:
        conn.execute("DELETE FROM subscriptions")
        conn.execute("DELETE FROM games")
        conn.execute("DELETE FROM categories")               # start clean…
        conn.executemany(                                    # …then re-seed the defaults
            "INSERT INTO categories (name, sort_order) VALUES (?, ?)",
            [(name, i) for i, name in enumerate(config.SEED_CATEGORIES)])
        # Re-seeding only covers SEED_CATEGORIES, so the one-time bucket has to
        # be put back explicitly or it would be missing until the next restart.
        database._ensure_one_time_category(conn)
    return jsonify({"ok": True})


init_db()

if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
