"""SQLite connection, schema/migrations and the key/value settings helpers."""
import sqlite3

import config


def db():
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 8000")
    return conn


def init_db():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS categories (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    NOT NULL UNIQUE,
                sort_order INTEGER NOT NULL DEFAULT 0,
                one_time   INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS subscriptions (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id    INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
                name           TEXT    NOT NULL,
                billing_cycle  TEXT    NOT NULL DEFAULT 'monthly',   -- monthly | yearly
                amount         REAL    NOT NULL DEFAULT 0,           -- price per billing cycle
                start_date     TEXT    NOT NULL DEFAULT '',          -- ISO date (YYYY-MM-DD) or ''
                renew_date     TEXT    NOT NULL DEFAULT '',          -- next renewal, ISO date or ''
                active         INTEGER NOT NULL DEFAULT 1,
                payment_method TEXT    NOT NULL DEFAULT '',          -- Credit Card | PayPal | iDeal | ''
                necessity      TEXT    NOT NULL DEFAULT 'Important',  -- Nice to Have | Important | Critical
                notes          TEXT    NOT NULL DEFAULT '',
                sort_order     INTEGER NOT NULL DEFAULT 0,
                created_at     TEXT    NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_subs_cat ON subscriptions(category_id);

            -- One row per recorded price (auto-appended when the amount changes).
            CREATE TABLE IF NOT EXISTS price_history (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                sub_id        INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                amount        REAL    NOT NULL,
                billing_cycle TEXT    NOT NULL DEFAULT 'monthly',
                changed_on    TEXT    NOT NULL,                      -- ISO date the price took effect
                note          TEXT    NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_price_sub ON price_history(sub_id);

            -- On/off spells: Enable opens a period, Disable closes it. A NULL
            -- ended_on means the subscription is currently active.
            CREATE TABLE IF NOT EXISTS activation_periods (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                sub_id     INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                started_on TEXT    NOT NULL,                         -- ISO date turned on
                ended_on   TEXT                                      -- ISO date turned off, or NULL
            );
            CREATE INDEX IF NOT EXISTS idx_period_sub ON activation_periods(sub_id);

            -- One row per month a one-time subscription was actually switched on.
            -- A ticker, not a period: each row is a single charge at the price
            -- that applied when it was logged, so re-reading history can never
            -- accrue extra months for a subscription left switched on.
            CREATE TABLE IF NOT EXISTS usage_charges (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                sub_id     INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                charged_on TEXT    NOT NULL,                         -- ISO date logged
                amount     REAL    NOT NULL,
                note       TEXT    NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_usage_sub ON usage_charges(sub_id);

            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        _migrate(conn)
        # First run: seed the starter categories so the app is usable immediately.
        empty = conn.execute("SELECT COUNT(*) AS n FROM categories").fetchone()["n"] == 0
        if empty:
            conn.executemany(
                "INSERT INTO categories (name, sort_order) VALUES (?, ?)",
                [(name, i) for i, name in enumerate(config.SEED_CATEGORIES)])
        _ensure_one_time_category(conn)


def _ensure_one_time_category(conn):
    """Make sure the one-time category exists and is flagged as such.

    Created on every install rather than only fresh ones, so an existing
    database gains it on upgrade. Only ever adds the flag: a category the user
    renamed or re-flagged themselves is left alone.
    """
    row = conn.execute("SELECT id, one_time FROM categories WHERE name = ?",
                       (config.ONE_TIME_CATEGORY,)).fetchone()
    if row is None:
        nxt = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM categories").fetchone()["n"]
        conn.execute("INSERT INTO categories (name, sort_order, one_time) VALUES (?, ?, 1)",
                     (config.ONE_TIME_CATEGORY, nxt))
    elif not row["one_time"]:
        conn.execute("UPDATE categories SET one_time = 1 WHERE id = ?", (row["id"],))


def _migrate(conn):
    """Bring an older database up to the current schema, without data loss.

    Adds the payment_method / necessity columns to pre-existing installs and
    backfills a starting price_history entry and an activation_period for every
    subscription that doesn't have them yet (so the derived totals keep working)."""
    cat_cols = {r["name"] for r in conn.execute("PRAGMA table_info(categories)")}
    if "one_time" not in cat_cols:
        conn.execute("ALTER TABLE categories ADD COLUMN one_time INTEGER NOT NULL DEFAULT 0")

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(subscriptions)")}
    if "payment_method" not in cols:
        conn.execute("ALTER TABLE subscriptions ADD COLUMN payment_method TEXT NOT NULL DEFAULT ''")
    if "necessity" not in cols:
        conn.execute("ALTER TABLE subscriptions ADD COLUMN necessity TEXT NOT NULL DEFAULT 'Important'")

    today = config.today().isoformat()
    for s in conn.execute("SELECT id, amount, billing_cycle, active, start_date, created_at "
                          "FROM subscriptions").fetchall():
        # A sensible date to attribute the starting price / first activation to.
        start = (s["start_date"] or (s["created_at"] or "")[:10] or today)
        if not conn.execute("SELECT 1 FROM price_history WHERE sub_id=?", (s["id"],)).fetchone():
            conn.execute(
                "INSERT INTO price_history (sub_id, amount, billing_cycle, changed_on, note) "
                "VALUES (?, ?, ?, ?, 'created')",
                (s["id"], s["amount"], s["billing_cycle"], start))
        if not conn.execute("SELECT 1 FROM activation_periods WHERE sub_id=?", (s["id"],)).fetchone():
            # Active → an open period from the start; inactive → a period we close
            # today (we don't know the real off-date, and this preserves the
            # previously-shown "total spent" estimate for existing data).
            conn.execute(
                "INSERT INTO activation_periods (sub_id, started_on, ended_on) VALUES (?, ?, ?)",
                (s["id"], start, None if s["active"] else today))


# ── Settings (simple key/value) ───────────────────────────────────────────────
def get_setting(key, default=None):
    with db() as conn:
        r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(key, value):
    with db() as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def get_currency():
    return get_setting("currency", config.DEFAULT_CURRENCY) or config.DEFAULT_CURRENCY
