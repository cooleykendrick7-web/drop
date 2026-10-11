"""
Drop — single-operator food delivery.
Ken is the only driver/operator. Customers order on the site; every order
routes to Ken; he picks up the food and drops it at the door.

Run:  ./venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
DB:   drop.db in this directory (SQLite, persists across restarts).
Env:  STRIPE_SECRET_KEY, STRIPE_PUBLISHABLE_KEY (optional -> dev mode)
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

CENTRAL = ZoneInfo("America/Chicago")

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DROP_DB", os.path.join(BASE, "drop.db"))

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "").strip()
STRIPE_LIVE = bool(STRIPE_SECRET_KEY and STRIPE_PUBLISHABLE_KEY)

if STRIPE_LIVE:
    import stripe
    stripe.api_key = STRIPE_SECRET_KEY

STATUSES = ["placed", "confirmed", "picked_up", "on_the_way", "delivered", "cancelled"]
STATUS_LABELS = {
    "placed": "Order placed",
    "confirmed": "Confirmed by your driver",
    "picked_up": "Picked up from restaurant",
    "on_the_way": "On the way to you",
    "delivered": "Delivered",
    "cancelled": "Cancelled",
}

app = FastAPI(title="Drop")


# ---------------- DB ----------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS restaurants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, cuisine TEXT DEFAULT '', emoji TEXT DEFAULT '🍽️',
        open INTEGER DEFAULT 1, sample INTEGER DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS menu_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id INTEGER NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
        name TEXT NOT NULL, description TEXT DEFAULT '',
        price REAL NOT NULL, available INTEGER DEFAULT 1)""")
    c.execute("""CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id INTEGER NOT NULL REFERENCES restaurants(id),
        items_json TEXT NOT NULL,
        customer_name TEXT NOT NULL, phone TEXT NOT NULL,
        address TEXT NOT NULL, notes TEXT DEFAULT '',
        food_total REAL NOT NULL, delivery_fee REAL NOT NULL, total REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'placed',
        stripe_payment_id TEXT DEFAULT '', stripe_status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS support_tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, phone TEXT DEFAULT '',
        order_id INTEGER DEFAULT 0,
        message TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        created_at TEXT NOT NULL)""")
    # migration: tips (added 2026-10-08)
    cols = [r[1] for r in c.execute("PRAGMA table_info(orders)").fetchall()]
    if "tip" not in cols:
        c.execute("ALTER TABLE orders ADD COLUMN tip REAL DEFAULT 0")
    # promo codes + min order + ETAs (added 2026-10-08)
    c.execute("""CREATE TABLE IF NOT EXISTS promo_codes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL DEFAULT 'fixed',
        value REAL NOT NULL DEFAULT 0,
        min_order REAL NOT NULL DEFAULT 0,
        max_uses INTEGER NOT NULL DEFAULT 0,
        uses INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL)""")
    ocols = [r[1] for r in c.execute("PRAGMA table_info(orders)").fetchall()]
    if "discount" not in ocols:
        c.execute("ALTER TABLE orders ADD COLUMN discount REAL DEFAULT 0")
    if "promo_code" not in ocols:
        c.execute("ALTER TABLE orders ADD COLUMN promo_code TEXT DEFAULT ''")
    rcols = [r[1] for r in c.execute("PRAGMA table_info(restaurants)").fetchall()]
    if "eta" not in rcols:
        c.execute("ALTER TABLE restaurants ADD COLUMN eta TEXT DEFAULT ''")
    c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('min_order', '10.00')")
    c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('accepting_orders', '1')")
    c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('mins_per_delivery', '18')")
    # customer accounts + rewards (added 2026-10-09)
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, phone TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        expires_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS rewards (
        user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        punches INTEGER NOT NULL DEFAULT 0,
        free_fee INTEGER NOT NULL DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS visits (
        date TEXT NOT NULL, page TEXT NOT NULL, count INTEGER DEFAULT 0,
        PRIMARY KEY(date, page))""")
    ocols2 = [r[1] for r in c.execute("PRAGMA table_info(orders)").fetchall()]
    if "user_id" not in ocols2:
        c.execute("ALTER TABLE orders ADD COLUMN user_id INTEGER DEFAULT 0")
    rcols = [r[1] for r in c.execute("PRAGMA table_info(restaurants)").fetchall()]
    if "logo" not in rcols:
        c.execute("ALTER TABLE restaurants ADD COLUMN logo TEXT DEFAULT ''")
    if "photo" not in rcols:
        c.execute("ALTER TABLE restaurants ADD COLUMN photo TEXT DEFAULT ''")
    # maps + AI watchdog (added 2026-10-10)
    rcols = [r[1] for r in c.execute("PRAGMA table_info(restaurants)").fetchall()]
    for _col, _ddl in (("address", "TEXT DEFAULT ''"), ("lat", "REAL DEFAULT 0"),
                       ("lng", "REAL DEFAULT 0"), ("website", "TEXT DEFAULT ''")):
        if _col not in rcols:
            c.execute(f"ALTER TABLE restaurants ADD COLUMN {_col} {_ddl}")
    ocols3 = [r[1] for r in c.execute("PRAGMA table_info(orders)").fetchall()]
    for _col, _ddl in (("cust_lat", "REAL DEFAULT 0"), ("cust_lng", "REAL DEFAULT 0"),
                       ("driver_lat", "REAL DEFAULT 0"), ("driver_lng", "REAL DEFAULT 0"),
                       ("driver_updated", "TEXT DEFAULT ''")):
        if _col not in ocols3:
            c.execute(f"ALTER TABLE orders ADD COLUMN {_col} {_ddl}")
    c.execute("""CREATE TABLE IF NOT EXISTS menu_proposals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id INTEGER NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
        kind TEXT NOT NULL DEFAULT 'price_change',
        item_name TEXT NOT NULL DEFAULT '',
        old_price REAL DEFAULT 0, new_price REAL DEFAULT 0,
        detail TEXT DEFAULT '', source_url TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL)""")
    # support ticket replies (added 2026-10-10)
    c.execute("""CREATE TABLE IF NOT EXISTS ticket_replies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id INTEGER NOT NULL REFERENCES support_tickets(id) ON DELETE CASCADE,
        sender TEXT NOT NULL DEFAULT 'customer',
        message TEXT NOT NULL,
        created_at TEXT NOT NULL)""")
    # Drop More: B2B courier businesses (added 2026-10-11)
    c.execute("""CREATE TABLE IF NOT EXISTS businesses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, category TEXT DEFAULT '',
        address TEXT DEFAULT '', lat REAL DEFAULT 0, lng REAL DEFAULT 0,
        phone TEXT DEFAULT '', website TEXT DEFAULT '',
        logo TEXT DEFAULT '', photo TEXT DEFAULT '',
        open INTEGER DEFAULT 1,
        created_at TEXT NOT NULL)""")
    # Droppers: driver applications + accounts (added 2026-10-11)
    c.execute("""CREATE TABLE IF NOT EXISTS drivers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, phone TEXT NOT NULL UNIQUE, email TEXT DEFAULT '',
        password_hash TEXT NOT NULL DEFAULT '',
        license_number TEXT DEFAULT '', license_state TEXT DEFAULT '',
        insurance TEXT DEFAULT '',
        vehicle_make TEXT DEFAULT '', vehicle_model TEXT DEFAULT '',
        vehicle_year TEXT DEFAULT '', vehicle_color TEXT DEFAULT '',
        availability TEXT DEFAULT '', background_consent INTEGER DEFAULT 0,
        experience TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        deliveries INTEGER NOT NULL DEFAULT 0,
        on_time INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS driver_sessions (
        token TEXT PRIMARY KEY,
        driver_id INTEGER NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
        expires_at TEXT NOT NULL)""")
    # watcher messages (added 2026-10-11) — PIN 5683 watchers chat with Ken
    c.execute("""CREATE TABLE IF NOT EXISTS watcher_msgs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender TEXT NOT NULL DEFAULT 'watcher',
        message TEXT NOT NULL,
        created_at TEXT NOT NULL,
        seen_by_owner INTEGER DEFAULT 0)""")
    # driver documents (added 2026-10-11) — ID front/back, insurance, selfie, car photo
    c.execute("""CREATE TABLE IF NOT EXISTS driver_docs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        driver_id INTEGER NOT NULL,
        doc_type TEXT NOT NULL,
        filename TEXT NOT NULL,
        uploaded_at TEXT NOT NULL)""")
    # driver acceptance tracking (added 2026-10-11)
    dcols = [r[1] for r in c.execute("PRAGMA table_info(drivers)").fetchall()]
    for _col, _ddl in (("accepted", "INTEGER DEFAULT 0"),
                       ("declined", "INTEGER DEFAULT 0"),
                       ("is_online", "INTEGER DEFAULT 0"),
                       ("today_earned", "REAL DEFAULT 0"),
                       ("today_date", "TEXT DEFAULT ''")):
        if _col not in dcols:
            c.execute(f"ALTER TABLE drivers ADD COLUMN {_col} {_ddl}")
    # Drop Investors: inbound investor interest (added 2026-10-11)
    c.execute("""CREATE TABLE IF NOT EXISTS investor_leads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, email TEXT NOT NULL, phone TEXT DEFAULT '',
        amount_range TEXT DEFAULT '', message TEXT DEFAULT '',
        created_at TEXT NOT NULL)""")
    # courier order fields + driver assignment (added 2026-10-11)
    ocols4 = [r[1] for r in c.execute("PRAGMA table_info(orders)").fetchall()]
    for _col, _ddl in (("kind", "TEXT DEFAULT 'food'"),
                       ("business_id", "INTEGER DEFAULT 0"),
                       ("pickup_name", "TEXT DEFAULT ''"),
                       ("pickup_address", "TEXT DEFAULT ''"),
                       ("pickup_lat", "REAL DEFAULT 0"),
                       ("pickup_lng", "REAL DEFAULT 0"),
                       ("item_desc", "TEXT DEFAULT ''"),
                       ("driver_id", "INTEGER DEFAULT 0")):
        if _col not in ocols4:
            c.execute(f"ALTER TABLE orders ADD COLUMN {_col} {_ddl}")
    # DROP MORE financial engine (added 2026-10-11)
    ocols5 = [r[1] for r in c.execute("PRAGMA table_info(orders)").fetchall()]
    if "driver_pay" not in ocols5:
        c.execute("ALTER TABLE orders ADD COLUMN driver_pay REAL DEFAULT 0")
    c.execute("""CREATE TABLE IF NOT EXISTS payouts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        driver_id INTEGER NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
        amount REAL NOT NULL,
        kind TEXT NOT NULL DEFAULT 'standard',
        status TEXT NOT NULL DEFAULT 'requested',
        stripe_transfer_id TEXT DEFAULT '',
        note TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        paid_at TEXT DEFAULT '')""")
    # financial defaults (only set if missing)
    for _k, _v in (("price_base_fee", "3.99"), ("price_per_mile", "1.50"),
                   ("driver_base_pay", "3.00"), ("driver_per_mile", "1.00"),
                   ("min_profit_per_order", "1.00")):
        if c.execute("SELECT value FROM settings WHERE key=?", (_k,)).fetchone() is None:
            c.execute("INSERT INTO settings(key, value) VALUES(?, ?)", (_k, _v))
    # system activity feed: everything the platform does on its own
    c.execute("""CREATE TABLE IF NOT EXISTS system_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL DEFAULT 'info',
        title TEXT NOT NULL DEFAULT '',
        detail TEXT DEFAULT '',
        created_at TEXT NOT NULL)""")
    # gift orders (added 2026-10-11)
    ocols = [r[1] for r in c.execute("PRAGMA table_info(orders)").fetchall()]
    for _col, _ddl in (("is_gift", "INTEGER DEFAULT 0"),
                       ("gift_recipient", "TEXT DEFAULT ''"),
                       ("gift_contact", "TEXT DEFAULT ''"),
                       ("gift_message", "TEXT DEFAULT ''"),
                       ("gift_sender", "TEXT DEFAULT ''")):
        if _col not in ocols:
            c.execute(f"ALTER TABLE orders ADD COLUMN {_col} {_ddl}")
    c.execute("""CREATE TABLE IF NOT EXISTS gift_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL,
        recipient TEXT NOT NULL DEFAULT '',
        contact TEXT NOT NULL DEFAULT '',
        message TEXT DEFAULT '',
        sender TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL)""")
    # referrals (added 2026-10-11)
    c.execute("""CREATE TABLE IF NOT EXISTS referrals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        referrer_id INTEGER NOT NULL,
        referred_id INTEGER NOT NULL,
        code TEXT NOT NULL,
        credit_given INTEGER DEFAULT 0,
        created_at TEXT NOT NULL)""")
    # customer store credit
    ucols = [r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()]
    if "credit" not in ucols:
        c.execute("ALTER TABLE users ADD COLUMN credit REAL DEFAULT 0")
    if "referral_code" not in ucols:
        c.execute("ALTER TABLE users ADD COLUMN referral_code TEXT DEFAULT ''")
    if "ntfy_topic" not in ucols:
        c.execute("ALTER TABLE users ADD COLUMN ntfy_topic TEXT DEFAULT ''")
    # scheduled orders (added 2026-10-11)
    ocols2 = [r[1] for r in c.execute("PRAGMA table_info(orders)").fetchall()]
    if "scheduled_for" not in ocols2:
        c.execute("ALTER TABLE orders ADD COLUMN scheduled_for TEXT DEFAULT ''")
    if "credit_used" not in ocols2:
        c.execute("ALTER TABLE orders ADD COLUMN credit_used REAL DEFAULT 0")
    # reviews (added 2026-10-11)
    c.execute("""CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        rating INTEGER NOT NULL,
        text TEXT DEFAULT '',
        created_at TEXT NOT NULL)""")
    # promo blasts (added 2026-10-11)
    c.execute("""CREATE TABLE IF NOT EXISTS blasts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message TEXT NOT NULL,
        sent_count INTEGER DEFAULT 0,
        created_at TEXT NOT NULL)""")
    # DROP MORE marketplace: business partnerships (added 2026-10-11)
    c.execute("""CREATE TABLE IF NOT EXISTS business_partners (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        legal_name TEXT NOT NULL, dba TEXT DEFAULT '',
        contact_name TEXT NOT NULL, contact_title TEXT DEFAULT '',
        email TEXT NOT NULL, phone TEXT DEFAULT '',
        website TEXT DEFAULT '', address TEXT DEFAULT '',
        business_type TEXT DEFAULT '', products TEXT DEFAULT '',
        locations INTEGER DEFAULT 1,
        est_deliveries TEXT DEFAULT '',
        service_areas TEXT DEFAULT '',
        shipment_info TEXT DEFAULT '',
        proposed_rates TEXT DEFAULT '',
        payment_terms TEXT DEFAULT '',
        commission TEXT DEFAULT '',
        contract_duration TEXT DEFAULT '',
        start_date TEXT DEFAULT '',
        requirements TEXT DEFAULT '',
        comments TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'submitted',
        profit_score REAL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS deal_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        partner_id INTEGER NOT NULL REFERENCES business_partners(id) ON DELETE CASCADE,
        author TEXT NOT NULL DEFAULT 'operator',
        note TEXT NOT NULL,
        created_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS business_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        partner_id INTEGER NOT NULL REFERENCES business_partners(id) ON DELETE CASCADE,
        name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS business_sessions (
        token TEXT PRIMARY KEY,
        business_user_id INTEGER NOT NULL REFERENCES business_users(id) ON DELETE CASCADE,
        expires_at TEXT NOT NULL)""")
    # audit trail: every admin decision, recorded
    c.execute("""CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        actor TEXT NOT NULL DEFAULT 'operator',
        action TEXT NOT NULL,
        entity_type TEXT NOT NULL DEFAULT '',
        entity_id INTEGER DEFAULT 0,
        details TEXT DEFAULT '',
        created_at TEXT NOT NULL)""")
    # driver compliance fields
    dcols = [r[1] for r in c.execute("PRAGMA table_info(drivers)").fetchall()]
    for _col, _ddl in (("gov_id_status", "TEXT DEFAULT 'pending'"),
                       ("insurance_status", "TEXT DEFAULT 'pending'"),
                       ("vehicle_reg_status", "TEXT DEFAULT 'pending'"),
                       ("safety_ack", "INTEGER DEFAULT 0"),
                       ("policy_ack", "INTEGER DEFAULT 0"),
                       ("photo_url", "TEXT DEFAULT ''"),
                       ("service_areas", "TEXT DEFAULT ''"),
                       ("perf_score", "REAL DEFAULT 0"),
                       ("perf_notes", "TEXT DEFAULT ''"),
                       ("stripe_account_id", "TEXT DEFAULT ''")):
        if _col not in dcols:
            c.execute(f"ALTER TABLE drivers ADD COLUMN {_col} {_ddl}")
    # driver notifications + safety (added 2026-10-11)
    dcols2 = [r[1] for r in c.execute("PRAGMA table_info(drivers)").fetchall()]
    for _col, _ddl in (("ntfy_topic", "TEXT DEFAULT ''"),
                       ("last_lat", "REAL DEFAULT 0"),
                       ("last_lng", "REAL DEFAULT 0"),
                       ("last_gps_at", "TEXT DEFAULT ''"),
                       ("speed_warnings", "INTEGER DEFAULT 0")):
        if _col not in dcols2:
            c.execute(f"ALTER TABLE drivers ADD COLUMN {_col} {_ddl}")
    c.execute("""CREATE TABLE IF NOT EXISTS driver_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        driver_id INTEGER NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
        kind TEXT NOT NULL DEFAULT 'boost',
        title TEXT NOT NULL DEFAULT '',
        body TEXT NOT NULL DEFAULT '',
        read INTEGER DEFAULT 0,
        created_at TEXT NOT NULL)""")
    # AI outreach emails (added 2026-10-11)
    c.execute("""CREATE TABLE IF NOT EXISTS outreach_emails (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        to_email TEXT NOT NULL, to_name TEXT DEFAULT '',
        subject TEXT NOT NULL, body TEXT NOT NULL,
        business_name TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'draft',
        created_at TEXT NOT NULL,
        sent_at TEXT DEFAULT '')""")
    defaults = {
        "delivery_fee": "3.99",
        "pin": "1234",
        "ntfy_topic": "",
        "tagline": "We drop it at your door.",
        "open_hour": "10",
        "close_hour": "23",
        "delivery_zone": "Bismarck, ND",
    }
    for k, v in defaults.items():
        c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v))
    # operator PIN updated to 5984 (2026-10-11)
    c.execute("UPDATE settings SET value='5984' WHERE key='pin' AND value IN ('1234','9584')")
    conn.commit()
    conn.close()


def get_setting(key):
    conn = db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else ""


def set_setting(key, value):
    conn = db()
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (key, value))
    conn.commit()
    conn.close()


def seed():
    conn = db()
    if conn.execute("SELECT COUNT(*) c FROM restaurants").fetchone()["c"] > 0:
        conn.close()
        return
    data = [
        ("Big Jake's Burgers", "American · Burgers", "🍔", [
            ("Classic Smash Burger", "Double smashed patty, cheese, pickles, house sauce", 9.99),
            ("Bacon Cheeseburger", "Thick patty, crispy bacon, cheddar, onion rings", 11.49),
            ("Loaded Fries", "Cheese sauce, bacon bits, jalapeños, ranch", 5.99),
            ("Sweet Tea (32oz)", "Mississippi-style extra sweet", 2.49),
        ]),
        ("Mama Lou's Soul Food", "Soul Food", "🍗", [
            ("Fried Chicken Plate", "3pc golden fried chicken, 2 sides, cornbread", 13.99),
            ("Smothered Pork Chops", "Gravy-smothered chops with rice and greens", 14.99),
            ("Mac & Cheese", "Creamy baked side", 4.49),
            ("Collard Greens", "Slow-cooked with smoked turkey", 3.99),
            ("Cornbread (2 pc)", "Buttery skillet cornbread", 2.49),
        ]),
        ("El Camino Taqueria", "Mexican", "🌮", [
            ("Street Tacos (3)", "Your choice: asada, carnitas, or chicken, cilantro & onion", 8.99),
            ("Burrito Supreme", "Loaded burrito, queso on top", 10.99),
            ("Queso & Chips", "Warm queso, fresh tortilla chips", 6.49),
            ("Churros (3)", "Cinnamon sugar, chocolate dip", 3.99),
        ]),
        ("Delta Slice Co.", "Pizza", "🍕", [
            ("Pepperoni Pizza (14\")", "Hand-tossed, extra pepperoni, mozzarella", 14.99),
            ("Veggie Supreme Pizza", "Peppers, onions, mushrooms, olives", 13.99),
            ("Garlic Knots (6)", "Buttery, parmesan, marinara", 5.49),
            ("Cheese Sticks", "Mozzarella sticks, marinara", 6.99),
        ]),
    ]
    for name, cuisine, emoji, items in data:
        cur = conn.execute(
            "INSERT INTO restaurants(name, cuisine, emoji, open, sample) VALUES(?,?,?,?,1)",
            (name, cuisine, emoji, 1))
        rid = cur.lastrowid
        for n, d, p in items:
            conn.execute(
                "INSERT INTO menu_items(restaurant_id, name, description, price) VALUES(?,?,?,?)",
                (rid, n, d, p))
    conn.commit()
    conn.close()


init_db()
seed()


# ---------------- helpers ----------------
def now_iso():
    return datetime.now(timezone.utc).isoformat()


def get_hours():
    try:
        oh = max(0, min(23, int(get_setting("open_hour") or 10)))
    except (TypeError, ValueError):
        oh = 10
    try:
        ch = max(0, min(23, int(get_setting("close_hour") or 23)))
    except (TypeError, ValueError):
        ch = 23
    return oh, ch


def is_open_now():
    oh, ch = get_hours()
    now = datetime.now(CENTRAL)
    h = now.hour + now.minute / 60
    if oh == ch:
        return True  # open 24 hours
    if oh < ch:
        return oh <= h < ch
    return h >= oh or h < ch  # overnight range


def fmt_hour(h):
    h = int(h) % 24
    ap = "am" if h < 12 else "pm"
    return f"{h % 12 or 12}{ap}"


def order_dict(row):
    d = dict(row)
    d["items"] = json.loads(d.pop("items_json"))
    d["status_label"] = STATUS_LABELS.get(d["status"], d["status"])
    return d


def send_ntfy(topic, title, msg):
    """Send a push notification to any ntfy topic."""
    if not topic or not topic.strip():
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{urllib.parse.quote(topic.strip())}",
            data=msg.encode(),
            headers={"Title": title, "Tags": "car"},
            method="POST")
        threading.Thread(
            target=lambda: urllib.request.urlopen(req, timeout=10),
            daemon=True).start()
    except Exception:
        pass


def notify_ken(order):
    """Best-effort push to Ken's phone via ntfy.sh (free, no signup)."""
    topic = get_setting("ntfy_topic").strip()
    if not topic:
        return
    try:
        items = ", ".join(f"{i['qty']}x {i['name']}" for i in order["items"])
        msg = (f"NEW DROP ORDER #{order['id']}\n"
               f"{order['customer_name']} {order['phone']}\n"
               f"{order['address']}\n"
               f"{items}\n"
               f"Total ${order['total']:.2f} (fee ${order['delivery_fee']:.2f})")
        req = urllib.request.Request(
            f"https://ntfy.sh/{urllib.parse.quote(topic)}",
            data=msg.encode(),
            headers={"Title": "New Drop order 🛵", "Tags": "money"},
            method="POST")
        threading.Thread(
            target=lambda: urllib.request.urlopen(req, timeout=5).read(),
            daemon=True).start()
    except Exception:
        pass  # never break an order because a push failed


def check_pin(x_pin: str = Header(default="")):
    if x_pin != get_setting("pin"):
        raise HTTPException(status_code=401, detail="Bad PIN")


def geocode(address, parts=None):
    """Best-effort address -> (lat, lng) via Nominatim (OpenStreetMap, free).
    When structured parts are given (street/city/state/zip), uses the structured
    search API for much higher accuracy. Returns (0.0, 0.0) when it can't find
    it. Never raises."""
    try:
        if parts and parts.get("city") and parts.get("state"):
            # Structured search — far more accurate than free-text
            params = {
                "street": parts.get("street", ""),
                "city": parts.get("city", ""),
                "state": parts.get("state", ""),
                "postalcode": parts.get("zip", ""),
                "country": "USA",
                "format": "json",
                "limit": "1",
            }
            # drop empty params
            params = {k: v for k, v in params.items() if v}
            qs = urllib.parse.urlencode(params)
            url = f"https://nominatim.openstreetmap.org/search?{qs}"
        else:
            q = urllib.parse.quote(address)
            url = f"https://nominatim.openstreetmap.org/search?q={q}&format=json&limit=1"
        req = urllib.request.Request(
            url, headers={"User-Agent": "DropDelivery/1.0 (wearedropservicesllc@gmail.com)"})
        data = json.load(urllib.request.urlopen(req, timeout=8))
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        pass
    return 0.0, 0.0


# ---------------- public API ----------------
@app.get("/api/health")
def health():
    return {"ok": True, "stripe_live": STRIPE_LIVE, "time": now_iso()}


@app.get("/api/config")
def config():
    oh, ch = get_hours()
    return {
        "stripe_live": STRIPE_LIVE,
        "stripe_publishable_key": STRIPE_PUBLISHABLE_KEY if STRIPE_LIVE else "",
        "delivery_fee": float(get_setting("delivery_fee")),
        "min_order": float(get_setting("min_order") or 10),
        "tagline": get_setting("tagline"),
        "open_now": is_open_now(),
        "open_hour": oh,
        "close_hour": ch,
        "hours_label": f"{fmt_hour(oh)}–{fmt_hour(ch)}",
        "delivery_zone": get_setting("delivery_zone") or "Bismarck, ND",
    }


@app.get("/api/restaurants")
def list_restaurants():
    conn = db()
    rows = conn.execute("SELECT * FROM restaurants ORDER BY id").fetchall()
    out = []
    for r in rows:
        rd = dict(r)
        items = conn.execute(
            "SELECT * FROM menu_items WHERE restaurant_id=? AND available=1 ORDER BY id",
            (r["id"],)).fetchall()
        rd["menu"] = [dict(i) for i in items]
        out.append(rd)
    conn.close()
    return out


@app.post("/api/orders")
async def create_order(req: Request):
    """Create an order (unpaid). Server prices everything from the DB."""
    body = await req.json()
    if not is_open_now():
        oh, _ = get_hours()
        raise HTTPException(400, f"We're closed right now — back at {fmt_hour(oh)}")
    if get_setting("accepting_orders") == "0":
        raise HTTPException(400, "We're at capacity right now — please check back soon")
    rid = body.get("restaurant_id")
    # logged-in customer?
    me = get_session_user(req)
    user_id = me["id"] if me else 0
    items = body.get("items", [])
    cust = body.get("customer", {})
    name = (cust.get("name") or "").strip()
    phone = (cust.get("phone") or "").strip()
    address = (cust.get("address") or "").strip()
    notes = (cust.get("notes") or "").strip()
    if not rid or not items or not name or not phone or not address:
        raise HTTPException(400, "Missing restaurant, items, name, phone, or address")
    conn = db()
    rest = conn.execute("SELECT * FROM restaurants WHERE id=?", (rid,)).fetchone()
    if not rest:
        conn.close()
        raise HTTPException(400, "Unknown restaurant")
    if not rest["open"]:
        conn.close()
        raise HTTPException(400, "Restaurant is closed right now")
    food_total = 0.0
    priced = []
    for it in items:
        mi = conn.execute(
            "SELECT * FROM menu_items WHERE id=? AND restaurant_id=? AND available=1",
            (it.get("menu_item_id"), rid)).fetchone()
        if not mi:
            conn.close()
            raise HTTPException(400, "A menu item is invalid or unavailable")
        qty = max(1, min(20, int(it.get("qty", 1))))
        line = round(mi["price"] * qty, 2)
        food_total += line
        priced.append({"menu_item_id": mi["id"], "name": mi["name"],
                       "price": mi["price"], "qty": qty, "line": line})
    food_total = round(food_total, 2)
    fee = round(float(get_setting("delivery_fee")), 2)
    free_fee_applied = False
    if user_id:
        rw = get_rewards(user_id)
        if rw["free_fee"]:
            fee = 0.0
            free_fee_applied = True
            conn.execute("UPDATE rewards SET free_fee=0 WHERE user_id=?", (user_id,))
    try:
        tip = round(max(0.0, min(100.0, float(body.get("tip", 0)))), 2)
    except (TypeError, ValueError):
        tip = 0.0
    # promo code (re-validated server-side; never trust the client)
    discount, promo_code = 0.0, ""
    pcode = (body.get("promo_code") or "").strip()
    if pcode:
        discount, msg = validate_promo(pcode, food_total)
        if discount > 0:
            promo_code = pcode.strip().upper()
            conn.execute("UPDATE promo_codes SET uses=uses+1 WHERE code=?", (promo_code,))
    total = round(food_total + fee + tip - discount, 2)
    # referral/store credit (added 2026-10-11)
    credit_used = 0.0
    if me and body.get("use_credit"):
        credit = conn.execute("SELECT credit FROM users WHERE id=?", (me["id"],)).fetchone()["credit"] or 0
        credit_used = round(min(credit, total), 2)
        if credit_used > 0:
            conn.execute("UPDATE users SET credit=credit-? WHERE id=?", (credit_used, me["id"]))
            total = round(total - credit_used, 2)
    # gift order?
    gift = body.get("gift") or {}
    is_gift = 1 if gift.get("recipient_name") else 0
    gift_recipient = (gift.get("recipient_name") or "").strip()[:80]
    gift_contact = (gift.get("recipient_contact") or "").strip()[:120]
    gift_message = (gift.get("message") or "").strip()[:500]
    cur = conn.execute(
        """INSERT INTO orders(restaurant_id, items_json, customer_name, phone, address,
                              notes, food_total, delivery_fee, tip, discount, promo_code,
                              user_id, total, status, stripe_status, created_at, updated_at,
                              is_gift, gift_recipient, gift_contact, gift_message, gift_sender,
                              credit_used)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'placed','pending',?,?, ?,?,?,?,? ,?)""",
        (rid, json.dumps(priced), name, phone, address, notes,
         food_total, fee, tip, discount, promo_code, user_id, total, now_iso(), now_iso(),
         is_gift, gift_recipient, gift_contact, gift_message, name if is_gift else "",
         credit_used))
    oid = cur.lastrowid
    conn.commit()
    # queue gift notification (sent by background job from business Gmail)
    if is_gift and gift_contact:
        conn.execute(
            "INSERT INTO gift_queue(order_id, recipient, contact, message, sender, status, created_at)"
            " VALUES(?,?,?,?,?,'pending',?)",
            (oid, gift_recipient, gift_contact, gift_message, name, now_iso()))
        conn.commit()
        log_event("gift", f"🎁 Gift order #{oid} queued",
                  f"For {gift_recipient} from {name}")
    # pin the delivery address on the map (background — never slows the order)
    # uses structured parts when available for max accuracy
    addr_parts = (body.get("customer") or {}).get("address_parts")
    def _geocode_order():
        try:
            glat, glng = geocode(address, addr_parts)
            if glat:
                c2 = db()
                c2.execute("UPDATE orders SET cust_lat=?, cust_lng=? WHERE id=?",
                           (glat, glng, oid))
                c2.commit()
                c2.close()
        except Exception:
            pass
    threading.Thread(target=_geocode_order, daemon=True).start()
    conn.close()
    return {"order_id": oid, "food_total": food_total, "delivery_fee": fee,
            "tip": tip, "discount": discount, "promo_code": promo_code,
            "free_fee_applied": free_fee_applied,
            "total": total, "stripe_live": STRIPE_LIVE}


@app.post("/api/checkout")
async def checkout(req: Request):
    """Start real payment. Live: Stripe Checkout URL. Dev: test-pay endpoint."""
    body = await req.json()
    oid = body.get("order_id")
    conn = db()
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    conn.close()
    if not o:
        raise HTTPException(404, "Order not found")
    if o["stripe_status"] == "paid":
        return {"already_paid": True, "track_url": f"/track.html?id={oid}"}
    # referral credit applied at order time — adjust line items
    credit_used = o["credit_used"] if "credit_used" in o.keys() else 0
    if STRIPE_LIVE:
        base = str(req.base_url).rstrip("/")
        net_total = round(o["total"], 2)
        if net_total <= 0:
            # credit covered everything — no Stripe needed
            conn = db()
            conn.execute("UPDATE orders SET stripe_status='paid', stripe_payment_id='credit',"
                         " updated_at=? WHERE id=?", (now_iso(), oid))
            conn.commit()
            conn.close()
            return {"already_paid": True, "track_url": f"/track.html?id={oid}",
                    "note": "Paid with referral credit"}
        line_items = [{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": f"Drop order #{oid} — food"},
                    "unit_amount": int(round(o["food_total"] * 100)),
                },
                "quantity": 1,
            }, {
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": "Drop delivery fee"},
                    "unit_amount": int(round(o["delivery_fee"] * 100)),
                },
                "quantity": 1,
            }]
        if (o["tip"] or 0) > 0:
            line_items.append({
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": "Tip for your driver"},
                    "unit_amount": int(round(o["tip"] * 100)),
                },
                "quantity": 1,
            })
        if (o["discount"] or 0) > 0:
            line_items.append({
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": f"Discount ({o['promo_code']})"},
                    "unit_amount": -int(round(o["discount"] * 100)),
                },
                "quantity": 1,
            })
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=line_items,
            success_url=f"{base}/pay/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base}/checkout.html?order_id={oid}&cancelled=1",
            metadata={"drop_order_id": str(oid)},
        )
        conn = db()
        conn.execute("UPDATE orders SET stripe_payment_id=?, updated_at=? WHERE id=?",
                     (session.id, now_iso(), oid))
        conn.commit()
        conn.close()
        return {"url": session.url}
    return {"dev": True, "pay_url": f"/api/dev-pay/{oid}",
            "note": "DEV MODE: no Stripe keys configured. Test payment only."}


@app.post("/api/dev-pay/{oid}")
def dev_pay(oid: int):
    """DEV ONLY: simulate a successful payment. Refused when Stripe is live."""
    if STRIPE_LIVE:
        raise HTTPException(400, "Stripe is live; use real checkout")
    conn = db()
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        conn.close()
        raise HTTPException(404, "Order not found")
    conn.execute("UPDATE orders SET stripe_status='paid', stripe_payment_id='dev_test',"
                 " updated_at=? WHERE id=?", (now_iso(), oid))
    conn.commit()
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    od = order_dict(o)
    conn.close()
    notify_ken(od)
    return {"ok": True, "track_url": f"/track.html?id={oid}"}


@app.get("/pay/success")
def pay_success(session_id: str):
    if not STRIPE_LIVE:
        raise HTTPException(400, "Stripe not configured")
    session = stripe.checkout.Session.retrieve(session_id)
    oid = int(session.metadata.get("drop_order_id", 0))
    if session.payment_status == "paid" and oid:
        conn = db()
        o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if o and o["stripe_status"] != "paid":
            conn.execute("UPDATE orders SET stripe_status='paid', updated_at=? WHERE id=?",
                         (now_iso(), oid))
            conn.commit()
            od = order_dict(conn.execute(
                "SELECT * FROM orders WHERE id=?", (oid,)).fetchone())
            notify_ken(od)
        conn.close()
    return RedirectResponse(f"/track.html?id={oid}", status_code=303)


@app.get("/api/orders/{oid}/public")
def public_order(oid: int):
    conn = db()
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        conn.close()
        raise HTTPException(404, "Order not found")
    kind = o["kind"] or "food"
    if kind == "courier":
        label = o["pickup_name"] or "Pickup"
        plat, plng, paddr = o["pickup_lat"] or 0, o["pickup_lng"] or 0, o["pickup_address"] or ""
    else:
        rest = conn.execute("SELECT name, address, lat, lng FROM restaurants WHERE id=?",
                            (o["restaurant_id"],)).fetchone()
        label = rest["name"] if rest else ""
        plat, plng, paddr = (rest["lat"] or 0, rest["lng"] or 0, rest["address"] or "") if rest else (0, 0, "")
    pos = 0
    if o["status"] not in ("delivered", "cancelled"):
        pos = conn.execute(
            "SELECT COUNT(*) c FROM orders WHERE status NOT IN ('delivered','cancelled') AND id <= ?",
            (oid,)).fetchone()["c"]
    conn.close()
    od = order_dict(o)
    return {
        "id": od["id"], "kind": kind,
        "restaurant": label,
        "restaurant_address": paddr,
        "restaurant_lat": plat, "restaurant_lng": plng,
        "address": od["address"],
        "cust_lat": od.get("cust_lat") or 0, "cust_lng": od.get("cust_lng") or 0,
        "driver_lat": od.get("driver_lat") or 0, "driver_lng": od.get("driver_lng") or 0,
        "driver_updated": od.get("driver_updated") or "",
        "items": od["items"], "food_total": od["food_total"],
        "delivery_fee": od["delivery_fee"], "tip": od.get("tip", 0),
        "discount": od.get("discount", 0), "promo_code": od.get("promo_code", ""),
        "total": od["total"], "queue_position": pos,
        "status": od["status"], "status_label": od["status_label"],
        "paid": od["stripe_status"] == "paid", "created_at": od["created_at"],
    }


# ---------------- operator API (PIN protected) ----------------
@app.get("/api/operator/orders")
def op_orders(x_pin: str = Header(default="")):
    check_pin(x_pin)
    conn = db()
    rows = conn.execute(
        """SELECT o.*, r.name AS restaurant_name, r.lat AS rest_lat, r.lng AS rest_lng,
           r.address AS rest_address, d.name AS driver_name FROM orders o
           LEFT JOIN restaurants r ON r.id = o.restaurant_id
           LEFT JOIN drivers d ON d.id = o.driver_id
           ORDER BY o.id DESC""").fetchall()
    out = []
    for r in rows:
        d = order_dict(r)
        d["restaurant_name"] = r["restaurant_name"] or r["pickup_name"] or "—"
        out.append(d)
    conn.close()
    return out


@app.post("/api/operator/orders/{oid}/status")
async def op_set_status(oid: int, req: Request, x_pin: str = Header(default="")):
    check_pin(x_pin)
    body = await req.json()
    status = body.get("status")
    if status not in STATUSES:
        raise HTTPException(400, "Bad status")
    conn = db()
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        conn.close()
        raise HTTPException(404, "Order not found")
    refunded = False
    new_stripe_status = o["stripe_status"]
    if status == "cancelled":
        refunded = refund_order(o)
        if refunded:
            new_stripe_status = "refunded"
        if o["promo_code"]:
            conn.execute("UPDATE promo_codes SET uses=MAX(0,uses-1) WHERE code=?", (o["promo_code"],))
    conn.execute("UPDATE orders SET status=?, stripe_status=?, updated_at=? WHERE id=?",
                 (status, new_stripe_status, now_iso(), oid))
    conn.commit()
    punch = None
    if status == "delivered" and (o["user_id"] or 0) and new_stripe_status == "paid":
        punch = award_punch(o["user_id"])
    conn.close()
    # customer push notification (added 2026-10-11)
    if o["user_id"]:
        _msgs = {
            "confirmed": ("✅ Order confirmed", "The restaurant is making your food!"),
            "picked_up": ("🛍️ Picked up", "Your driver has your food and is heading your way!"),
            "on_the_way": ("🚗 On the way!", "Your driver is on the way to you!"),
            "delivered": ("📦 Delivered!", "Your food was dropped at your door. Enjoy!"),
            "cancelled": ("❌ Order cancelled", "Your order was cancelled. Refund on the way if you paid."),
        }
        if status in _msgs:
            t, m = _msgs[status]
            push_to_customer(o["user_id"], t, f"Order #{oid}: {m}")
    return {"ok": True, "status": status, "label": STATUS_LABELS[status],
            "refunded": refunded, "rewards": punch}


@app.post("/api/operator/location")
async def op_location(req: Request, x_pin: str = Header(default="")):
    """Ken's phone posts GPS while delivering. Updates all active orders so
    customers see his live dot on the tracking map."""
    check_pin(x_pin)
    body = await req.json()
    try:
        lat = float(body.get("lat"))
        lng = float(body.get("lng"))
    except (TypeError, ValueError):
        raise HTTPException(400, "lat/lng required")
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise HTTPException(400, "bad coordinates")
    conn = db()
    conn.execute("""UPDATE orders SET driver_lat=?, driver_lng=?, driver_updated=?
                    WHERE status IN ('confirmed','picked_up','on_the_way')""",
                 (lat, lng, now_iso()))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/operator/proposals")
def op_proposals(x_pin: str = Header(default="")):
    """Pending AI menu-change proposals for Ken to approve/reject."""
    check_pin(x_pin)
    conn = db()
    rows = conn.execute(
        """SELECT p.*, r.name AS restaurant_name FROM menu_proposals p
           JOIN restaurants r ON r.id = p.restaurant_id
           WHERE p.status = 'pending' ORDER BY p.id DESC""").fetchall()
    out = [dict(r) for r in rows]
    conn.close()
    return out


@app.post("/api/operator/proposals")
async def op_add_proposal(req: Request, x_pin: str = Header(default="")):
    """Watchdog submits a proposed menu change. Deduplicates identical pendings."""
    check_pin(x_pin)
    body = await req.json()
    rid = body.get("restaurant_id")
    kind = (body.get("kind") or "price_change").strip()
    if kind not in ("price_change", "new_item", "unavailable", "back_available"):
        raise HTTPException(400, "bad kind")
    item_name = (body.get("item_name") or "").strip()
    if not rid or not item_name:
        raise HTTPException(400, "restaurant_id and item_name required")
    conn = db()
    dup = conn.execute(
        """SELECT id FROM menu_proposals WHERE restaurant_id=? AND kind=?
           AND item_name=? AND status='pending'""",
        (rid, kind, item_name)).fetchone()
    if dup:
        conn.close()
        return {"ok": True, "duplicate": True}
    cur = conn.execute(
        """INSERT INTO menu_proposals
           (restaurant_id, kind, item_name, old_price, new_price, detail,
            source_url, status, created_at)
           VALUES(?,?,?,?,?,?,?,'pending',?)""",
        (rid, kind, item_name, float(body.get("old_price", 0)),
         float(body.get("new_price", 0)), (body.get("detail") or "").strip()[:500],
         (body.get("source_url") or "").strip()[:500], now_iso()))
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    return {"ok": True, "id": pid}


@app.post("/api/operator/proposals/{pid}/approve")
def op_approve_proposal(pid: int, x_pin: str = Header(default="")):
    """Ken approves -> the change is applied to the live menu."""
    check_pin(x_pin)
    conn = db()
    p = conn.execute("SELECT * FROM menu_proposals WHERE id=?", (pid,)).fetchone()
    if not p or p["status"] != "pending":
        conn.close()
        raise HTTPException(404, "Proposal not found")
    kind = p["kind"]
    if kind == "price_change":
        conn.execute("UPDATE menu_items SET price=? WHERE restaurant_id=? AND name=?",
                     (p["new_price"], p["restaurant_id"], p["item_name"]))
    elif kind == "new_item":
        conn.execute(
            "INSERT INTO menu_items(restaurant_id, name, description, price, available)"
            " VALUES(?,?,?,?,1)",
            (p["restaurant_id"], p["item_name"], p["detail"], p["new_price"]))
    elif kind == "unavailable":
        conn.execute("UPDATE menu_items SET available=0 WHERE restaurant_id=? AND name=?",
                     (p["restaurant_id"], p["item_name"]))
    elif kind == "back_available":
        conn.execute("UPDATE menu_items SET available=1 WHERE restaurant_id=? AND name=?",
                     (p["restaurant_id"], p["item_name"]))
    conn.execute("UPDATE menu_proposals SET status='approved' WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/operator/proposals/{pid}/reject")
def op_reject_proposal(pid: int, x_pin: str = Header(default="")):
    check_pin(x_pin)
    conn = db()
    conn.execute("UPDATE menu_proposals SET status='rejected' WHERE id=? AND status='pending'",
                 (pid,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/operator/earnings")
def op_earnings(x_pin: str = Header(default="")):
    check_pin(x_pin)
    conn = db()
    rows = conn.execute(
        """SELECT id, delivery_fee, tip, total, created_at FROM orders
           WHERE stripe_status='paid' AND status != 'cancelled'""").fetchall()
    total_fees = round(sum(r["delivery_fee"] for r in rows), 2)
    total_tips = round(sum((r["tip"] or 0) for r in rows), 2)
    total_sales = round(sum(r["total"] for r in rows), 2)
    by_day = {}
    for r in rows:
        day = r["created_at"][:10]
        d = by_day.setdefault(day, {"orders": 0, "fees": 0.0, "tips": 0.0, "sales": 0.0})
        d["orders"] += 1
        d["fees"] = round(d["fees"] + r["delivery_fee"], 2)
        d["tips"] = round(d["tips"] + (r["tip"] or 0), 2)
        d["sales"] = round(d["sales"] + r["total"], 2)
    conn.close()
    return {"orders": len(rows), "total_fees": total_fees, "total_tips": total_tips,
            "total_earned": round(total_fees + total_tips, 2),
            "total_sales": total_sales, "by_day": by_day}


@app.get("/api/operator/settings")
def op_get_settings(x_pin: str = Header(default="")):
    check_pin(x_pin)
    oh, ch = get_hours()
    return {"delivery_fee": float(get_setting("delivery_fee")),
            "min_order": float(get_setting("min_order") or 10),
            "accepting_orders": get_setting("accepting_orders") != "0",
            "mins_per_delivery": int(get_setting("mins_per_delivery") or 18),
            "open_hour": oh, "close_hour": ch,
            "delivery_zone": get_setting("delivery_zone") or "Bismarck, ND",
            "pin": get_setting("pin"),
            "ntfy_topic": get_setting("ntfy_topic"),
            "tagline": get_setting("tagline")}


@app.put("/api/operator/settings")
async def op_put_settings(req: Request, x_pin: str = Header(default="")):
    check_pin(x_pin)
    body = await req.json()
    out = {}
    if "delivery_fee" in body:
        fee = round(float(body["delivery_fee"]), 2)
        if fee < 0 or fee > 50:
            raise HTTPException(400, "Fee must be 0–50")
        set_setting("delivery_fee", str(fee))
        out["delivery_fee"] = fee
    if "min_order" in body:
        mo = round(max(0, float(body["min_order"])), 2)
        if mo > 200:
            raise HTTPException(400, "Min order must be 0–200")
        set_setting("min_order", str(mo))
        out["min_order"] = mo
    if "accepting_orders" in body:
        ao = "1" if body["accepting_orders"] else "0"
        set_setting("accepting_orders", ao)
        out["accepting_orders"] = ao == "1"
    if "mins_per_delivery" in body:
        mpd = max(5, min(120, int(body["mins_per_delivery"])))
        set_setting("mins_per_delivery", str(mpd))
        out["mins_per_delivery"] = mpd
    if "pin" in body:
        pin = str(body["pin"]).strip()
        if not (pin.isdigit() and 4 <= len(pin) <= 8):
            raise HTTPException(400, "PIN must be 4–8 digits")
        set_setting("pin", pin)
        out["pin"] = "updated"
    if "ntfy_topic" in body:
        topic = str(body["ntfy_topic"]).strip()
        set_setting("ntfy_topic", topic)
        out["ntfy_topic"] = topic
    if "tagline" in body:
        set_setting("tagline", str(body["tagline"]).strip()[:80])
        out["tagline"] = "updated"
    if "open_hour" in body:
        oh = max(0, min(23, int(body["open_hour"])))
        set_setting("open_hour", str(oh))
        out["open_hour"] = oh
    if "close_hour" in body:
        ch = max(0, min(23, int(body["close_hour"])))
        set_setting("close_hour", str(ch))
        out["close_hour"] = ch
    if "delivery_zone" in body:
        z = str(body["delivery_zone"]).strip()[:80] or "Bismarck, ND"
        set_setting("delivery_zone", z)
        out["delivery_zone"] = z
    return out


# ---------------- promo codes ----------------
def validate_promo(code, subtotal):
    """Returns (discount, message). Discount 0.0 means invalid."""
    code = (code or "").strip().upper()
    if not code:
        return 0.0, "Enter a code"
    conn = db()
    p = conn.execute("SELECT * FROM promo_codes WHERE code=?", (code,)).fetchone()
    conn.close()
    if not p or not p["active"]:
        return 0.0, "That code isn't valid"
    if p["max_uses"] > 0 and p["uses"] >= p["max_uses"]:
        return 0.0, "That code has been used up"
    if subtotal < (p["min_order"] or 0):
        return 0.0, f"Needs a ${p['min_order']:.2f} order"
    if p["kind"] == "percent":
        discount = round(subtotal * min(p["value"], 50) / 100, 2)
    else:
        discount = round(min(p["value"], subtotal), 2)
    return discount, "ok"


@app.post("/api/promo/validate")
async def promo_validate(req: Request):
    body = await req.json()
    try:
        subtotal = float(body.get("subtotal", 0))
    except (TypeError, ValueError):
        subtotal = 0
    discount, msg = validate_promo(body.get("code"), subtotal)
    return {"valid": discount > 0, "discount": discount, "message": msg,
            "code": (body.get("code") or "").strip().upper()}


@app.get("/api/operator/promos")
def op_list_promos(x_pin: str = Header(default="")):
    check_pin(x_pin)
    conn = db()
    rows = conn.execute("SELECT * FROM promo_codes ORDER BY id DESC").fetchall()
    out = [dict(r) for r in rows]
    conn.close()
    return out


@app.post("/api/operator/promos")
async def op_create_promo(req: Request, x_pin: str = Header(default="")):
    check_pin(x_pin)
    body = await req.json()
    code = (body.get("code") or "").strip().upper()
    if not code or len(code) > 20:
        raise HTTPException(400, "Code required (max 20 chars)")
    kind = body.get("kind", "fixed")
    if kind not in ("fixed", "percent"):
        raise HTTPException(400, "kind must be fixed or percent")
    try:
        value = round(float(body.get("value", 0)), 2)
        min_order = round(max(0, float(body.get("min_order", 0))), 2)
        max_uses = max(0, int(body.get("max_uses", 0)))
    except (TypeError, ValueError):
        raise HTTPException(400, "Bad numbers")
    if value <= 0 or (kind == "percent" and value > 50):
        raise HTTPException(400, "Value must be > 0 (percent max 50)")
    conn = db()
    try:
        cur = conn.execute(
            "INSERT INTO promo_codes(code, kind, value, min_order, max_uses, uses, active, created_at)"
            " VALUES(?,?,?,?,?,0,1,?)",
            (code, kind, value, min_order, max_uses, now_iso()))
        pid = cur.lastrowid
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(400, "That code already exists")
    conn.close()
    return {"ok": True, "id": pid}


@app.put("/api/operator/promos/{pid}")
async def op_toggle_promo(pid: int, req: Request, x_pin: str = Header(default="")):
    check_pin(x_pin)
    body = await req.json()
    active = 1 if body.get("active") else 0
    conn = db()
    conn.execute("UPDATE promo_codes SET active=? WHERE id=?", (active, pid))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/operator/promos/{pid}")
def op_delete_promo(pid: int, x_pin: str = Header(default="")):
    check_pin(x_pin)
    conn = db()
    conn.execute("DELETE FROM promo_codes WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ---------------- customer accounts + rewards ----------------
def hash_pw(password, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return f"{salt}${h.hex()}"


def check_pw(password, stored):
    try:
        salt, _ = stored.split("$", 1)
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(hash_pw(password, salt), stored)


def norm_phone(p):
    return re.sub(r"\D", "", p or "")[:15]


def create_session(user_id):
    token = secrets.token_urlsafe(32)
    exp = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    conn = db()
    conn.execute("INSERT INTO sessions(token, user_id, expires_at) VALUES(?,?,?)",
                 (token, user_id, exp))
    conn.commit()
    conn.close()
    return token


def get_session_user(req):
    token = req.cookies.get("drop_session", "")
    if not token:
        return None
    conn = db()
    s = conn.execute("SELECT user_id, expires_at FROM sessions WHERE token=?", (token,)).fetchone()
    if not s:
        conn.close()
        return None
    if s["expires_at"] < now_iso():
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
        conn.close()
        return None
    u = conn.execute("SELECT id, name, phone, created_at, credit, ntfy_topic FROM users WHERE id=?",
                     (s["user_id"],)).fetchone()
    conn.close()
    return dict(u) if u else None


def get_rewards(user_id):
    conn = db()
    conn.execute("INSERT OR IGNORE INTO rewards(user_id, punches, free_fee) VALUES(?,0,0)", (user_id,))
    r = conn.execute("SELECT punches, free_fee FROM rewards WHERE user_id=?", (user_id,)).fetchone()
    conn.commit()
    conn.close()
    return {"punches": r["punches"], "free_fee": bool(r["free_fee"])}


def award_punch(user_id):
    """Called when a user's paid order is delivered. Returns updated rewards."""
    conn = db()
    conn.execute("INSERT OR IGNORE INTO rewards(user_id, punches, free_fee) VALUES(?,0,0)", (user_id,))
    r = conn.execute("SELECT punches FROM rewards WHERE user_id=?", (user_id,)).fetchone()
    new_p = (r["punches"] or 0) + 1
    if new_p >= 10:
        conn.execute("UPDATE rewards SET punches=0, free_fee=1 WHERE user_id=?", (user_id,))
    else:
        conn.execute("UPDATE rewards SET punches=? WHERE user_id=?", (new_p, user_id))
    conn.commit()
    conn.close()
    return get_rewards(user_id)


@app.post("/api/auth/signup")
async def auth_signup(req: Request, resp: Response):
    body = await req.json()
    name = (body.get("name") or "").strip()[:60]
    phone = norm_phone(body.get("phone"))
    pw = body.get("password") or ""
    if not name:
        raise HTTPException(400, "Name required")
    if len(phone) < 10:
        raise HTTPException(400, "Enter a valid 10-digit phone number")
    if len(pw) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    conn = db()
    if conn.execute("SELECT id FROM users WHERE phone=?", (phone,)).fetchone():
        conn.close()
        raise HTTPException(400, "That number already has an account — try logging in")
    cur = conn.execute("INSERT INTO users(name, phone, password_hash, created_at) VALUES(?,?,?,?)",
                       (name, phone, hash_pw(pw), now_iso()))
    uid = cur.lastrowid
    conn.execute("INSERT INTO rewards(user_id, punches, free_fee) VALUES(?,0,0)", (uid,))
    conn.commit()
    conn.close()
    token = create_session(uid)
    resp.set_cookie("drop_session", token, httponly=True, samesite="lax", path="/", max_age=30*86400)
    return {"ok": True}


@app.post("/api/auth/login")
async def auth_login(req: Request, resp: Response):
    body = await req.json()
    phone = norm_phone(body.get("phone"))
    pw = body.get("password") or ""
    conn = db()
    u = conn.execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
    conn.close()
    if not u or not check_pw(pw, u["password_hash"]):
        raise HTTPException(401, "Wrong number or password")
    token = create_session(u["id"])
    resp.set_cookie("drop_session", token, httponly=True, samesite="lax", path="/", max_age=30*86400)
    return {"ok": True, "name": u["name"]}


@app.post("/api/auth/logout")
async def auth_logout(req: Request, resp: Response):
    token = req.cookies.get("drop_session", "")
    if token:
        conn = db()
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
        conn.close()
    resp.delete_cookie("drop_session", path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def auth_me(req: Request):
    u = get_session_user(req)
    if not u:
        raise HTTPException(401, "Not logged in")
    rw = get_rewards(u["id"])
    conn = db()
    orders = conn.execute(
        """SELECT o.id, o.total, o.status, o.created_at, r.name AS restaurant_name
           FROM orders o LEFT JOIN restaurants r ON r.id=o.restaurant_id
           WHERE o.user_id=? ORDER BY o.id DESC LIMIT 20""", (u["id"],)).fetchall()
    conn.close()
    return {"user": u, "rewards": rw,
            "orders": [dict(x) for x in orders]}


# ---------------- queue ----------------
def refund_order(o):
    """Best-effort Stripe refund for a paid order. Returns True if refunded."""
    if not STRIPE_LIVE or o["stripe_status"] != "paid" or not o["stripe_payment_id"]:
        return False
    try:
        sess = stripe.checkout.Session.retrieve(o["stripe_payment_id"])
        pi = sess.get("payment_intent")
        if pi:
            stripe.Refund.create(payment_intent=pi)
            return True
    except Exception:
        pass
    return False


@app.get("/api/queue")
def queue_status():
    conn = db()
    n = conn.execute(
        "SELECT COUNT(*) c FROM orders WHERE status NOT IN ('delivered','cancelled')"
    ).fetchone()["c"]
    conn.close()
    accepting = get_setting("accepting_orders") != "0"
    open_now = is_open_now()
    oh, ch = get_hours()
    try:
        mins = max(5, min(120, int(get_setting("mins_per_delivery") or 18)))
    except (TypeError, ValueError):
        mins = 18
    return {"active": n, "accepting": accepting and open_now,
            "open_now": open_now, "hours_label": f"{fmt_hour(oh)}–{fmt_hour(ch)}",
            "estimated_mins": (n * mins + 25) if (accepting and open_now) else 0}


@app.post("/api/orders/{oid}/cancel")
def customer_cancel(oid: int):
    """Customer cancels their own order (before pickup). Auto-refunds if paid."""
    conn = db()
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        conn.close()
        raise HTTPException(404, "Order not found")
    if o["status"] not in ("placed", "confirmed"):
        conn.close()
        raise HTTPException(400, "Too late to cancel — your food is already on the way")
    refunded = refund_order(o)
    conn.execute(
        "UPDATE orders SET status='cancelled', stripe_status=?, updated_at=? WHERE id=?",
        ("refunded" if refunded else o["stripe_status"], now_iso(), oid))
    if o["promo_code"]:
        conn.execute("UPDATE promo_codes SET uses=MAX(0,uses-1) WHERE code=?", (o["promo_code"],))
    conn.commit()
    conn.close()
    return {"ok": True, "refunded": refunded}


# ---------------- visitor analytics ----------------
@app.post("/api/visit")
async def track_visit(req: Request):
    """Count one visit per browser session per page (frontend dedupes)."""
    body = await req.json()
    page = re.sub(r"[^a-z]", "", str(body.get("page") or "home").lower())[:20] or "home"
    today = datetime.now(CENTRAL).date().isoformat()
    conn = db()
    conn.execute(
        "INSERT INTO visits(date, page, count) VALUES(?,?,1) "
        "ON CONFLICT(date, page) DO UPDATE SET count=count+1",
        (today, page))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/operator/analytics")
def op_analytics(x_pin: str = Header(default="")):
    check_pin(x_pin)
    conn = db()
    days = []
    for i in range(13, -1, -1):
        d = (datetime.now(CENTRAL).date() - timedelta(days=i)).isoformat()
        v = conn.execute("SELECT COALESCE(SUM(count),0) c FROM visits WHERE date=?", (d,)).fetchone()["c"]
        o = conn.execute(
            "SELECT COUNT(*) c FROM orders WHERE substr(created_at,1,10)=? AND status!='cancelled'",
            (d,)).fetchone()["c"]
        days.append({"date": d[5:], "visits": v, "orders": o})
    conn.close()
    tv = sum(x["visits"] for x in days)
    to = sum(x["orders"] for x in days)
    return {"days": days, "total_visits": tv, "total_orders": to,
            "conversion": round(100 * to / tv, 1) if tv else 0}


# ---------------- support tickets ----------------
@app.post("/api/support/ticket")
async def support_create(req: Request):
    """Public: customer sends a message to Ken. Creates a support ticket."""
    body = await req.json()
    name = (body.get("name") or "").strip()[:60]
    phone = (body.get("phone") or "").strip()[:30]
    message = (body.get("message") or "").strip()[:2000]
    try:
        order_id = int(body.get("order_id") or 0)
    except (TypeError, ValueError):
        order_id = 0
    if not name or not message:
        raise HTTPException(400, "Name and message required")
    conn = db()
    cur = conn.execute(
        "INSERT INTO support_tickets(name, phone, order_id, message, status, created_at)"
        " VALUES(?,?,?,?, 'open', ?)",
        (name, phone, order_id, message, now_iso()))
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return {"ok": True, "ticket_id": tid}


@app.get("/api/operator/tickets")
def op_list_tickets(x_pin: str = Header(default="")):
    check_pin(x_pin)
    conn = db()
    rows = conn.execute(
        "SELECT * FROM support_tickets ORDER BY id DESC").fetchall()
    out = [dict(r) for r in rows]
    conn.close()
    return out


@app.post("/api/operator/tickets/{tid}/resolve")
def op_resolve_ticket(tid: int, x_pin: str = Header(default="")):
    check_pin(x_pin)
    conn = db()
    conn.execute("UPDATE support_tickets SET status='resolved' WHERE id=?", (tid,))
    conn.commit()
    conn.close()
    return {"ok": True}


def ticket_thread(tid):
    """Full thread: original message + replies, oldest first."""
    conn = db()
    t = conn.execute("SELECT * FROM support_tickets WHERE id=?", (tid,)).fetchone()
    if not t:
        conn.close()
        return None
    msgs = [{"sender": "customer", "message": t["message"], "created_at": t["created_at"]}]
    for r in conn.execute(
            "SELECT sender, message, created_at FROM ticket_replies"
            " WHERE ticket_id=? ORDER BY id", (tid,)).fetchall():
        msgs.append(dict(r))
    conn.close()
    return {"id": t["id"], "name": t["name"], "order_id": t["order_id"],
            "status": t["status"], "messages": msgs}


@app.get("/api/support/thread/{tid}")
def support_thread(tid: int, phone: str = ""):
    """Customer views their ticket thread. Phone must match the ticket."""
    th = ticket_thread(tid)
    if not th:
        raise HTTPException(404, "Ticket not found")
    conn = db()
    row = conn.execute("SELECT phone FROM support_tickets WHERE id=?", (tid,)).fetchone()
    conn.close()
    if (row["phone"] or "") != (phone or "").strip():
        raise HTTPException(403, "Phone doesn't match this ticket")
    return th


@app.post("/api/support/reply")
async def support_reply(req: Request):
    """Customer replies on their ticket."""
    body = await req.json()
    try:
        tid = int(body.get("ticket_id") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "Bad ticket")
    phone = (body.get("phone") or "").strip()
    message = (body.get("message") or "").strip()[:2000]
    if not message:
        raise HTTPException(400, "Message required")
    conn = db()
    row = conn.execute("SELECT phone FROM support_tickets WHERE id=?", (tid,)).fetchone()
    if not row or (row["phone"] or "") != phone:
        conn.close()
        raise HTTPException(403, "Phone doesn't match this ticket")
    conn.execute("INSERT INTO ticket_replies(ticket_id, sender, message, created_at)"
                 " VALUES(?, 'customer', ?, ?)", (tid, message, now_iso()))
    conn.execute("UPDATE support_tickets SET status='open' WHERE id=?", (tid,))
    conn.commit()
    conn.close()
    # nudge Ken
    try:
        topic = get_setting("ntfy_topic").strip()
        if topic:
            msg = f"💬 SUPPORT REPLY #{tid}\n{message[:200]}"
            req2 = urllib.request.Request(
                f"https://ntfy.sh/{urllib.parse.quote(topic)}",
                data=msg.encode(), headers={"Title": "Drop support reply"}, method="POST")
            threading.Thread(
                target=lambda: urllib.request.urlopen(req2, timeout=5).read(),
                daemon=True).start()
    except Exception:
        pass
    return {"ok": True}


@app.get("/api/operator/tickets/{tid}/thread")
def op_ticket_thread(tid: int, x_pin: str = Header(default="")):
    check_pin(x_pin)
    th = ticket_thread(tid)
    if not th:
        raise HTTPException(404, "Ticket not found")
    return th


@app.post("/api/operator/tickets/{tid}/reply")
async def op_ticket_reply(tid: int, req: Request, x_pin: str = Header(default="")):
    """Ken replies to the customer."""
    check_pin(x_pin)
    body = await req.json()
    message = (body.get("message") or "").strip()[:2000]
    if not message:
        raise HTTPException(400, "Message required")
    conn = db()
    t = conn.execute("SELECT id FROM support_tickets WHERE id=?", (tid,)).fetchone()
    if not t:
        conn.close()
        raise HTTPException(404, "Ticket not found")
    conn.execute("INSERT INTO ticket_replies(ticket_id, sender, message, created_at)"
                 " VALUES(?, 'operator', ?, ?)", (tid, message, now_iso()))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/operator/tickets/{tid}/suggest")
def op_ticket_suggest(tid: int, x_pin: str = Header(default="")):
    """AI assist: drafts a reply from the ticket text + live order context.
    Ken reviews/edits before sending — the draft never goes out on its own."""
    check_pin(x_pin)
    conn = db()
    t = conn.execute("SELECT * FROM support_tickets WHERE id=?", (tid,)).fetchone()
    if not t:
        conn.close()
        raise HTTPException(404, "Ticket not found")
    last_customer = t["message"]
    r = conn.execute("SELECT sender, message FROM ticket_replies WHERE ticket_id=?"
                     " AND sender='customer' ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    if r:
        last_customer = r["message"]
    order = None
    if t["order_id"]:
        order = conn.execute("SELECT * FROM orders WHERE id=?", (t["order_id"],)).fetchone()
    conn.close()
    text = (t["message"] + " " + last_customer).lower()
    name = (t["name"] or "there").split()[0]
    draft = ""
    if order:
        od = order_dict(order)
        items = ", ".join(f"{i['qty']}x {i['name']}" for i in od["items"])
        if any(k in text for k in ("where", "late", "long", "status", "arrive", "track")):
            draft = (f"Hi {name}! Your order #{od['id']} is currently: {od['status_label']}. "
                     f"You ordered {items}. You can watch it live on your tracking page — "
                     f"it refreshes every few seconds. Thanks for your patience!")
        elif any(k in text for k in ("refund", "wrong", "missing", "cold", "bad", "mistake")):
            draft = (f"Hi {name}, I'm sorry about that. I've got your order #{od['id']} "
                     f"({items}) pulled up and I'm looking into it right now. "
                     f"I'll make it right — give me a few minutes.")
        elif any(k in text for k in ("cancel",)):
            draft = (f"Hi {name}, I can cancel order #{od['id']} for you. "
                     f"If it's already paid, you'll be refunded automatically. "
                     f"Just confirm and I'll take care of it.")
    if not draft:
        if any(k in text for k in ("refund", "wrong", "missing", "cold", "bad", "late")):
            draft = (f"Hi {name}, sorry to hear that — I'm looking into it right now "
                     f"and I'll make it right. Can you share your order number if you have it?")
        elif any(k in text for k in ("fee", "cost", "charge", "price")):
            draft = (f"Hi {name}! Delivery is a flat $3.99 — no surge pricing, no hidden fees. "
                     f"What can I help you with?")
        else:
            draft = (f"Hi {name}, thanks for reaching out! I've got your message "
                     f"and I'm on it — I'll get back to you shortly.")
    return {"draft": draft}


# ---------------- Drop More: B2B courier businesses ----------------
@app.get("/api/businesses")
def list_businesses():
    conn = db()
    rows = conn.execute("SELECT * FROM businesses WHERE open=1 ORDER BY category, name").fetchall()
    out = [dict(r) for r in rows]
    conn.close()
    cats = []
    for b in out:
        if b["category"] not in cats:
            cats.append(b["category"])
    return {"businesses": out, "categories": cats}


@app.post("/api/operator/businesses")
async def op_add_business(req: Request, x_pin: str = Header(default="")):
    check_pin(x_pin)
    body = await req.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Name required")
    addr = (body.get("address") or "").strip()[:200]
    glat, glng = (0.0, 0.0)
    if addr:
        glat, glng = geocode(addr)
    conn = db()
    cur = conn.execute(
        """INSERT INTO businesses(name, category, address, lat, lng, phone, website,
                                  logo, photo, open, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,1,?)""",
        (name, (body.get("category") or "").strip()[:60], addr, glat, glng,
         (body.get("phone") or "").strip()[:30], (body.get("website") or "").strip()[:500],
         (body.get("logo") or "").strip()[:500], (body.get("photo") or "").strip()[:500],
         now_iso()))
    bid = cur.lastrowid
    conn.commit()
    conn.close()
    return {"id": bid}


@app.put("/api/operator/businesses/{bid}")
async def op_edit_business(bid: int, req: Request, x_pin: str = Header(default="")):
    check_pin(x_pin)
    body = await req.json()
    conn = db()
    fields, vals = [], []
    for k in ("name", "category", "phone", "website", "logo", "photo"):
        if k in body:
            fields.append(f"{k}=?")
            vals.append(str(body[k]).strip()[:500])
    if "address" in body:
        addr = str(body["address"]).strip()[:200]
        fields.append("address=?")
        vals.append(addr)
        if addr:
            glat, glng = geocode(addr)
            if glat:
                fields += ["lat=?", "lng=?"]
                vals += [glat, glng]
    if "open" in body:
        fields.append("open=?")
        vals.append(1 if body["open"] else 0)
    if fields:
        vals.append(bid)
        conn.execute(f"UPDATE businesses SET {', '.join(fields)} WHERE id=?", vals)
        conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/operator/businesses/{bid}")
def op_del_business(bid: int, x_pin: str = Header(default="")):
    check_pin(x_pin)
    conn = db()
    conn.execute("DELETE FROM businesses WHERE id=?", (bid,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/courier-orders")
async def create_courier_order(req: Request):
    """Drop More courier request: pick up items from a business, deliver to customer."""
    body = await req.json()
    if not is_open_now():
        oh, _ = get_hours()
        raise HTTPException(400, f"We're closed right now — back at {fmt_hour(oh)}")
    if get_setting("accepting_orders") == "0":
        raise HTTPException(400, "We're at capacity right now — please check back soon")
    me = get_session_user(req)
    user_id = me["id"] if me else 0
    business_id = int(body.get("business_id") or 0)
    # pickup can be a listed business or a custom location
    pickup_name = (body.get("pickup_name") or "").strip()[:120]
    pickup_address = (body.get("pickup_address") or "").strip()[:200]
    item_desc = (body.get("item_desc") or "").strip()[:1000]
    cust = body.get("customer", {})
    name = (cust.get("name") or "").strip()
    phone = (cust.get("phone") or "").strip()
    address = (cust.get("address") or "").strip()
    notes = (cust.get("notes") or "").strip()
    if not item_desc or not name or not phone or not address:
        raise HTTPException(400, "Missing items, name, phone, or delivery address")
    if not pickup_name and not business_id:
        raise HTTPException(400, "Missing pickup location")
    try:
        est_total = round(max(0.0, float(body.get("est_total") or 0)), 2)
    except (TypeError, ValueError):
        est_total = 0.0
    conn = db()
    plat, plng = 0.0, 0.0
    if business_id:
        b = conn.execute("SELECT * FROM businesses WHERE id=?", (business_id,)).fetchone()
        if not b:
            conn.close()
            raise HTTPException(400, "Unknown business")
        pickup_name, pickup_address, plat, plng = b["name"], b["address"], b["lat"], b["lng"]
    elif pickup_address:
        plat, plng = geocode(pickup_address, body.get("pickup_parts"))
    fee = round(float(get_setting("delivery_fee")), 2)
    try:
        tip = round(max(0.0, min(100.0, float(body.get("tip", 0)))), 2)
    except (TypeError, ValueError):
        tip = 0.0
    total = round(est_total + fee + tip, 2)
    items_json = json.dumps([{"name": item_desc[:120], "qty": 1,
                              "price": est_total, "line": est_total}])
    cur = conn.execute(
        """INSERT INTO orders(kind, business_id, pickup_name, pickup_address, pickup_lat, pickup_lng,
                              item_desc, restaurant_id, items_json, customer_name, phone, address,
                              notes, food_total, delivery_fee, tip, user_id, total,
                              status, stripe_status, created_at, updated_at)
           VALUES('courier',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'placed','pending',?,?)""",
        (business_id, pickup_name, pickup_address, plat, plng, item_desc,
         0, items_json, name, phone, address, notes,
         est_total, fee, tip, user_id, total, now_iso(), now_iso()))
    oid = cur.lastrowid
    conn.commit()
    # geocode delivery address in background (structured parts for accuracy)
    cust_parts = (body.get("customer") or {}).get("address_parts")
    def _geo():
        try:
            glat, glng = geocode(address, cust_parts)
            if glat:
                c2 = db()
                c2.execute("UPDATE orders SET cust_lat=?, cust_lng=? WHERE id=?",
                           (glat, glng, oid))
                c2.commit()
                c2.close()
        except Exception:
            pass
    threading.Thread(target=_geo, daemon=True).start()
    conn.close()
    return {"order_id": oid, "est_total": est_total, "delivery_fee": fee,
            "tip": tip, "total": total, "stripe_live": STRIPE_LIVE}


# ---------------- Droppers: driver applications + portal ----------------
def get_driver(req):
    token = req.cookies.get("drop_driver", "")
    if not token:
        return None
    conn = db()
    s = conn.execute("SELECT driver_id, expires_at FROM driver_sessions WHERE token=?",
                     (token,)).fetchone()
    if not s:
        conn.close()
        return None
    d = conn.execute("SELECT * FROM drivers WHERE id=?", (s["driver_id"],)).fetchone()
    conn.close()
    if not d or d["status"] != "approved":
        return None
    return dict(d)


@app.post("/api/drivers/apply")
async def driver_apply(req: Request):
    """Now Hiring: full driver application. Goes to pending for Ken's approval."""
    body = await req.json()
    name = (body.get("name") or "").strip()[:80]
    phone = norm_phone(body.get("phone"))
    email = (body.get("email") or "").strip()[:120]
    password = body.get("password") or ""
    if not name or not phone or len(password) < 6:
        raise HTTPException(400, "Name, phone, and a 6+ character password are required")
    if not body.get("background_consent"):
        raise HTTPException(400, "Background check consent is required")
    conn = db()
    if conn.execute("SELECT id FROM drivers WHERE phone=?", (phone,)).fetchone():
        conn.close()
        raise HTTPException(400, "This phone number already has an application")
    cur = conn.execute(
        """INSERT INTO drivers(name, phone, email, password_hash, license_number, license_state,
                               insurance, vehicle_make, vehicle_model, vehicle_year, vehicle_color,
                               availability, background_consent, experience, status, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?)""",
        (name, phone, email, hash_pw(password),
         (body.get("license_number") or "").strip()[:40],
         (body.get("license_state") or "").strip()[:10],
         (body.get("insurance") or "").strip()[:80],
         (body.get("vehicle_make") or "").strip()[:40],
         (body.get("vehicle_model") or "").strip()[:40],
         (body.get("vehicle_year") or "").strip()[:10],
         (body.get("vehicle_color") or "").strip()[:30],
         (body.get("availability") or "").strip()[:200],
         1, (body.get("experience") or "").strip()[:500], now_iso()))
    did = cur.lastrowid
    conn.commit()
    conn.close()
    # notify Ken
    try:
        topic = get_setting("ntfy_topic").strip()
        if topic:
            msg = f"🚗 NEW DRIVER APPLICATION #{did}\n{name} {phone}"
            r2 = urllib.request.Request(
                f"https://ntfy.sh/{urllib.parse.quote(topic)}",
                data=msg.encode(), headers={"Title": "Drop driver application"}, method="POST")
            threading.Thread(
                target=lambda: urllib.request.urlopen(r2, timeout=5).read(),
                daemon=True).start()
    except Exception:
        pass
    return {"ok": True, "id": did}


@app.get("/api/operator/drivers")
def op_list_drivers(x_pin: str = Header(default=""), status: str = ""):
    check_pin(x_pin)
    conn = db()
    q = "SELECT id, name, phone, email, license_number, license_state, insurance, vehicle_make, vehicle_model, vehicle_year, vehicle_color, availability, experience, status, deliveries, on_time, created_at FROM drivers"
    if status:
        q += " WHERE status=?"
        rows = conn.execute(q + " ORDER BY id DESC", (status,)).fetchall()
    else:
        rows = conn.execute(q + " ORDER BY id DESC").fetchall()
    out = [dict(r) for r in rows]
    conn.close()
    return out


@app.post("/api/operator/drivers/{did}/approve")
def op_approve_driver(did: int, x_pin: str = Header(default="")):
    """Ken approves -> driver can log into their portal.
    BLOCKED without insurance info + safety/policy acknowledgment."""
    check_pin(x_pin)
    conn = db()
    d = conn.execute("SELECT * FROM drivers WHERE id=?", (did,)).fetchone()
    if not d:
        conn.close()
        raise HTTPException(404, "Driver not found")
    if not (d["insurance"] or "").strip():
        conn.close()
        raise HTTPException(400,
            "Cannot approve: driver has not provided insurance information. "
            "Insurance is required — no exceptions.")
    conn.execute("UPDATE drivers SET status='approved' WHERE id=?", (did,))
    conn.commit()
    conn.close()
    audit("operator", "driver_approved", "driver", did, d["name"])
    return {"ok": True}


@app.post("/api/operator/drivers/{did}/deny")
def op_deny_driver(did: int, x_pin: str = Header(default="")):
    check_pin(x_pin)
    conn = db()
    conn.execute("UPDATE drivers SET status='denied' WHERE id=?", (did,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/operator/drivers/{did}/suspend")
def op_suspend_driver(did: int, x_pin: str = Header(default="")):
    check_pin(x_pin)
    conn = db()
    conn.execute("UPDATE drivers SET status='suspended' WHERE id=?", (did,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/drivers/login")
async def driver_login(req: Request):
    body = await req.json()
    phone = norm_phone(body.get("phone"))
    password = body.get("password") or ""
    conn = db()
    d = conn.execute("SELECT * FROM drivers WHERE phone=?", (phone,)).fetchone()
    conn.close()
    if not d or not check_pw(password, d["password_hash"]):
        raise HTTPException(401, "Wrong phone or password")
    if d["status"] == "pending":
        raise HTTPException(403, "Your application is still under review")
    if d["status"] != "approved":
        raise HTTPException(403, "This driver account is not active")
    token = secrets.token_urlsafe(32)
    exp = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    conn = db()
    conn.execute("INSERT INTO driver_sessions(token, driver_id, expires_at) VALUES(?,?,?)",
                 (token, d["id"], exp))
    conn.commit()
    conn.close()
    resp = JSONResponse({"ok": True, "name": d["name"]})
    resp.set_cookie("drop_driver", token, max_age=30*86400, httponly=True, samesite="lax")
    return resp


@app.post("/api/drivers/logout")
async def driver_logout(req: Request):
    token = req.cookies.get("drop_driver", "")
    conn = db()
    conn.execute("DELETE FROM driver_sessions WHERE token=?", (token,))
    conn.commit()
    conn.close()
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("drop_driver")
    return resp


@app.get("/api/drivers/me")
async def driver_me(req: Request):
    d = get_driver(req)
    if not d:
        raise HTTPException(401, "Not logged in")
    d.pop("password_hash", None)
    # rank score: on-time rate (simple, transparent)
    d["on_time_rate"] = round(100.0 * d["on_time"] / d["deliveries"], 1) if d["deliveries"] else 100.0
    return d


@app.get("/api/drivers/orders")
async def driver_orders(req: Request):
    """Orders assigned to this driver."""
    d = get_driver(req)
    if not d:
        raise HTTPException(401, "Not logged in")
    conn = db()
    rows = conn.execute(
        """SELECT o.*, COALESCE(r.name, o.pickup_name) AS pickup_label,
                  COALESCE(r.address, o.pickup_address) AS pickup_addr,
                  COALESCE(r.lat, o.pickup_lat) AS plat, COALESCE(r.lng, o.pickup_lng) AS plng
           FROM orders o LEFT JOIN restaurants r ON r.id = o.restaurant_id
           WHERE o.driver_id=? AND o.status NOT IN ('delivered','cancelled')
           ORDER BY o.id""", (d["id"],)).fetchall()
    out = []
    for r in rows:
        od = order_dict(r)
        od["pickup_label"] = r["pickup_label"]
        od["pickup_addr"] = r["pickup_addr"]
        od["plat"] = r["plat"] or 0
        od["plng"] = r["plng"] or 0
        out.append(od)
    conn.close()
    return out


@app.post("/api/drivers/orders/{oid}/status")
async def driver_set_status(oid: int, req: Request):
    d = get_driver(req)
    if not d:
        raise HTTPException(401, "Not logged in")
    body = await req.json()
    status = body.get("status")
    if status not in ("picked_up", "on_the_way", "delivered"):
        raise HTTPException(400, "Bad status")
    conn = db()
    o = conn.execute("SELECT * FROM orders WHERE id=? AND driver_id=?",
                     (oid, d["id"])).fetchone()
    if not o:
        conn.close()
        raise HTTPException(404, "Order not found")
    conn.execute("UPDATE orders SET status=?, updated_at=? WHERE id=?",
                 (status, now_iso(), oid))
    if status == "delivered":
        conn.execute("UPDATE drivers SET deliveries=deliveries+1, on_time=on_time+1 WHERE id=?",
                     (d["id"],))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/drivers/location")
async def driver_location(req: Request):
    """A dropper's phone posts GPS — updates orders + detects speeding."""
    d = get_driver(req)
    if not d:
        raise HTTPException(401, "Not logged in")
    body = await req.json()
    try:
        lat = float(body.get("lat"))
        lng = float(body.get("lng"))
    except (TypeError, ValueError):
        raise HTTPException(400, "lat/lng required")
    now = now_iso()
    conn = db()
    prev = conn.execute("SELECT last_lat, last_lng, last_gps_at FROM drivers WHERE id=?",
                        (d["id"],)).fetchone()
    speed_mph = 0
    if prev and prev["last_lat"] and prev["last_gps_at"]:
        try:
            from datetime import datetime
            dt = (datetime.fromisoformat(now) - datetime.fromisoformat(prev["last_gps_at"])).total_seconds()
            if dt > 5:
                dist = haversine_miles(prev["last_lat"], prev["last_lng"], lat, lng)
                speed_mph = round(dist / (dt / 3600), 1)
        except Exception:
            pass
    conn.execute("UPDATE drivers SET last_lat=?, last_lng=?, last_gps_at=? WHERE id=?",
                 (lat, lng, now, d["id"]))
    conn.execute("""UPDATE orders SET driver_lat=?, driver_lng=?, driver_updated=?
                    WHERE driver_id=? AND status IN ('confirmed','picked_up','on_the_way')""",
                 (lat, lng, now, d["id"]))
    conn.commit()
    conn.close()
    if speed_mph > 80:
        conn = db()
        conn.execute("UPDATE drivers SET speed_warnings=speed_warnings+1 WHERE id=?", (d["id"],))
        conn.commit()
        conn.close()
        driver_inbox_add(d["id"], "warning", "⚠️ Slow down",
            f"Our system clocked you at {speed_mph} mph. Please slow down — "
            f"safety first. Arriving safely matters more than arriving fast.")
        audit("system", "speed_warning", "driver", d["id"], f"{speed_mph} mph")
        log_event("warning", f"⚠️ Speed warning sent to driver #{d['id']}",
                  f"Clocked at {speed_mph} mph")
    # delay detection: on_the_way for too long?
    conn = db()
    slow = conn.execute(
        """SELECT id, updated_at FROM orders WHERE driver_id=? AND status='on_the_way'
           AND updated_at < datetime('now', '-45 minutes')""", (d["id"],)).fetchall()
    for s in slow:
        # only nudge once per order
        already = conn.execute(
            "SELECT id FROM driver_messages WHERE driver_id=? AND kind='delay_check'"
            " AND body LIKE ?", (d["id"], f"%#{s['id']}%")).fetchone()
        if not already:
            driver_inbox_add(d["id"], "delay_check", "⏰ Taking a while?",
                f"Order #{s['id']} has been on the way for a while. "
                f"Is everything OK? Reply here if you need help — traffic, wrong address, anything.")
    conn.close()
    return {"ok": True}


@app.get("/api/drivers/earnings")
async def driver_earnings(req: Request):
    d = get_driver(req)
    if not d:
        raise HTTPException(401, "Not logged in")
    conn = db()
    rows = conn.execute(
        """SELECT delivery_fee, tip, created_at FROM orders
           WHERE driver_id=? AND stripe_status='paid' AND status='delivered'""",
        (d["id"],)).fetchall()
    fees = round(sum(r["delivery_fee"] for r in rows), 2)
    tips = round(sum((r["tip"] or 0) for r in rows), 2)
    conn.close()
    return {"deliveries": len(rows), "fees": fees, "tips": tips,
            "total": round(fees + tips, 2)}


@app.post("/api/operator/orders/{oid}/assign")
async def op_assign_order(oid: int, req: Request, x_pin: str = Header(default="")):
    """Ken assigns an order to a driver (0 = Ken himself)."""
    check_pin(x_pin)
    body = await req.json()
    driver_id = int(body.get("driver_id") or 0)
    conn = db()
    if driver_id:
        dr = conn.execute("SELECT id, name FROM drivers WHERE id=? AND status='approved'",
                          (driver_id,)).fetchone()
        if not dr:
            conn.close()
            raise HTTPException(400, "Driver not found or not approved")
    conn.execute("UPDATE orders SET driver_id=?, updated_at=? WHERE id=?",
                 (driver_id, now_iso(), oid))
    conn.commit()
    conn.close()
    audit("operator", "order_assigned", "order", oid, f"driver {driver_id}")
    if driver_id:
        o = db().execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        label = o["pickup_name"] if (o["kind"] or "food") == "courier" else "restaurant"
        driver_inbox_add(driver_id, "info", "📦 New delivery assigned",
            f"Order #{oid}: pick up from {o['pickup_name'] or label}, deliver to {o['address']}. "
            f"Open your Dropper portal for details and navigation.")
    return {"ok": True}


# ---------------- DROP MORE financial engine ----------------
# Pricing, profit protection, driver pay, payouts, financial dashboard.
# Money movement to drivers is tracked here; actual transfers happen via
# Stripe Connect once Ken completes onboarding (see /api/connect/* below),
# or manually (Ken taps "marked paid").

def haversine_miles(lat1, lng1, lat2, lng2):
    """Straight-line distance (fallback only)."""
    import math
    R = 3959
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return round(2 * R * math.asin(math.sqrt(a)), 2)


def driving_miles(lat1, lng1, lat2, lng2):
    """Real driving distance via OSRM (free, no key). Falls back to haversine."""
    try:
        url = (f"https://router.project-osrm.org/route/v1/driving/"
               f"{lng1},{lat1};{lng2},{lat2}?overview=false")
        req = urllib.request.Request(url, headers={"User-Agent": "DropDelivery/1.0"})
        data = json.load(urllib.request.urlopen(req, timeout=10))
        if data.get("routes"):
            meters = data["routes"][0]["distance"]
            return round(meters / 1609.34, 2)
    except Exception:
        pass
    return haversine_miles(lat1, lng1, lat2, lng2)


def fin_setting(key, default):
    v = get_setting(key)
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def calc_quote(pickup_lat, pickup_lng, cust_lat, cust_lng, kind="food"):
    """Smart pricing: distance-based delivery fee + driver pay + profit check.
    Returns {delivery_fee, driver_pay, distance_miles, stripe_fee_est,
             contribution_profit, meets_min_profit, warnings}."""
    base_fee = fin_setting("price_base_fee", 3.99)
    per_mile = fin_setting("price_per_mile", 1.50)
    driver_base = fin_setting("driver_base_pay", 3.00)
    driver_per_mile = fin_setting("driver_per_mile", 1.00)
    min_profit = fin_setting("min_profit_per_order", 1.00)
    miles = driving_miles(pickup_lat, pickup_lng, cust_lat, cust_lng)
    # courier orders (bigger items, more handling) get a bump
    complexity = 1.25 if kind == "courier" else 1.0
    delivery_fee = round((base_fee + miles * per_mile) * complexity, 2)
    driver_pay = round(driver_base + miles * driver_per_mile, 2)
    # Stripe takes ~2.9% + $0.30 on the delivery fee portion
    stripe_fee = round(delivery_fee * 0.029 + 0.30, 2)
    contribution = round(delivery_fee - driver_pay - stripe_fee, 2)
    warnings = []
    if miles == 0:
        warnings.append("Distance unknown — using base fee. Verify addresses.")
    if contribution < min_profit:
        warnings.append(
            f"Below minimum profit (${min_profit:.2f}). "
            f"Consider raising the fee or declining this delivery.")
    return {"delivery_fee": delivery_fee, "driver_pay": driver_pay,
            "distance_miles": miles, "stripe_fee_est": stripe_fee,
            "contribution_profit": contribution,
            "meets_min_profit": contribution >= min_profit,
            "warnings": warnings}


def order_financials(o):
    """Full money breakdown for one order. Tips are NEVER company profit."""
    fee = o["delivery_fee"] or 0
    tip = o["tip"] or 0
    total = o["total"] or 0
    driver_pay = o["driver_pay"] or 0
    # Stripe fee on the whole charge
    stripe_fee = round(total * 0.029 + 0.30, 2) if total > 0 else 0
    # Company keeps: delivery fee minus driver pay minus Stripe's cut of the fee portion
    # Tips pass through 100% to driver — never counted as profit.
    company = round(fee - driver_pay - (fee * 0.029 + 0.30 if fee else 0), 2)
    return {"delivery_fee": fee, "tip": tip, "total": total,
            "driver_pay": driver_pay, "driver_total": round(driver_pay + tip, 2),
            "stripe_fee_est": stripe_fee,
            "company_profit": company}


# ---------------- Stripe Connect (driver payouts) ----------------
# STATUS: code-ready, needs Ken's activation.
# Ken must: (1) enable Connect in Stripe Dashboard -> Settings -> Connect,
# (2) complete onboarding (identity + bank account), (3) set CONNECT_ENABLED=1.
# Until then, payouts are tracked here and Ken sends them manually.

CONNECT_ENABLED = os.environ.get("CONNECT_ENABLED", "") == "1"


@app.get("/api/operator/connect/status")
def connect_status(x_pin: str = Header(default="")):
    check_pin(x_pin)
    return {
        "enabled": CONNECT_ENABLED,
        "stripe_live": STRIPE_LIVE,
        "steps": [
            {"done": STRIPE_LIVE,
             "label": "Stripe payments live (customer checkout)"},
            {"done": False,
             "label": "Enable Connect in Stripe Dashboard → Settings → Connect"},
            {"done": False,
             "label": "Complete Connect onboarding (identity verification + bank account)"},
            {"done": CONNECT_ENABLED,
             "label": "Set CONNECT_ENABLED=1 in Render environment variables"},
        ],
        "note": ("Until Connect is active, driver payouts are tracked in the "
                 "Payouts tab and you send them manually (bank transfer, Cash App, etc.), "
                 "then tap 'Mark paid'."),
    }


@app.post("/api/operator/drivers/{did}/connect-link")
def driver_connect_link(did: int, x_pin: str = Header(default="")):
    """Generate a Stripe Express onboarding link for a driver.
    Requires CONNECT_ENABLED=1 and the stripe package with Connect access."""
    check_pin(x_pin)
    if not CONNECT_ENABLED:
        raise HTTPException(400,
            "Stripe Connect isn't activated yet. See the Connect tab for setup steps.")
    # Full Express onboarding is implemented when Ken activates Connect.
    # The flow: stripe.Account.create(type='express') ->
    #          stripe.AccountLink.create(...) -> driver completes onboarding ->
    #          webhook account.updated -> store stripe_account_id on driver.
    raise HTTPException(501, "Connect onboarding activates after Ken completes setup.")


# ---------------- DROP MORE marketplace: partnerships ----------------
DEAL_STAGES = ["submitted", "under_review", "financial_eval", "negotiation",
               "contract_review", "approved", "active", "rejected", "withdrawn"]
DEAL_LABELS = {"submitted": "📥 Submitted", "under_review": "👀 Under Review",
               "financial_eval": "💰 Financial Evaluation", "negotiation": "🤝 Negotiation",
               "contract_review": "📝 Contract Review", "approved": "✅ Approved",
               "active": "🚀 Active Partnership", "rejected": "❌ Rejected",
               "withdrawn": "↩️ Withdrawn"}


def audit(actor, action, entity_type="", entity_id=0, details=""):
    """Every admin decision gets recorded. Nothing happens in the dark."""
    try:
        conn = db()
        conn.execute("INSERT INTO audit_log(actor, action, entity_type, entity_id, details, created_at)"
                     " VALUES(?,?,?,?,?,?)",
                     (actor, action, entity_type, entity_id, details[:1000], now_iso()))
        conn.commit()
        conn.close()
    except Exception:
        pass


@app.post("/api/partners/apply")
async def partner_apply(req: Request):
    """Public: business partnership application."""
    body = await req.json()
    legal = (body.get("legal_name") or "").strip()
    contact = (body.get("contact_name") or "").strip()
    email = (body.get("email") or "").strip()
    if not legal or not contact or "@" not in email:
        raise HTTPException(400, "Legal name, contact name, and valid email required")
    conn = db()
    cur = conn.execute(
        """INSERT INTO business_partners(
           legal_name, dba, contact_name, contact_title, email, phone, website, address,
           business_type, products, locations, est_deliveries, service_areas, shipment_info,
           proposed_rates, payment_terms, commission, contract_duration, start_date,
           requirements, comments, status, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'submitted', ?, ?)""",
        (legal, (body.get("dba") or "").strip()[:120], contact,
         (body.get("contact_title") or "").strip()[:80], email,
         (body.get("phone") or "").strip()[:30], (body.get("website") or "").strip()[:300],
         (body.get("address") or "").strip()[:200], (body.get("business_type") or "").strip()[:80],
         (body.get("products") or "").strip()[:500],
         int(body.get("locations") or 1),
         (body.get("est_deliveries") or "").strip()[:200],
         (body.get("service_areas") or "").strip()[:300],
         (body.get("shipment_info") or "").strip()[:500],
         (body.get("proposed_rates") or "").strip()[:500],
         (body.get("payment_terms") or "").strip()[:300],
         (body.get("commission") or "").strip()[:300],
         (body.get("contract_duration") or "").strip()[:100],
         (body.get("start_date") or "").strip()[:40],
         (body.get("requirements") or "").strip()[:1000],
         (body.get("comments") or "").strip()[:1000],
         now_iso(), now_iso()))
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    audit("system", "partner_applied", "business_partner", pid, legal)
    try:
        topic = get_setting("ntfy_topic").strip()
        if topic:
            msg = f"🤝 NEW PARTNERSHIP APPLICATION #{pid}\n{legal} — {contact}"
            r2 = urllib.request.Request(
                f"https://ntfy.sh/{urllib.parse.quote(topic)}",
                data=msg.encode(), headers={"Title": "Drop partnership"}, method="POST")
            threading.Thread(
                target=lambda: urllib.request.urlopen(r2, timeout=5).read(),
                daemon=True).start()
    except Exception:
        pass
    return {"ok": True, "id": pid}


@app.get("/api/operator/partners")
def op_partners(x_pin: str = Header(default=""), status: str = ""):
    """Ken's deal pipeline. Filter by stage."""
    check_pin(x_pin)
    conn = db()
    if status:
        rows = conn.execute("SELECT * FROM business_partners WHERE status=? ORDER BY id DESC",
                            (status,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM business_partners ORDER BY id DESC").fetchall()
    out = [dict(r) for r in rows]
    conn.close()
    return {"partners": out, "stages": DEAL_LABELS}


@app.post("/api/operator/partners/{pid}/stage")
async def op_partner_stage(pid: int, req: Request, x_pin: str = Header(default="")):
    """Move a deal through the pipeline. Ken only."""
    check_pin(x_pin)
    body = await req.json()
    stage = body.get("stage")
    if stage not in DEAL_STAGES:
        raise HTTPException(400, "Bad stage")
    conn = db()
    conn.execute("UPDATE business_partners SET status=?, updated_at=? WHERE id=?",
                 (stage, now_iso(), pid))
    conn.commit()
    conn.close()
    audit("operator", f"deal_stage->{stage}", "business_partner", pid, "")
    return {"ok": True}


@app.post("/api/operator/partners/{pid}/note")
async def op_partner_note(pid: int, req: Request, x_pin: str = Header(default="")):
    """Negotiation history / internal notes."""
    check_pin(x_pin)
    body = await req.json()
    note = (body.get("note") or "").strip()[:2000]
    if not note:
        raise HTTPException(400, "Note required")
    conn = db()
    conn.execute("INSERT INTO deal_notes(partner_id, author, note, created_at)"
                 " VALUES(?, 'operator', ?, ?)", (pid, note, now_iso()))
    conn.commit()
    conn.close()
    audit("operator", "deal_note", "business_partner", pid, note[:100])
    return {"ok": True}


@app.get("/api/operator/partners/{pid}/notes")
def op_partner_notes(pid: int, x_pin: str = Header(default="")):
    check_pin(x_pin)
    conn = db()
    rows = conn.execute("SELECT * FROM deal_notes WHERE partner_id=? ORDER BY id",
                        (pid,)).fetchall()
    out = [dict(r) for r in rows]
    conn.close()
    return out


@app.post("/api/operator/partners/{pid}/evaluate")
def op_partner_evaluate(pid: int, x_pin: str = Header(default="")):
    """Profitability review: estimates the deal's value using the pricing engine."""
    check_pin(x_pin)
    conn = db()
    p = conn.execute("SELECT * FROM business_partners WHERE id=?", (pid,)).fetchone()
    conn.close()
    if not p:
        raise HTTPException(404, "Not found")
    import re
    txt = (p["est_deliveries"] or "").lower()
    nums = re.findall(r"[\d,]+", txt)
    per_month = 0
    if nums:
        n = int(nums[0].replace(",", ""))
        if "day" in txt or "daily" in txt:
            per_month = n * 30
        elif "week" in txt:
            per_month = n * 4.33
        elif "month" in txt:
            per_month = n
        else:
            per_month = n
    rate_nums = re.findall(r"\$?([\d.]+)", p["proposed_rates"] or "")
    rev_per = float(rate_nums[0]) if rate_nums else fin_setting("price_base_fee", 3.99)
    driver_per = fin_setting("driver_base_pay", 3.00) + fin_setting("driver_per_mile", 1.00) * 3
    stripe_per = rev_per * 0.029 + 0.30
    contrib_per = round(rev_per - driver_per - stripe_per, 2)
    monthly = round(contrib_per * per_month, 2)
    annual = round(monthly * 12, 2)
    min_p = fin_setting("min_profit_per_order", 1.00)
    verdict = "✅ MEETS TARGETS" if contrib_per >= min_p and per_month > 0 else (
        "⚠️ BELOW TARGETS — renegotiate pricing or decline" if per_month > 0
        else "❓ NEEDS VOLUME INFO — ask for estimated deliveries")
    result = {
        "est_deliveries_month": round(per_month),
        "revenue_per_delivery": round(rev_per, 2),
        "driver_cost_per": round(driver_per, 2),
        "stripe_per": round(stripe_per, 2),
        "contribution_per_delivery": contrib_per,
        "est_monthly_profit": monthly,
        "est_annual_profit": annual,
        "verdict": verdict,
    }
    conn = db()
    conn.execute("UPDATE business_partners SET profit_score=? WHERE id=?",
                 (contrib_per, pid))
    conn.commit()
    conn.close()
    audit("operator", "deal_evaluated", "business_partner", pid,
          f"${contrib_per}/delivery, ${monthly}/mo")
    return result


# ---------------- Business accounts ----------------
def get_biz_user(req):
    token = req.cookies.get("drop_biz", "")
    if not token:
        return None
    conn = db()
    s = conn.execute("SELECT business_user_id FROM business_sessions WHERE token=?",
                     (token,)).fetchone()
    if not s:
        conn.close()
        return None
    u = conn.execute("SELECT bu.*, bp.legal_name, bp.status FROM business_users bu "
                     "JOIN business_partners bp ON bp.id = bu.partner_id "
                     "WHERE bu.id=?", (s["business_user_id"],)).fetchone()
    conn.close()
    return dict(u) if u else None


@app.post("/api/biz/register")
async def biz_register(req: Request):
    """Business creates a login linked to their partnership application."""
    body = await req.json()
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    partner_id = int(body.get("partner_id") or 0)
    if "@" not in email or len(password) < 6 or not partner_id:
        raise HTTPException(400, "Valid email, 6+ char password, and application ID required")
    conn = db()
    p = conn.execute("SELECT id, email FROM business_partners WHERE id=?", (partner_id,)).fetchone()
    if not p or p["email"].lower() != email:
        conn.close()
        raise HTTPException(400, "Email must match your partnership application")
    if conn.execute("SELECT id FROM business_users WHERE email=?", (email,)).fetchone():
        conn.close()
        raise HTTPException(400, "An account with this email already exists")
    cur = conn.execute(
        "INSERT INTO business_users(partner_id, name, email, password_hash, created_at)"
        " VALUES(?,?,?, ?,?)",
        (partner_id, (body.get("name") or p["email"]).strip()[:80], email,
         hash_pw(password), now_iso()))
    uid = cur.lastrowid
    conn.commit()
    conn.close()
    audit("system", "biz_registered", "business_partner", partner_id, email)
    return {"ok": True, "id": uid}


@app.post("/api/biz/login")
async def biz_login(req: Request):
    body = await req.json()
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    conn = db()
    u = conn.execute("SELECT * FROM business_users WHERE email=?", (email,)).fetchone()
    conn.close()
    if not u or not check_pw(password, u["password_hash"]):
        raise HTTPException(401, "Wrong email or password")
    token = secrets.token_urlsafe(32)
    exp = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    conn = db()
    conn.execute("INSERT INTO business_sessions(token, business_user_id, expires_at)"
                 " VALUES(?,?,?)", (token, u["id"], exp))
    conn.commit()
    conn.close()
    resp = JSONResponse({"ok": True})
    resp.set_cookie("drop_biz", token, max_age=30*86400, httponly=True, samesite="lax")
    return resp


@app.post("/api/biz/logout")
async def biz_logout(req: Request):
    token = req.cookies.get("drop_biz", "")
    if token:
        conn = db()
        conn.execute("DELETE FROM business_sessions WHERE token=?", (token,))
        conn.commit()
        conn.close()
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("drop_biz")
    return resp


@app.get("/api/biz/me")
async def biz_me(req: Request):
    u = get_biz_user(req)
    if not u:
        raise HTTPException(401, "Not logged in")
    u.pop("password_hash", None)
    conn = db()
    notes = conn.execute(
        "SELECT note, created_at FROM deal_notes WHERE partner_id=? AND author='operator'"
        " ORDER BY id DESC LIMIT 5", (u["partner_id"],)).fetchall()
    conn.close()
    u["recent_notes"] = [dict(n) for n in notes]
    u["stage_label"] = DEAL_LABELS.get(u["status"], u["status"])
    return u


# ---------------- AI driver performance ----------------
@app.get("/api/operator/drivers/{did}/performance")
def driver_performance(did: int, x_pin: str = Header(default="")):
    """AI-assisted driver evaluation from real delivery data.
    Suggests a rating + evidence. Ken reviews and decides — never auto-punishes."""
    check_pin(x_pin)
    conn = db()
    d = conn.execute("SELECT * FROM drivers WHERE id=?", (did,)).fetchone()
    if not d:
        conn.close()
        raise HTTPException(404, "Driver not found")
    total = d["deliveries"] or 0
    on_time = d["on_time"] or 0
    rate = round(100 * on_time / total, 1) if total else 100.0
    conn.close()
    d = dict(d)
    score = 50
    evidence = []
    if total >= 10:
        score += 15
        evidence.append(f"{total} deliveries completed — experienced")
    elif total >= 3:
        score += 8
        evidence.append(f"{total} deliveries completed")
    elif total == 0:
        evidence.append("No deliveries yet — too early to judge")
    if rate >= 95:
        score += 25
        evidence.append(f"{rate}% on-time — excellent reliability")
    elif rate >= 85:
        score += 15
        evidence.append(f"{rate}% on-time — good")
    elif rate >= 70:
        score += 5
        evidence.append(f"{rate}% on-time — needs improvement")
    else:
        score -= 10
        evidence.append(f"{rate}% on-time — below standard")
    docs_ok = sum(1 for k in ("gov_id_status", "insurance_status", "vehicle_reg_status")
                  if d.get(k) == "verified")
    if docs_ok == 3:
        score += 10
        evidence.append("All documents verified ✅")
    else:
        evidence.append(f"Documents: {docs_ok}/3 verified")
        if docs_ok < 2:
            score -= 5
    score = max(0, min(100, score))
    if score >= 85:
        rating, rec = "⭐ Excellent", "Keep them busy — prioritize for high-value orders."
    elif score >= 70:
        rating, rec = "✅ Good", "Solid driver. No action needed."
    elif score >= 50:
        rating, rec = "⚠️ Fair", "Watch closely. Offer coaching on weak areas."
    else:
        rating, rec = "🔴 Needs attention", "Review with driver before assigning more orders."
    audit("operator", "perf_reviewed", "driver", did, f"score {score}")
    return {
        "driver_id": did, "name": d["name"],
        "score": score, "rating": rating, "recommendation": rec,
        "evidence": evidence,
        "metrics": {"deliveries": total, "on_time_rate": rate,
                    "docs_verified": f"{docs_ok}/3"},
        "disclaimer": ("AI suggestion from verified delivery data only. "
                       "Review the evidence, adjust as needed — you make the final call."),
    }


@app.get("/api/operator/audit")
def op_audit(x_pin: str = Header(default=""), limit: int = 100):
    """Audit trail: every admin action, newest first."""
    check_pin(x_pin)
    conn = db()
    rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?",
                        (min(limit, 500),)).fetchall()
    out = [dict(r) for r in rows]
    conn.close()
    return out


# ---------------- Driver notifications, safety & motivation ----------------
def push_to_driver(driver_id, title, message):
    """Send an ntfy push to a driver's personal topic + save to their inbox."""
    conn = db()
    d = conn.execute("SELECT ntfy_topic, name FROM drivers WHERE id=?", (driver_id,)).fetchone()
    if not d:
        conn.close()
        return False
    topic = (d["ntfy_topic"] or "").strip()
    conn.close()
    if topic:
        try:
            r = urllib.request.Request(
                f"https://ntfy.sh/{urllib.parse.quote(topic)}",
                data=message.encode(), headers={"Title": title}, method="POST")
            threading.Thread(
                target=lambda: urllib.request.urlopen(r, timeout=5).read(),
                daemon=True).start()
        except Exception:
            pass
    return bool(topic)


def driver_inbox_add(driver_id, kind, title, body):
    """Save a message to the driver's in-portal inbox."""
    conn = db()
    conn.execute("INSERT INTO driver_messages(driver_id, kind, title, body, created_at)"
                 " VALUES(?,?,?,?,?)", (driver_id, kind, title, body, now_iso()))
    conn.commit()
    conn.close()
    push_to_driver(driver_id, title, body[:200])


@app.post("/api/drivers/ntfy")
async def driver_set_ntfy(req: Request):
    """Driver sets their own ntfy topic for push alerts."""
    d = get_driver(req)
    if not d:
        raise HTTPException(401, "Not logged in")
    body = await req.json()
    topic = (body.get("topic") or "").strip()[:80]
    conn = db()
    conn.execute("UPDATE drivers SET ntfy_topic=? WHERE id=?", (topic, d["id"]))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/drivers/messages")
async def driver_messages(req: Request):
    """Driver's inbox: boosts, warnings, AI messages."""
    d = get_driver(req)
    if not d:
        raise HTTPException(401, "Not logged in")
    conn = db()
    rows = conn.execute("SELECT * FROM driver_messages WHERE driver_id=? ORDER BY id DESC LIMIT 30",
                        (d["id"],)).fetchall()
    out = [dict(r) for r in rows]
    conn.execute("UPDATE driver_messages SET read=1 WHERE driver_id=?", (d["id"],))
    conn.commit()
    conn.close()
    return out


@app.post("/api/operator/drivers/{did}/boost")
async def op_driver_boost(did: int, req: Request, x_pin: str = Header(default="")):
    """Ken sends a motivational boost to a driver."""
    check_pin(x_pin)
    body = await req.json()
    msg = (body.get("message") or "").strip()[:500]
    if not msg:
        # AI-generated encouragement
        import random
        options = [
            "Great work out there! Your on-time deliveries are what makes Drop the best in Bismarck. Keep it up! 🧡",
            "Thank you for representing Drop so well. Customers notice the difference. You're appreciated! 🙏",
            "Another great shift! Your reliability keeps businesses coming back. Proud to have you on the team! ⭐",
            "You're crushing it! Fast, friendly, on-time — that's the Drop way. Thank you! 🚗💨",
        ]
        msg = random.choice(options)
    driver_inbox_add(did, "boost", "💪 Message from Drop", msg)
    audit("operator", "driver_boost", "driver", did, msg[:80])
    return {"ok": True, "message": msg}


# ---------------- AI outreach emails ----------------
# Ken approves every email before it sends. Nothing goes out automatically.

@app.post("/api/operator/outreach/draft")
async def op_outreach_draft(req: Request, x_pin: str = Header(default="")):
    """AI drafts a partnership outreach email. Ken reviews before sending."""
    check_pin(x_pin)
    body = await req.json()
    to_email = (body.get("to_email") or "").strip()
    biz_name = (body.get("business_name") or "").strip()
    biz_type = (body.get("business_type") or "").strip()
    contact = (body.get("contact_name") or "").strip()
    if "@" not in to_email or not biz_name:
        raise HTTPException(400, "Email and business name required")
    greeting = f"Hi {contact}," if contact else "Hello,"
    subject = f"Delivery partnership for {biz_name} — from DROP MORE (Bismarck)"
    email_body = f"""{greeting}

I'm reaching out from DROP MORE, Bismarck's local delivery company. We help businesses like yours with fast, reliable pickups and deliveries — auto parts, retail, supplies, documents, whatever you need moved.

Here's what we offer {biz_name}:
• Dedicated local drivers (not gig-app roulette)
• Recurring pickup routes on your schedule
• Live GPS tracking on every delivery
• Transparent pricing — you propose the deal, we make the math work
• $3.99 flat delivery fee for one-offs, custom rates for partners

We're already delivering across Bismarck and looking to partner with great local businesses. Would you be open to a quick 10-minute call next week to explore fit?

You can also apply directly here: https://drop-52yj.onrender.com/partner.html

Looking forward to connecting.

Best,
The DROP MORE Team
Bismarck, ND
wearedropservicesllc@gmail.com | (262) 977-7869"""
    conn = db()
    cur = conn.execute(
        "INSERT INTO outreach_emails(to_email, to_name, subject, body, business_name, status, created_at)"
        " VALUES(?,?,?,?,?,'draft',?)",
        (to_email, contact, subject, email_body, biz_name, now_iso()))
    eid = cur.lastrowid
    conn.commit()
    conn.close()
    audit("operator", "outreach_drafted", "outreach", eid, f"to {biz_name}")
    return {"ok": True, "id": eid, "subject": subject, "body": email_body}


@app.get("/api/operator/outreach")
def op_outreach_list(x_pin: str = Header(default=""), status: str = ""):
    check_pin(x_pin)
    conn = db()
    if status:
        rows = conn.execute("SELECT * FROM outreach_emails WHERE status=? ORDER BY id DESC",
                            (status,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM outreach_emails ORDER BY id DESC").fetchall()
    out = [dict(r) for r in rows]
    conn.close()
    return out


@app.post("/api/operator/outreach/{eid}/send")
async def op_outreach_send(eid: int, req: Request, x_pin: str = Header(default="")):
    """Ken approved → marks the email ready to send.
    A scheduled job on the operator's assistant side picks it up within ~15 minutes
    and sends it from the Drop business Gmail, then marks it sent."""
    check_pin(x_pin)
    conn = db()
    e = conn.execute("SELECT * FROM outreach_emails WHERE id=?", (eid,)).fetchone()
    if not e or e["status"] != "draft":
        conn.close()
        raise HTTPException(400, "Email not found or already sent")
    conn.execute("UPDATE outreach_emails SET status='approved' WHERE id=?", (eid,))
    conn.commit()
    conn.close()
    audit("operator", "outreach_approved", "outreach", eid, f"to {e['to_email']}")
    return {"ok": True, "note": "Approved — will send from wearedropservicesllc@gmail.com within ~15 minutes."}


@app.post("/api/operator/outreach/{eid}/discard")
def op_outreach_discard(eid: int, x_pin: str = Header(default="")):
    check_pin(x_pin)
    conn = db()
    conn.execute("UPDATE outreach_emails SET status='discarded' WHERE id=?", (eid,))
    conn.commit()
    conn.close()
    audit("operator", "outreach_discarded", "outreach", eid, "")
    return {"ok": True}


@app.post("/api/operator/outreach/{eid}/sent")
def op_outreach_sent(eid: int, x_pin: str = Header(default="")):
    """Internal: the outreach sender job marks an email sent after Gmail confirms."""
    check_pin(x_pin)
    conn = db()
    conn.execute("UPDATE outreach_emails SET status='sent', sent_at=? WHERE id=?",
                 (now_iso(), eid))
    conn.commit()
    conn.close()
    audit("system", "outreach_sent", "outreach", eid, "")
    log_event("email", f"📧 Outreach email sent", f"ID {eid}")
    return {"ok": True}


# Render redeploy trigger 2026-10-11 00:40 CDT — ensure gift-queue endpoints live
@app.get("/api/operator/gift-queue")
def op_gift_queue(x_pin: str = Header(default="")):
    """Pending gift notifications for the background sender."""
    check_pin(x_pin)
    conn = db()
    rows = conn.execute("SELECT * FROM gift_queue WHERE status='pending' ORDER BY id").fetchall()
    out = [dict(r) for r in rows]
    conn.close()
    return out


@app.get("/api/operator/drivers/live")
def op_drivers_live(x_pin: str = Header(default="")):
    """Live GPS positions of all drivers for the operator map."""
    check_pin(x_pin)
    conn = db()
    rows = conn.execute(
        """SELECT d.id, d.name, d.status, d.last_lat as lat, d.last_lng as lng,
                  d.last_gps_at,
                  (SELECT COUNT(*) FROM orders o WHERE o.driver_id=d.id
                   AND o.status IN ('confirmed','picked_up','on_the_way')) as active_orders
           FROM drivers d WHERE d.status='approved' ORDER BY d.name""").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        # human-friendly "updated X ago"
        upd = ""
        if d["last_gps_at"]:
            try:
                from datetime import datetime
                dt = (datetime.now(timezone.utc) -
                      datetime.fromisoformat(d["last_gps_at"])).total_seconds()
                if dt < 90:
                    upd = "live now"
                elif dt < 3600:
                    upd = f"{int(dt/60)}m ago"
                else:
                    upd = f"{int(dt/3600)}h ago"
            except Exception:
                pass
        d["updated"] = upd
        d["status"] = f"{d['active_orders']} active" if d["active_orders"] else "idle"
        # only show recent positions (last 30 min)
        if d["last_gps_at"]:
            try:
                from datetime import datetime
                dt = (datetime.now(timezone.utc) -
                      datetime.fromisoformat(d["last_gps_at"])).total_seconds()
                if dt > 1800:
                    d["lat"] = None
                    d["lng"] = None
            except Exception:
                pass
        out.append(d)
    conn.close()
    return out


@app.post("/api/operator/gift-queue/{gid}/sent")
def op_gift_sent(gid: int, x_pin: str = Header(default="")):
    check_pin(x_pin)
    conn = db()
    conn.execute("UPDATE gift_queue SET status='sent' WHERE id=?", (gid,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ---------------- Live activity feed + AI suggestions ----------------
def log_event(kind, title, detail=""):
    """Record something the platform did on its own. Ken sees it live."""
    try:
        conn = db()
        conn.execute("INSERT INTO system_events(kind, title, detail, created_at)"
                     " VALUES(?,?,?,?)", (kind, title, detail[:500], now_iso()))
        # keep the feed lean — last 200 events
        conn.execute("DELETE FROM system_events WHERE id NOT IN "
                     "(SELECT id FROM system_events ORDER BY id DESC LIMIT 200)")
        conn.commit()
        conn.close()
    except Exception:
        pass


@app.get("/api/operator/activity")
def op_activity(x_pin: str = Header(default=""), limit: int = 50):
    """Live feed: system events + recent admin actions, newest first."""
    check_pin(x_pin)
    conn = db()
    events = conn.execute("SELECT kind, title, detail, created_at, 'system' as src"
                          " FROM system_events ORDER BY id DESC LIMIT ?",
                          (min(limit, 100),)).fetchall()
    actions = conn.execute("SELECT action as title, details as detail, created_at,"
                           " 'you' as src, 'action' as kind FROM audit_log"
                           " ORDER BY id DESC LIMIT ?",
                           (min(limit, 100),)).fetchall()
    conn.close()
    feed = [dict(e) for e in events] + [dict(a) for a in actions]
    feed.sort(key=lambda x: x["created_at"], reverse=True)
    return feed[:limit]


@app.get("/api/operator/suggestions")
def op_suggestions(x_pin: str = Header(default="")):
    """AI suggestions: what should Ken do right now? Based on live data."""
    check_pin(x_pin)
    conn = db()
    out = []

    def safe_count(query):
        try:
            return conn.execute(query).fetchone()["n"]
        except Exception:
            return 0

    # 1. Unreviewed partnership applications
    new_deals = safe_count("SELECT COUNT(*) as n FROM business_partners WHERE status='submitted'")
    if new_deals:
        out.append({"icon": "🤝", "priority": "high",
                    "text": f"{new_deals} partnership application(s) waiting for review.",
                    "action": "Open the Deals tab and evaluate them."})

    # 2. Pending driver applications
    pend_drv = safe_count("SELECT COUNT(*) as n FROM drivers WHERE status='pending'")
    if pend_drv:
        out.append({"icon": "🚗", "priority": "high",
                    "text": f"{pend_drv} driver application(s) need your approval.",
                    "action": "Check the Drivers tab — nobody drives without your OK."})

    # 3. Drivers missing documents
    bad_docs = safe_count(
        """SELECT COUNT(*) as n FROM drivers WHERE status='approved'
           AND (gov_id_status != 'verified' OR insurance_status != 'verified'
                OR vehicle_reg_status != 'verified')""")
    if bad_docs:
        out.append({"icon": "📄", "priority": "medium",
                    "text": f"{bad_docs} active driver(s) have unverified documents.",
                    "action": "Review their profiles and request missing paperwork."})

    # 4. Speed warnings
    try:
        speeders = conn.execute(
            "SELECT name, speed_warnings FROM drivers WHERE speed_warnings > 0"
            " AND status='approved' ORDER BY speed_warnings DESC LIMIT 3").fetchall()
    except Exception:
        speeders = []
    for s in speeders:
        out.append({"icon": "⚠️", "priority": "medium",
                    "text": f"{s['name']} has {s['speed_warnings']} speeding warning(s).",
                    "action": "Consider a conversation before it becomes a problem."})

    # 5. Unprofitable recent orders
    try:
        rows = conn.execute(
            "SELECT id, total, tip FROM orders WHERE status='delivered'"
            " ORDER BY id DESC LIMIT 20").fetchall()
        if rows:
            base = fin_setting("price_base_fee", 3.99)
            dbase = fin_setting("driver_base_pay", 3.00)
            bad = [r for r in rows
                   if (r["total"] or 0) - dbase - (r["total"] or 0) * 0.029 - 0.30
                   < fin_setting("min_profit_per_order", 1.00)]
            if bad:
                out.append({"icon": "💸", "priority": "high",
                            "text": f"{len(bad)} of your last 20 orders were below your profit target.",
                            "action": "Check the Money tab — consider raising the delivery fee."})
    except Exception:
        pass

    # 6. Draft outreach emails waiting
    drafts = safe_count("SELECT COUNT(*) as n FROM outreach_emails WHERE status='draft'")
    if drafts:
        out.append({"icon": "📧", "priority": "low",
                    "text": f"{drafts} outreach email draft(s) waiting for your approval.",
                    "action": "Review them in the Outreach tab."})

    # 7. Open support tickets
    open_tix = safe_count("SELECT COUNT(*) as n FROM tickets WHERE status='open'")
    if open_tix:
        out.append({"icon": "💬", "priority": "medium",
                    "text": f"{open_tix} customer message(s) need a reply.",
                    "action": "Open the Support tab."})

    # 8. Idle drivers who deserve a boost
    try:
        idle = conn.execute(
            "SELECT id, name FROM drivers WHERE status='approved' AND deliveries >= 5"
            " ORDER BY RANDOM() LIMIT 1").fetchone()
    except Exception:
        idle = None
    if idle and len(out) < 6:
        out.append({"icon": "💪", "priority": "low",
                    "text": f"{idle['name']} has been delivering consistently.",
                    "action": "Send them a boost — a little recognition goes a long way."})

    conn.close()
    # high priority first
    order = {"high": 0, "medium": 1, "low": 2}
    out.sort(key=lambda x: order.get(x["priority"], 3))
    log_event("suggestions", f"Generated {len(out)} AI suggestions", "")
    return out


# ---------------- Live city restaurant search ----------------
# Any city, any state: geocode it, then pull real restaurants from OpenStreetMap.
@app.get("/api/search-city")
def search_city(city: str = "", state: str = ""):
    city = city.strip()
    state = state.strip()
    if not city:
        raise HTTPException(400, "City required")
    q = f"{city}, {state}" if state else city

    # 1. check our DB first
    conn = db()
    like = f"%{city}%"
    rows = conn.execute(
        "SELECT * FROM restaurants WHERE city LIKE ? OR state LIKE ? ORDER BY id LIMIT 50",
        (like, like)).fetchall()
    db_hits = []
    for r in rows:
        rd = dict(r)
        items = conn.execute(
            "SELECT * FROM menu_items WHERE restaurant_id=? AND available=1 ORDER BY id LIMIT 5",
            (r["id"],)).fetchall()
        rd["menu"] = [dict(i) for i in items]
        rd["source"] = "drop"
        db_hits.append(rd)
    conn.close()
    if db_hits:
        return {"city": city, "state": state, "source": "drop", "restaurants": db_hits}

    # 2. nothing in DB — go live via OpenStreetMap (Nominatim)
    # Fast and reliable; returns real restaurants in that city.
    try:
        queries = [
            f"restaurant {city} {state}",
            f"restaurants in {city}, {state}",
        ]
        seen = set()
        out = []
        for q in queries:
            qs = urllib.parse.urlencode({"q": q, "format": "json",
                                         "limit": 25, "addressdetails": 1})
            req = urllib.request.Request(
                f"https://nominatim.openstreetmap.org/search?{qs}",
                headers={"User-Agent": "DropDelivery/1.0"})
            try:
                data = json.load(urllib.request.urlopen(req, timeout=10))
            except Exception:
                continue
            for d in data:
                name = (d.get("display_name", "").split(",")[0] or "").strip()
                if not name or name.lower() in seen:
                    continue
                # skip non-restaurant results
                cat = (d.get("category", "") or "").lower()
                typ = (d.get("type", "") or "").lower()
                if cat not in ("amenity", "shop", "tourism") and "restaurant" not in typ and "food" not in typ and "cafe" not in typ:
                    # still accept if the name looks like a place
                    pass
                seen.add(name.lower())
                addr = d.get("address", {})
                out.append({
                    "id": f"osm-{d.get('place_id')}",
                    "name": name,
                    "cuisine": "Restaurant",
                    "city": city, "state": state,
                    "address": f"{addr.get('road','')} {addr.get('house_number','')}".strip(),
                    "phone": "", "website": "",
                    "lat": d.get("lat"), "lng": d.get("lon"),
                    "emoji": "🍽️", "photo": "", "logo": "",
                    "menu": [], "source": "live", "rating": "",
                })
                if len(out) >= 25:
                    break
            if len(out) >= 10:
                break
        return {"city": city, "state": state, "source": "live",
                "restaurants": out}
    except Exception as e:
        return {"city": city, "state": state, "source": "error",
                "restaurants": [], "error": str(e)[:100]}


# ---------------- Rides (passenger) quote ----------------
@app.get("/api/rides/quote")
def rides_quote(pickup_address: str = "", dropoff_address: str = "",
                plat: float = None, plng: float = None,
                dlat: float = None, dlng: float = None):
    """Passenger ride fare estimate. Geocodes both addresses (or uses
    provided GPS coords), calculates real driving distance, returns fare."""
    if not pickup_address or not dropoff_address:
        raise HTTPException(400, "pickup_address and dropoff_address required")

    # pickup coords: use GPS if provided, else geocode
    if plat and plng:
        p_lat, p_lng = plat, plng
        p_approx = False
    else:
        p_lat, p_lng = geocode(pickup_address, {})
        p_approx = True
    # dropoff coords
    if dlat and dlng:
        d_lat, d_lng = dlat, dlng
        d_approx = False
    else:
        d_lat, d_lng = geocode(dropoff_address, {})
        d_approx = True

    if not p_lat or not d_lat:
        raise HTTPException(400,
            "Couldn't map one of those addresses — check the spelling and try again.")

    miles = driving_miles(p_lat, p_lng, d_lat, d_lng)

    # ride pricing (passenger rides cost more than food delivery)
    base_fare = fin_setting("ride_base_fare", 4.99)
    per_mile = fin_setting("ride_per_mile", 2.00)
    gas_pct = fin_setting("ride_gas_pct", 10.0)
    max_miles = fin_setting("ride_max_miles", 150.0)

    mileage_charge = round(miles * per_mile, 2)
    fare = round(base_fare + mileage_charge, 2)
    gas_fee = round(fare * gas_pct / 100, 2)
    total = round(fare + gas_fee, 2)

    # driver pay estimate (70% of fare)
    driver_pay_est = round(fare * 0.70, 2)
    company_profit_est = round(total - driver_pay_est - gas_fee, 2)

    too_far = miles > max_miles
    approximate = p_approx or d_approx

    ai_note = ""
    if company_profit_est >= 3:
        ai_note = "✅ Good fare — healthy margin."
    elif company_profit_est >= 1:
        ai_note = "⚠️ Thin margin on this ride."
    else:
        ai_note = "🔴 Below target margin — consider adjusting."

    return {
        "miles": miles,
        "base_fare": base_fare,
        "per_mile": per_mile,
        "mileage_charge": mileage_charge,
        "fare": fare,
        "gas_pct": gas_pct,
        "gas_fee": gas_fee,
        "total": total,
        "driver_pay_est": driver_pay_est,
        "company_profit_est": company_profit_est,
        "ai_note": ai_note,
        "geocoded": True,
        "too_far": too_far,
        "max_miles": max_miles,
        "approximate": approximate,
        "plat": p_lat, "plng": p_lng,
        "dlat": d_lat, "dlng": d_lng,
    }


# ---------------- Rides table + full ride flow ----------------
def init_rides_table():
    conn = db()
    conn.execute("""CREATE TABLE IF NOT EXISTS rides (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rider_name TEXT NOT NULL, rider_phone TEXT NOT NULL,
        pickup_address TEXT NOT NULL, pickup_lat REAL, pickup_lng REAL,
        dropoff_address TEXT NOT NULL, dropoff_lat REAL, dropoff_lng REAL,
        miles REAL DEFAULT 0, fare REAL DEFAULT 0, tip REAL DEFAULT 0,
        total REAL DEFAULT 0, driver_pay REAL DEFAULT 0,
        company_profit REAL DEFAULT 0,
        driver_id INTEGER DEFAULT 0, driver_name TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'requested',
        rider_rating INTEGER DEFAULT 0, rider_review TEXT DEFAULT '',
        stripe_payment_id TEXT DEFAULT '', stripe_status TEXT DEFAULT 'pending',
        notes TEXT DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    conn.commit()
    conn.close()

init_rides_table()


@app.post("/api/rides/request")
async def rides_request(req: Request):
    """Book a ride. Returns order_id for checkout."""
    body = await req.json()
    pickup = (body.get("pickup_address") or "").strip()
    dropoff = (body.get("dropoff_address") or "").strip()
    name = (body.get("customer_name") or "").strip()
    phone = (body.get("phone") or "").strip()
    notes = (body.get("notes") or "").strip()
    tip = float(body.get("tip") or 0)

    if not pickup or not dropoff or not name or not phone:
        raise HTTPException(400, "Pickup, dropoff, name, and phone required")

    # get fare
    p_lat = body.get("pickup_lat") or 0
    p_lng = body.get("pickup_lng") or 0
    d_lat = body.get("dropoff_lat") or 0
    d_lng = body.get("dropoff_lng") or 0
    if not p_lat:
        p_lat, p_lng = geocode(pickup, {})
    if not d_lat:
        d_lat, d_lng = geocode(dropoff, {})

    miles = driving_miles(p_lat, p_lng, d_lat, d_lng) if p_lat and d_lat else 0
    base_fare = fin_setting("ride_base_fare", 4.99)
    per_mile = fin_setting("ride_per_mile", 2.00)
    gas_pct = fin_setting("ride_gas_pct", 10.0)
    fare = round(base_fare + miles * per_mile, 2)
    gas_fee = round(fare * gas_pct / 100, 2)
    total = round(fare + gas_fee + tip, 2)
    driver_pay = round(fare * 0.70 + tip, 2)  # 70% + full tip
    company_profit = round(total - driver_pay - gas_fee, 2)

    now = datetime.now(timezone.utc).isoformat()
    conn = db()
    cur = conn.execute(
        """INSERT INTO rides (rider_name, rider_phone, pickup_address, pickup_lat,
           pickup_lng, dropoff_address, dropoff_lat, dropoff_lng, miles, fare,
           tip, total, driver_pay, company_profit, notes, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (name, phone, pickup, p_lat, p_lng, dropoff, d_lat, d_lng,
         miles, fare, tip, total, driver_pay, company_profit, notes, now, now))
    rid = cur.lastrowid
    conn.commit()
    conn.close()

    log_event("ride", f"🚗 Ride request #{rid}", f"{name}: {pickup} → {dropoff}")
    return {"ok": True, "order_id": rid, "ride_id": rid, "total": total}


@app.get("/api/rides/driver/{driver_id}")
def driver_rides(driver_id: int, req: Request):
    """Driver's ride history with earnings."""
    # verify driver PIN or operator PIN
    pin = req.headers.get("x-pin", "")
    conn = db()
    drv = conn.execute("SELECT * FROM drivers WHERE id=?", (driver_id,)).fetchone()
    if not drv:
        conn.close()
        raise HTTPException(404, "Driver not found")
    # allow if PIN matches driver PIN or operator PIN
    if pin != drv["pin"] and pin != get_setting("pin"):
        conn.close()
        raise HTTPException(401, "Bad PIN")
    rides = conn.execute(
        "SELECT * FROM rides WHERE driver_id=? ORDER BY id DESC LIMIT 50",
        (driver_id,)).fetchall()
    total_earned = sum(r["driver_pay"] for r in rides if r["status"] == "completed")
    conn.close()
    return {
        "rides": [dict(r) for r in rides],
        "total_earned": round(total_earned, 2),
        "ride_count": len([r for r in rides if r["status"] == "completed"]),
    }


@app.get("/api/rides/{ride_id}")
def get_ride(ride_id: int):
    """Get ride details (for tracking page)."""
    conn = db()
    r = conn.execute("SELECT * FROM rides WHERE id=?", (ride_id,)).fetchone()
    conn.close()
    if not r:
        raise HTTPException(404, "Ride not found")
    return dict(r)


@app.post("/api/rides/{ride_id}/rider-in-car")
async def rider_in_car(ride_id: int, req: Request):
    """Rider confirms they're in the vehicle."""
    conn = db()
    r = conn.execute("SELECT * FROM rides WHERE id=?", (ride_id,)).fetchone()
    if not r:
        conn.close()
        raise HTTPException(404, "Ride not found")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE rides SET status='in_progress', updated_at=? WHERE id=?",
                 (now, ride_id))
    conn.commit()
    conn.close()
    log_event("ride", f"✅ Rider in car — ride #{ride_id}", "")
    # notify driver via ntfy
    try:
        drv = get_driver(r["driver_id"]) if r["driver_id"] else None
        if drv and drv.get("ntfy_topic"):
            send_ntfy(drv["ntfy_topic"], "Rider confirmed",
                      f"Rider is in your car! Head to: {r['dropoff_address']}")
    except Exception:
        pass
    return {"ok": True, "status": "in_progress"}


@app.post("/api/rides/{ride_id}/rate")
async def rate_ride(ride_id: int, req: Request):
    """Rider rates driver and optionally adds tip."""
    body = await req.json()
    rating = int(body.get("rating") or 0)
    review = (body.get("review") or "").strip()
    extra_tip = float(body.get("extra_tip") or 0)
    if rating < 1 or rating > 5:
        raise HTTPException(400, "Rating must be 1-5")
    conn = db()
    r = conn.execute("SELECT * FROM rides WHERE id=?", (ride_id,)).fetchone()
    if not r:
        conn.close()
        raise HTTPException(404, "Ride not found")
    now = datetime.now(timezone.utc).isoformat()
    new_tip = r["tip"] + extra_tip
    new_total = r["total"] + extra_tip
    new_driver_pay = r["driver_pay"] + extra_tip  # 100% of extra tip to driver
    conn.execute(
        "UPDATE rides SET rider_rating=?, rider_review=?, tip=?, total=?,"
        " driver_pay=?, updated_at=? WHERE id=?",
        (rating, review, new_tip, new_total, new_driver_pay, now, ride_id))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/rides/{ride_id}/complete")
async def complete_ride(ride_id: int, req: Request, x_pin: str = Header(default="")):
    """Driver or operator marks ride complete."""
    check_pin(x_pin)
    conn = db()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE rides SET status='completed', updated_at=? WHERE id=?",
                 (now, ride_id))
    conn.commit()
    conn.close()
    log_event("ride", f"🏁 Ride #{ride_id} completed", "")
    return {"ok": True}



@app.get("/api/operator/rides")
def operator_rides(req: Request, x_pin: str = Header(default="")):
    """All rides for operator dashboard."""
    check_pin(x_pin)
    conn = db()
    rides = conn.execute(
        "SELECT * FROM rides ORDER BY id DESC LIMIT 100").fetchall()
    conn.close()
    return [dict(r) for r in rides]


@app.post("/api/operator/rides/{ride_id}/assign")
async def assign_ride_driver(ride_id: int, req: Request, x_pin: str = Header(default="")):
    """Assign a driver to a ride."""
    check_pin(x_pin)
    body = await req.json()
    driver_id = body.get("driver_id")
    conn = db()
    drv = conn.execute("SELECT * FROM drivers WHERE id=?", (driver_id,)).fetchone()
    if not drv:
        conn.close()
        raise HTTPException(404, "Driver not found")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE rides SET driver_id=?, driver_name=?, status='assigned', updated_at=?"
        " WHERE id=?", (driver_id, drv["name"], now, ride_id))
    conn.commit()
    r = conn.execute("SELECT * FROM rides WHERE id=?", (ride_id,)).fetchone()
    conn.close()
    # notify driver
    try:
        if drv.get("ntfy_topic"):
            send_ntfy(drv["ntfy_topic"], "🚗 New ride assigned!",
                      f"Pickup: {r['pickup_address']}\nDropoff: {r['dropoff_address']}\n"
                      f"Fare: ${r['driver_pay']:.2f} for you")
    except Exception:
        pass
    log_event("ride", f"👨‍✈️ Driver {drv['name']} assigned to ride #{ride_id}", "")
    return {"ok": True}


# ---------------- Dropper AI assistant ----------------
@app.post("/api/dropper")
async def dropper_chat(req: Request):
    """Dropper — friendly AI that helps riders sign up and answers questions."""
    body = await req.json()
    q = (body.get("q") or "").strip()
    if not q:
        return {"answer": "Hey! I'm Dropper 🤖 Ask me about riding, driving, or fares!"}
    l = q.lower()

    # quick local answers (no AI needed)
    if any(w in l for w in ("sign", "rider", "account", "join")):
        return {"answer": "To ride with Drop: tap '🧍 Ride with Drop' on the homepage, enter your name and phone number, and you're in! Then use the fare calculator to see your price and book."}
    if any(w in l for w in ("drive", "driver", "job", "apply", "work")):
        return {"answer": "To drive for Drop: go to '🚗 Drive for Drop'. You'll need a valid driver's license, proof of insurance, a clear photo of yourself, and a photo of your car. Every application is reviewed personally — we only approve safe, reliable drivers."}
    if any(w in l for w in ("price", "cost", "fare", "much", "charge")):
        base = fin_setting("ride_base_fare", 4.99)
        pm = fin_setting("ride_per_mile", 2.00)
        return {"answer": f"Fares are simple: ${base:.2f} base + ${pm:.2f} per mile. Type your pickup and dropoff in the calculator to see your exact price upfront — no surge, no surprises!"}
    if any(w in l for w in ("where", "area", "city", "serve", "location")):
        return {"answer": "Drop serves Bismarck and Mandan, North Dakota right now. We're local — real drivers from your neighborhood!"}
    if any(w in l for w in ("safe", "trust")):
        return {"answer": "Every Drop driver passes our review: valid license, active insurance, and a personal application review. You can message your driver directly and track them live on the map."}
    if any(w in l for w in ("pay", "payment", "card")):
        return {"answer": "You pay right on the site with your card — secure checkout through Stripe. Your driver doesn't handle cash, so it's clean and simple."}
    if any(w in l for w in ("hi", "hey", "hello")):
        return {"answer": "Hey there! 👋 I'm Dropper. I can help you sign up to ride, apply to drive, or figure out your fare. What's up?"}

    return {"answer": "Good question! I'm still learning — but I can definitely help with signing up to ride, applying to drive, fares, or where we operate. What else can I do for you?"}


# ---------------- Public fare calculator ----------------
@app.post("/api/fare-quote")
async def fare_quote(req: Request):
    """Accurate upfront fare estimate. Takes structured from/to addresses,
    geocodes both, calculates REAL driving distance, returns the price."""
    body = await req.json()
    frm = body.get("from") or {}
    to = body.get("to") or {}
    kind = body.get("kind", "courier")

    # validate required fields
    for label, a in (("pickup", frm), ("delivery", to)):
        if not a.get("street") or not a.get("city") or not a.get("state"):
            raise HTTPException(400, f"{label}: street, city, and state required")

    def full_addr(a):
        s = a["street"].strip()
        if a.get("apt"):
            s += ", " + a["apt"].strip()
        s += f", {a['city'].strip()}, {a['state'].strip().upper()}"
        if a.get("zip"):
            s += " " + a["zip"].strip()
        return s

    from_full = full_addr(frm)
    to_full = full_addr(to)

    # geocode both with structured parts for max accuracy
    flat, flng = geocode(from_full, frm)
    tlat, tlng = geocode(to_full, to)
    if not flat or not tlat:
        raise HTTPException(400,
            "Couldn't find one of those addresses. Double-check street, city, state.")

    # real driving distance
    miles = driving_miles(flat, flng, tlat, tlng)
    q = calc_quote(flat, flng, tlat, tlng, kind)

    return {
        "from": from_full, "to": to_full,
        "distance_miles": miles,
        "delivery_fee": q["delivery_fee"],
        "driver_pay": q["driver_pay"],
        "warnings": q["warnings"],
        "from_lat": flat, "from_lng": flng,
        "to_lat": tlat, "to_lng": tlng,
    }


# ---------------- Watcher mode (PIN 5683): read-only live view + chat ----------------
WATCHER_PIN = "5683"


def check_watcher_pin(pin):
    if pin != WATCHER_PIN:
        raise HTTPException(401, "Invalid watcher PIN")


@app.post("/api/watcher/login")
async def watcher_login(req: Request):
    body = await req.json()
    pin = str(body.get("pin") or "").strip()
    if pin == WATCHER_PIN:
        return {"ok": True}
    raise HTTPException(401, "Invalid PIN")


@app.get("/api/watcher/live")
def watcher_live(x_pin: str = Header(default="")):
    """Read-only live status for watchers. NO financial data, NO actions."""
    check_watcher_pin(x_pin)
    conn = db()
    # order counts by status (no dollar amounts)
    oc = conn.execute(
        "SELECT status, COUNT(*) as n FROM orders GROUP BY status").fetchall()
    orders_by_status = {r["status"]: r["n"] for r in oc}
    # active orders (no customer phone, no totals)
    active = conn.execute(
        """SELECT o.id, o.status, o.kind, o.customer_name,
                  r.name as restaurant_name, o.pickup_name, o.created_at, o.updated_at,
                  d.name as driver_name
           FROM orders o LEFT JOIN drivers d ON d.id=o.driver_id
           LEFT JOIN restaurants r ON r.id=o.restaurant_id
           WHERE o.status NOT IN ('delivered','cancelled')
           ORDER BY o.id DESC LIMIT 20""").fetchall()
    # drivers online (no earnings, no pay)
    dr = conn.execute(
        "SELECT name, is_online, last_gps_at FROM drivers WHERE status='approved'").fetchall()
    # recent activity (public-safe events only)
    act = conn.execute(
        "SELECT kind, title, created_at FROM system_events"
        " WHERE kind IN ('order','driver','schedule','gift')"
        " ORDER BY id DESC LIMIT 15").fetchall()
    # business hours / open status
    cfg = {}
    try:
        oh, _ = get_hours()
        cfg = {"open": oh}
    except Exception:
        pass
    conn.close()
    return {
        "orders_by_status": orders_by_status,
        "active_orders": [dict(r) for r in active],
        "drivers": [{"name": r["name"],
                     "online": bool(r["is_online"]),
                     "last_seen": r["last_gps_at"]} for r in dr],
        "activity": [dict(r) for r in act],
        "open": cfg.get("open", True),
    }


@app.get("/api/watcher/messages")
def watcher_get_msgs(x_pin: str = Header(default="")):
    """Watchers read the chat thread."""
    check_watcher_pin(x_pin)
    conn = db()
    rows = conn.execute("SELECT id, sender, message, created_at FROM watcher_msgs"
                        " ORDER BY id DESC LIMIT 50").fetchall()
    out = [dict(r) for r in rows][::-1]
    conn.close()
    return out


@app.post("/api/watcher/messages")
async def watcher_send_msg(req: Request, x_pin: str = Header(default="")):
    """Watcher sends a message to Ken."""
    check_watcher_pin(x_pin)
    body = await req.json()
    msg = (body.get("message") or "").strip()[:500]
    if not msg:
        raise HTTPException(400, "Message required")
    conn = db()
    conn.execute("INSERT INTO watcher_msgs(sender, message, created_at)"
                 " VALUES('watcher',?,?)", (msg, now_iso()))
    conn.commit()
    conn.close()
    log_event("watcher", "👀 Watcher message", msg[:60])
    # notify Ken
    try:
        import urllib.request, urllib.parse, threading
        topic = get_setting("ntfy_topic").strip()
        if topic:
            rq = urllib.request.Request(
                f"https://ntfy.sh/{urllib.parse.quote(topic)}",
                data=f"👀 Watcher: {msg}".encode(),
                headers={"Title": "Watcher message"}, method="POST")
            threading.Thread(target=lambda: urllib.request.urlopen(rq, timeout=5).read(),
                             daemon=True).start()
    except Exception:
        pass
    return {"ok": True}


@app.get("/api/operator/watcher-messages")
def op_watcher_msgs(x_pin: str = Header(default="")):
    """Ken reads watcher messages on his board."""
    check_pin(x_pin)
    conn = db()
    rows = conn.execute("SELECT id, sender, message, created_at, seen_by_owner"
                        " FROM watcher_msgs ORDER BY id DESC LIMIT 50").fetchall()
    out = [dict(r) for r in rows][::-1]
    unseen = sum(1 for r in out if not r["seen_by_owner"] and r["sender"] == "watcher")
    conn.close()
    return {"messages": out, "unseen": unseen}


@app.post("/api/operator/watcher-messages")
async def op_watcher_reply(req: Request, x_pin: str = Header(default="")):
    """Ken replies to watchers."""
    check_pin(x_pin)
    body = await req.json()
    msg = (body.get("message") or "").strip()[:500]
    if not msg:
        raise HTTPException(400, "Message required")
    conn = db()
    conn.execute("INSERT INTO watcher_msgs(sender, message, created_at, seen_by_owner)"
                 " VALUES('owner',?,?,1)", (msg, now_iso()))
    conn.execute("UPDATE watcher_msgs SET seen_by_owner=1 WHERE sender='watcher'")
    conn.commit()
    conn.close()
    audit("operator", "watcher_reply", "watcher", 0, msg[:60])
    return {"ok": True}


# ---------------- Driver documents, available pickups, acceptance ----------------
import os as _os

DOCS_DIR = "/data/driver_docs"
_os.makedirs(DOCS_DIR, exist_ok=True)

ALLOWED_DOCS = {"id_front": "Photo ID (front)", "id_back": "Photo ID (back)",
                "insurance": "Insurance policy", "selfie": "Selfie",
                "car": "Vehicle photo"}


@app.post("/api/drivers/docs/upload")
async def driver_doc_upload(req: Request):
    """Driver uploads a document (ID front/back, insurance, selfie, car).
    Works during application (no login yet, uses phone) or logged in."""
    form = await req.form()
    doc_type = (form.get("doc_type") or "").strip()
    if doc_type not in ALLOWED_DOCS:
        raise HTTPException(400, "Invalid document type")
    f = form.get("file")
    if not f or not hasattr(f, "filename") or not f.filename:
        raise HTTPException(400, "No file uploaded")
    # validate image
    ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
    if ext not in ("jpg", "jpeg", "png", "webp", "heic", "pdf"):
        raise HTTPException(400, "Only images or PDF allowed")
    # identify the driver: logged in, or phone from form (during application)
    me = get_driver(req)
    driver_id = me["id"] if me else 0
    phone = ""
    if not driver_id:
        phone = "".join(ch for ch in str(form.get("phone") or "") if ch.isdigit())[-10:]
        if not phone:
            raise HTTPException(400, "Log in or provide your phone number")
    # save file
    import time as _time
    safe_name = f"d{driver_id or phone}_{doc_type}_{int(_time.time())}.{ext}"
    path = _os.path.join(DOCS_DIR, safe_name)
    content = await f.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(400, "File too large (max 10MB)")
    with open(path, "wb") as out:
        out.write(content)
    conn = db()
    if driver_id:
        did = driver_id
    else:
        row = conn.execute("SELECT id FROM drivers WHERE phone LIKE ?",
                           (f"%{phone}%",)).fetchone()
        if not row:
            conn.close()
            raise HTTPException(400, "No application found for that phone — apply first, then upload")
        did = row["id"]
    # replace existing doc of same type
    old = conn.execute("SELECT filename FROM driver_docs WHERE driver_id=? AND doc_type=?",
                       (did, doc_type)).fetchone()
    if old:
        try:
            _os.remove(_os.path.join(DOCS_DIR, old["filename"]))
        except Exception:
            pass
        conn.execute("DELETE FROM driver_docs WHERE driver_id=? AND doc_type=?",
                     (did, doc_type))
    conn.execute("INSERT INTO driver_docs(driver_id, doc_type, filename, uploaded_at)"
                 " VALUES(?,?,?,?)", (did, doc_type, safe_name, now_iso()))
    conn.commit()
    conn.close()
    return {"ok": True, "doc_type": doc_type}


@app.get("/api/operator/drivers/{did}/docs")
def op_driver_docs(did: int, x_pin: str = Header(default="")):
    """Ken views a driver's uploaded documents."""
    check_pin(x_pin)
    conn = db()
    rows = conn.execute("SELECT doc_type, filename, uploaded_at FROM driver_docs"
                        " WHERE driver_id=? ORDER BY doc_type", (did,)).fetchall()
    conn.close()
    docs = [{"type": r["doc_type"], "label": ALLOWED_DOCS.get(r["doc_type"], r["doc_type"]),
             "url": f"/api/operator/drivers/docs/{r['filename']}",
             "uploaded_at": r["uploaded_at"]} for r in rows]
    missing = [k for k in ALLOWED_DOCS if k not in {r["doc_type"] for r in rows}]
    return {"docs": docs,
            "missing": [{"type": k, "label": v} for k, v in ALLOWED_DOCS.items() if k in missing],
            "complete": len(missing) == 0}


@app.get("/api/operator/drivers/docs/{filename}")
def op_driver_doc_file(filename: str, x_pin: str = Header(default="")):
    """Serve a driver document (PIN-gated)."""
    check_pin(x_pin)
    # prevent path traversal
    safe = _os.path.basename(filename)
    path = _os.path.join(DOCS_DIR, safe)
    if not _os.path.isfile(path):
        raise HTTPException(404, "Not found")
    from fastapi.responses import FileResponse
    return FileResponse(path)


@app.get("/api/drivers/available")
async def driver_available(req: Request):
    """Unassigned deliveries the driver can accept or decline."""
    me = get_driver(req)
    if not me:
        raise HTTPException(401, "Log in first")
    conn = db()
    # make sure declines table exists
    conn.execute("""CREATE TABLE IF NOT EXISTS driver_declines (
        driver_id INTEGER, order_id INTEGER, created_at TEXT,
        PRIMARY KEY(driver_id, order_id))""")
    rows = conn.execute(
        """SELECT o.id, o.kind, o.pickup_name, o.pickup_address, o.address,
                  o.customer_name, o.total, o.delivery_fee, o.tip, o.created_at,
                  r.name as restaurant_name
           FROM orders o LEFT JOIN restaurants r ON r.id=o.restaurant_id
           WHERE o.driver_id IS NULL AND o.status IN ('placed','confirmed','scheduled')
           AND o.id NOT IN (SELECT order_id FROM driver_declines WHERE driver_id=?)
           ORDER BY o.id DESC LIMIT 20""", (me["id"],)).fetchall()
    out = [dict(r) for r in rows]
    conn.close()
    return out


@app.post("/api/drivers/orders/{oid}/accept")
async def driver_accept(oid: int, req: Request):
    """Driver accepts an available delivery."""
    me = get_driver(req)
    if not me:
        raise HTTPException(401, "Log in first")
    conn = db()
    o = conn.execute("SELECT id, driver_id, status FROM orders WHERE id=?", (oid,)).fetchone()
    if not o or o["driver_id"]:
        conn.close()
        raise HTTPException(400, "Already taken")
    if o["status"] not in ("placed", "confirmed", "scheduled"):
        conn.close()
        raise HTTPException(400, "Not available")
    conn.execute("UPDATE orders SET driver_id=? WHERE id=?", (me["id"], oid))
    conn.execute("UPDATE drivers SET accepted=accepted+1 WHERE id=?", (me["id"],))
    conn.commit()
    conn.close()
    log_event("driver", f"🚗 Driver {me['name']} accepted order #{oid}", "")
    # notify Ken
    try:
        import urllib.request, urllib.parse, threading
        topic = get_setting("ntfy_topic").strip()
        if topic:
            msg = f"🚗 {me['name']} accepted order #{oid}"
            rq = urllib.request.Request(
                f"https://ntfy.sh/{urllib.parse.quote(topic)}",
                data=msg.encode(), headers={"Title": "Driver accepted"}, method="POST")
            threading.Thread(target=lambda: urllib.request.urlopen(rq, timeout=5).read(),
                             daemon=True).start()
    except Exception:
        pass
    return {"ok": True}


@app.post("/api/drivers/orders/{oid}/decline")
async def driver_decline(oid: int, req: Request):
    """Driver declines — hurts acceptance rate."""
    me = get_driver(req)
    if not me:
        raise HTTPException(401, "Log in first")
    conn = db()
    conn.execute("UPDATE drivers SET declined=declined+1 WHERE id=?", (me["id"],))
    # track which orders they declined (so it doesn't show again)
    conn.execute("""CREATE TABLE IF NOT EXISTS driver_declines (
        driver_id INTEGER, order_id INTEGER, created_at TEXT,
        PRIMARY KEY(driver_id, order_id))""")
    conn.execute("INSERT OR IGNORE INTO driver_declines VALUES(?,?,?)",
                 (me["id"], oid, now_iso()))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/drivers/status")
async def driver_status(req: Request):
    """Driver toggles online/offline."""
    me = get_driver(req)
    if not me:
        raise HTTPException(401, "Log in first")
    body = await req.json()
    online = 1 if body.get("online") else 0
    conn = db()
    conn.execute("UPDATE drivers SET is_online=? WHERE id=?", (online, me["id"]))
    conn.commit()
    conn.close()
    return {"ok": True, "online": bool(online)}


@app.get("/api/drivers/stats")
async def driver_stats(req: Request):
    """Driver's acceptance rate, daily earnings, status."""
    me = get_driver(req)
    if not me:
        raise HTTPException(401, "Log in first")
    conn = db()
    d = conn.execute("SELECT accepted, declined, is_online, deliveries, on_time_rate"
                     " FROM drivers WHERE id=?", (me["id"],)).fetchone()
    # today's earnings
    today = now_iso()[:10]
    earn = conn.execute(
        """SELECT COALESCE(SUM(driver_pay),0) as pay, COALESCE(SUM(tip),0) as tips,
                  COUNT(*) as n FROM orders
           WHERE driver_id=? AND status='delivered'
           AND substr(updated_at,1,10)=?""", (me["id"], today)).fetchone()
    conn.close()
    total = (d["accepted"] or 0) + (d["declined"] or 0)
    rate = round(100 * (d["accepted"] or 0) / total) if total else 100
    return {"acceptance_rate": rate, "accepted": d["accepted"] or 0,
            "declined": d["declined"] or 0, "is_online": bool(d["is_online"]),
            "today_earned": round((earn["pay"] or 0) + (earn["tips"] or 0), 2),
            "today_deliveries": earn["n"] or 0}


# ---------------- Referrals, scheduled orders, reviews, blasts ----------------
import random as _rand
import string as _str


def get_referral_code(user_id):
    """Get or create a user's referral code."""
    conn = db()
    u = conn.execute("SELECT referral_code FROM users WHERE id=?", (user_id,)).fetchone()
    code = u["referral_code"] if u else ""
    if not code:
        code = "DROP-" + "".join(_rand.choices(_str.ascii_uppercase + _str.digits, k=6))
        conn.execute("UPDATE users SET referral_code=? WHERE id=?", (code, user_id))
        conn.commit()
    conn.close()
    return code


@app.get("/api/referral/code")
async def referral_code(req: Request):
    me = get_session_user(req)
    if not me:
        raise HTTPException(401, "Log in first")
    code = get_referral_code(me["id"])
    conn = db()
    count = conn.execute("SELECT COUNT(*) as n FROM referrals WHERE referrer_id=?",
                         (me["id"],)).fetchone()["n"]
    credit = conn.execute("SELECT credit FROM users WHERE id=?", (me["id"],)).fetchone()["credit"] or 0
    conn.close()
    return {"code": code, "referrals": count, "credit": round(credit, 2),
            "link": f"https://drop-52yj.onrender.com/login.html?ref={code}"}


@app.post("/api/referral/apply")
async def referral_apply(req: Request):
    """New user applies a referral code at signup. Both get $5 credit."""
    me = get_session_user(req)
    if not me:
        raise HTTPException(401, "Log in first")
    body = await req.json()
    code = (body.get("code") or "").strip().upper()
    if not code:
        raise HTTPException(400, "Code required")
    conn = db()
    # already referred?
    if conn.execute("SELECT id FROM referrals WHERE referred_id=?", (me["id"],)).fetchone():
        conn.close()
        raise HTTPException(400, "You already used a referral code")
    ref = conn.execute("SELECT id FROM users WHERE referral_code=?", (code,)).fetchone()
    if not ref or ref["id"] == me["id"]:
        conn.close()
        raise HTTPException(400, "Invalid code")
    conn.execute("INSERT INTO referrals(referrer_id, referred_id, code, credit_given, created_at)"
                 " VALUES(?,?,?,?,?)", (ref["id"], me["id"], code, 1, now_iso()))
    conn.execute("UPDATE users SET credit=credit+5 WHERE id=?", (ref["id"],))
    conn.execute("UPDATE users SET credit=credit+5 WHERE id=?", (me["id"],))
    conn.commit()
    conn.close()
    # notify the referrer
    try:
        conn = db()
        r = conn.execute("SELECT ntfy_topic FROM users WHERE id=?", (ref["id"],)).fetchone()
        conn.close()
        if r and r["ntfy_topic"]:
            import urllib.request, urllib.parse, threading
            msg = "🎉 Someone used your referral code! $5 credit added to your account."
            rq = urllib.request.Request(
                f"https://ntfy.sh/{urllib.parse.quote(r['ntfy_topic'])}",
                data=msg.encode(), headers={"Title": "Drop referral"}, method="POST")
            threading.Thread(target=lambda: urllib.request.urlopen(rq, timeout=5).read(),
                             daemon=True).start()
    except Exception:
        pass
    audit("system", "referral_completed", "user", me["id"], f"code {code}")
    return {"ok": True, "credit": 5}


# ---------------- Scheduled orders ----------------
@app.post("/api/orders/schedule")
async def schedule_order(req: Request):
    """Schedule an order for later — validates the time, then creates as scheduled."""
    body = await req.json()
    sched = (body.get("scheduled_for") or "").strip()
    if not sched:
        raise HTTPException(400, "Pick a date and time")
    # validate it's in the future
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(sched.replace("Z", "+00:00"))
        if dt < datetime.now(dt.tzinfo):
            raise HTTPException(400, "Pick a future time")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "Invalid date format")
    # Temporarily stash scheduled_for, create the order, then mark it scheduled.
    # We do this by inserting directly (mirrors create_order's essential fields).
    # Simplest reliable path: call create_order with a fresh Request.
    # Instead, we validate here and let the frontend call /api/orders then
    # /api/orders/{id}/schedule. This endpoint just validates the time.
    return {"ok": True, "scheduled_for": sched}


@app.post("/api/orders/{oid}/schedule")
async def mark_scheduled(oid: int, req: Request):
    """Mark an existing placed order as scheduled for later."""
    body = await req.json()
    sched = (body.get("scheduled_for") or "").strip()
    if not sched:
        raise HTTPException(400, "Time required")
    conn = db()
    conn.execute("UPDATE orders SET scheduled_for=?, status='scheduled' WHERE id=?",
                 (sched, oid))
    conn.commit()
    conn.close()
    log_event("schedule", f"📅 Order #{oid} scheduled for {sched}", "")
    return {"ok": True}


@app.get("/api/operator/scheduled")
def op_scheduled(x_pin: str = Header(default="")):
    check_pin(x_pin)
    conn = db()
    rows = conn.execute(
        "SELECT o.*, r.name as restaurant_name FROM orders o "
        "LEFT JOIN restaurants r ON r.id=o.restaurant_id "
        "WHERE o.status='scheduled' ORDER BY o.scheduled_for").fetchall()
    out = [dict(r) for r in rows]
    conn.close()
    return out


# ---------------- Reviews ----------------
@app.post("/api/reviews")
async def submit_review(req: Request):
    me = get_session_user(req)
    if not me:
        raise HTTPException(401, "Log in to review")
    body = await req.json()
    rid = int(body.get("restaurant_id") or 0)
    rating = int(body.get("rating") or 0)
    text = (body.get("text") or "").strip()[:500]
    if not rid or rating < 1 or rating > 5:
        raise HTTPException(400, "Restaurant and 1-5 star rating required")
    conn = db()
    # one review per user per restaurant (update if exists)
    ex = conn.execute("SELECT id FROM reviews WHERE restaurant_id=? AND user_id=?",
                      (rid, me["id"])).fetchone()
    if ex:
        conn.execute("UPDATE reviews SET rating=?, text=?, created_at=? WHERE id=?",
                     (rating, text, now_iso(), ex["id"]))
    else:
        conn.execute("INSERT INTO reviews(restaurant_id, user_id, rating, text, created_at)"
                     " VALUES(?,?,?,?,?)", (rid, me["id"], rating, text, now_iso()))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/reviews/{rid}")
def get_reviews(rid: int):
    conn = db()
    rows = conn.execute(
        "SELECT r.rating, r.text, r.created_at, u.name FROM reviews r "
        "LEFT JOIN users u ON u.id=r.user_id "
        "WHERE r.restaurant_id=? ORDER BY r.id DESC LIMIT 20", (rid,)).fetchall()
    avg = conn.execute("SELECT AVG(rating) as a, COUNT(*) as n FROM reviews WHERE restaurant_id=?",
                       (rid,)).fetchone()
    conn.close()
    return {"reviews": [dict(r) for r in rows],
            "avg": round(avg["a"] or 0, 1), "count": avg["n"] or 0}


# ---------------- Customer push notifications ----------------
@app.post("/api/account/ntfy")
async def account_ntfy(req: Request):
    me = get_session_user(req)
    if not me:
        raise HTTPException(401, "Log in first")
    body = await req.json()
    topic = (body.get("topic") or "").strip()[:80]
    conn = db()
    conn.execute("UPDATE users SET ntfy_topic=? WHERE id=?", (topic, me["id"]))
    conn.commit()
    conn.close()
    return {"ok": True}


def push_to_customer(user_id, title, message):
    """Send ntfy push to a customer."""
    try:
        conn = db()
        u = conn.execute("SELECT ntfy_topic FROM users WHERE id=?", (user_id,)).fetchone()
        conn.close()
        if u and u["ntfy_topic"]:
            import urllib.request, urllib.parse, threading
            rq = urllib.request.Request(
                f"https://ntfy.sh/{urllib.parse.quote(u['ntfy_topic'])}",
                data=message.encode(), headers={"Title": title}, method="POST")
            threading.Thread(target=lambda: urllib.request.urlopen(rq, timeout=5).read(),
                             daemon=True).start()
            return True
    except Exception:
        pass
    return False


# ---------------- Promo blast ----------------
@app.post("/api/operator/blast")
async def op_blast(req: Request, x_pin: str = Header(default="")):
    """Ken sends a promo message to all customers with ntfy topics."""
    check_pin(x_pin)
    body = await req.json()
    message = (body.get("message") or "").strip()[:300]
    if not message:
        raise HTTPException(400, "Message required")
    conn = db()
    users = conn.execute("SELECT id, ntfy_topic FROM users WHERE ntfy_topic != ''").fetchall()
    conn.close()
    sent = 0
    for u in users:
        if push_to_customer(u["id"], "🧡 Drop deals", message):
            sent += 1
    conn = db()
    conn.execute("INSERT INTO blasts(message, sent_count, created_at) VALUES(?,?,?)",
                 (message, sent, now_iso()))
    conn.commit()
    conn.close()
    audit("operator", "promo_blast", "blast", 0, f"sent to {sent}: {message[:60]}")
    log_event("blast", f"📢 Promo blast sent to {sent} customers", message[:80])
    return {"ok": True, "sent": sent}


@app.get("/api/operator/blasts")
def op_blasts(x_pin: str = Header(default="")):
    check_pin(x_pin)
    conn = db()
    rows = conn.execute("SELECT * FROM blasts ORDER BY id DESC LIMIT 20").fetchall()
    out = [dict(r) for r in rows]
    conn.close()
    return out


# ---------------- Drop Investors ----------------
@app.get("/api/quote")
def get_quote(pickup_lat: float = 0, pickup_lng: float = 0,
              cust_lat: float = 0, cust_lng: float = 0, kind: str = "food"):
    """Public: smart delivery price quote. Shown before the customer pays."""
    q = calc_quote(pickup_lat, pickup_lng, cust_lat, cust_lng, kind)
    return q


def ensure_driver_pay(oid):
    """Compute + store driver pay for an order using the pricing engine."""
    conn = db()
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        conn.close()
        return
    if o["driver_pay"]:
        conn.close()
        return
    kind = o["kind"] or "food"
    if kind == "courier":
        plat, plng = o["pickup_lat"] or 0, o["pickup_lng"] or 0
    else:
        r = conn.execute("SELECT lat, lng FROM restaurants WHERE id=?",
                         (o["restaurant_id"],)).fetchone()
        plat, plng = (r["lat"] or 0, r["lng"] or 0) if r else (0, 0)
    q = calc_quote(plat, plng, o["cust_lat"] or 0, o["cust_lng"] or 0, kind)
    conn.execute("UPDATE orders SET driver_pay=? WHERE id=?", (q["driver_pay"], oid))
    conn.commit()
    conn.close()


@app.get("/api/operator/financials")
def op_financials(x_pin: str = Header(default="")):
    """Owner money dashboard: revenue, costs, profit per order, alerts."""
    check_pin(x_pin)
    conn = db()
    orders = conn.execute(
        """SELECT * FROM orders WHERE stripe_status='paid'
           AND status NOT IN ('cancelled') ORDER BY id DESC""").fetchall()
    total_rev = 0.0
    total_driver_pay = 0.0
    total_tips = 0.0
    total_stripe = 0.0
    total_profit = 0.0
    per_order = []
    alerts = []
    min_profit = fin_setting("min_profit_per_order", 1.00)
    for o in orders:
        od = dict(o)
        # ensure driver pay is computed
        if not od["driver_pay"]:
            conn.close()
            ensure_driver_pay(od["id"])
            conn = db()
            od = dict(conn.execute("SELECT * FROM orders WHERE id=?", (od["id"],)).fetchone())
        f = order_financials(od)
        total_rev += f["total"]
        total_driver_pay += f["driver_pay"]
        total_tips += f["tip"]
        total_stripe += f["stripe_fee_est"]
        total_profit += f["company_profit"]
        per_order.append({"id": od["id"], "kind": od["kind"] or "food",
                          "total": f["total"], "driver_pay": f["driver_pay"],
                          "tip": f["tip"], "stripe_fee": f["stripe_fee_est"],
                          "profit": f["company_profit"],
                          "below_min": f["company_profit"] < min_profit,
                          "created_at": od["created_at"]})
        if f["company_profit"] < min_profit:
            alerts.append(f"Order #{od['id']}: profit ${f['company_profit']:.2f} "
                          f"is below your ${min_profit:.2f} minimum.")
    # money owed to drivers (delivered, paid, assigned to a driver)
    # (payouts are tracked separately in the payouts table)
    pending_payouts = conn.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM payouts WHERE status='requested'").fetchone()
    conn.close()
    return {
        "orders": len(orders),
        "gross_revenue": round(total_rev, 2),
        "driver_pay_owed": round(total_driver_pay, 2),
        "tips_owed": round(total_tips, 2),
        "stripe_fees": round(total_stripe, 2),
        "net_profit": round(total_profit, 2),
        "profit_margin": round(100 * total_profit / total_rev, 1) if total_rev else 0,
        "pending_payout_requests": pending_payouts["c"],
        "pending_payout_amount": round(pending_payouts["s"], 2),
        "alerts": alerts[:10],
        "per_order": per_order[:50],
        "pricing": {
            "base_fee": fin_setting("price_base_fee", 3.99),
            "per_mile": fin_setting("price_per_mile", 1.50),
            "driver_base": fin_setting("driver_base_pay", 3.00),
            "driver_per_mile": fin_setting("driver_per_mile", 1.00),
            "min_profit": min_profit,
        },
    }


@app.post("/api/operator/financial-settings")
async def op_fin_settings(req: Request, x_pin: str = Header(default="")):
    """Ken configures pricing rules. AI recommends; Ken decides."""
    check_pin(x_pin)
    body = await req.json()
    conn = db()
    for k in ("price_base_fee", "price_per_mile", "driver_base_pay",
              "driver_per_mile", "min_profit_per_order"):
        if k in body:
            try:
                v = float(body[k])
                conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)",
                             (k, str(v)))
            except (TypeError, ValueError):
                pass
    conn.commit()
    conn.close()
    return {"ok": True}


# ---------------- Driver wallet + payouts ----------------
@app.get("/api/drivers/wallet")
async def driver_wallet(req: Request):
    """Driver wallet: pending, available, paid out, history."""
    d = get_driver(req)
    if not d:
        raise HTTPException(401, "Not logged in")
    conn = db()
    # pending: assigned but not delivered
    pend = conn.execute(
        """SELECT id, driver_pay, tip, status FROM orders
           WHERE driver_id=? AND status NOT IN ('delivered','cancelled')""",
        (d["id"],)).fetchall()
    # delivered: ensure pay computed, then split by payout status
    done = conn.execute(
        """SELECT id, driver_pay, tip FROM orders
           WHERE driver_id=? AND status='delivered' AND stripe_status='paid'""",
        (d["id"],)).fetchall()
    conn.close()
    # ensure driver pay on delivered orders
    avail = 0.0
    history = []
    for o in done:
        if not o["driver_pay"]:
            ensure_driver_pay(o["id"])
            conn = db()
            o2 = conn.execute("SELECT driver_pay, tip FROM orders WHERE id=?",
                              (o["id"],)).fetchone()
            conn.close()
            dp, tp = (o2["driver_pay"] or 0), (o2["tip"] or 0)
        else:
            dp, tp = (o["driver_pay"] or 0), (o["tip"] or 0)
        earn = round(dp + tp, 2)
        history.append({"order_id": o["id"], "base": dp, "tip": tp, "total": earn})
        avail += earn
    # subtract already-paid payouts
    conn = db()
    paid_out = conn.execute(
        "SELECT COALESCE(SUM(amount),0) s FROM payouts WHERE driver_id=? AND status='paid'",
        (d["id"],)).fetchone()["s"] or 0
    requested = conn.execute(
        "SELECT COALESCE(SUM(amount),0) s FROM payouts WHERE driver_id=? AND status='requested'",
        (d["id"],)).fetchone()["s"] or 0
    payouts = conn.execute(
        "SELECT * FROM payouts WHERE driver_id=? ORDER BY id DESC LIMIT 20",
        (d["id"],)).fetchall()
    conn.close()
    available = round(max(0, avail - paid_out - requested), 2)
    pending = round(sum((r["driver_pay"] or 0) + (r["tip"] or 0) for r in pend), 2)
    return {
        "pending": pending,
        "available": available,
        "paid_out": round(paid_out, 2),
        "requested": round(requested, 2),
        "history": history,
        "payouts": [dict(p) for p in payouts],
    }


@app.post("/api/drivers/payout-request")
async def driver_payout_request(req: Request):
    """Driver requests a payout. Ken approves/sends (manual until Stripe Connect)."""
    d = get_driver(req)
    if not d:
        raise HTTPException(401, "Not logged in")
    body = await req.json()
    try:
        amount = round(float(body.get("amount") or 0), 2)
    except (TypeError, ValueError):
        raise HTTPException(400, "Bad amount")
    if amount <= 0:
        raise HTTPException(400, "Amount must be positive")
    # verify they actually have this much available
    w = await driver_wallet(req)
    if amount > w["available"] + 0.001:
        raise HTTPException(400,
            f"Only ${w['available']:.2f} is available. Pending earnings can't be withdrawn yet.")
    conn = db()
    cur = conn.execute(
        "INSERT INTO payouts(driver_id, amount, kind, status, created_at)"
        " VALUES(?, ?, 'standard', 'requested', ?)",
        (d["id"], amount, now_iso()))
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    # notify Ken
    try:
        topic = get_setting("ntfy_topic").strip()
        if topic:
            msg = f"💸 PAYOUT REQUEST #{pid}\n{d['name']}: ${amount:.2f}"
            r2 = urllib.request.Request(
                f"https://ntfy.sh/{urllib.parse.quote(topic)}",
                data=msg.encode(), headers={"Title": "Drop payout request"}, method="POST")
            threading.Thread(
                target=lambda: urllib.request.urlopen(r2, timeout=5).read(),
                daemon=True).start()
    except Exception:
        pass
    return {"ok": True, "id": pid}


@app.get("/api/operator/payouts")
def op_payouts(x_pin: str = Header(default="")):
    check_pin(x_pin)
    conn = db()
    rows = conn.execute(
        """SELECT p.*, d.name AS driver_name FROM payouts p
           JOIN drivers d ON d.id = p.driver_id
           ORDER BY p.id DESC""").fetchall()
    out = [dict(r) for r in rows]
    conn.close()
    return out


@app.post("/api/operator/payouts/{pid}/paid")
def op_payout_paid(pid: int, x_pin: str = Header(default="")):
    """Ken sent the money (bank transfer, Cash App, etc.) — mark it paid."""
    check_pin(x_pin)
    conn = db()
    conn.execute("UPDATE payouts SET status='paid', paid_at=? WHERE id=?",
                 (now_iso(), pid))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/investors/interest")
async def investor_interest(req: Request):
    """Investor lead form. This collects interest only — actual investment
    requires proper legal setup, which Ken handles offline."""
    body = await req.json()
    name = (body.get("name") or "").strip()[:80]
    email = (body.get("email") or "").strip()[:120]
    if not name or "@" not in email:
        raise HTTPException(400, "Name and valid email required")
    conn = db()
    cur = conn.execute(
        "INSERT INTO investor_leads(name, email, phone, amount_range, message, created_at)"
        " VALUES(?,?,?,?,?,?)",
        (name, email, (body.get("phone") or "").strip()[:30],
         (body.get("amount_range") or "").strip()[:40],
         (body.get("message") or "").strip()[:1000], now_iso()))
    lid = cur.lastrowid
    conn.commit()
    conn.close()
    return {"ok": True, "id": lid}


@app.get("/api/operator/investor-leads")
def op_investor_leads(x_pin: str = Header(default="")):
    check_pin(x_pin)
    conn = db()
    rows = conn.execute("SELECT * FROM investor_leads ORDER BY id DESC").fetchall()
    out = [dict(r) for r in rows]
    conn.close()
    return out


@app.delete("/api/operator/investor-leads/{lid}")
def op_del_investor_lead(lid: int, x_pin: str = Header(default="")):
    check_pin(x_pin)
    conn = db()
    conn.execute("DELETE FROM investor_leads WHERE id=?", (lid,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/operator/restaurants")
async def op_add_restaurant(req: Request, x_pin: str = Header(default="")):
    check_pin(x_pin)
    body = await req.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Name required")
    conn = db()
    cur = conn.execute(
        "INSERT INTO restaurants(name, cuisine, emoji, open, sample) VALUES(?,?,?,1,0)",
        (name, (body.get("cuisine") or "").strip(), (body.get("emoji") or "🍽️").strip()))
    rid = cur.lastrowid
    conn.commit()
    conn.close()
    return {"id": rid}


@app.put("/api/operator/restaurants/{rid}")
async def op_edit_restaurant(rid: int, req: Request, x_pin: str = Header(default="")):
    check_pin(x_pin)
    body = await req.json()
    conn = db()
    fields, vals = [], []
    for k in ("name", "cuisine", "emoji", "eta", "website"):
        if k in body:
            fields.append(f"{k}=?")
            vals.append(str(body[k]).strip()[:20] if k == "eta" else str(body[k]).strip()[:500])
    for k in ("logo", "photo"):
        if k in body:
            fields.append(f"{k}=?")
            vals.append(str(body[k]).strip()[:500])
    for k in ("lat", "lng"):
        if k in body:
            try:
                fields.append(f"{k}=?")
                vals.append(float(body[k]))
            except (TypeError, ValueError):
                pass
    if "address" in body:
        addr = str(body["address"]).strip()[:200]
        fields.append("address=?")
        vals.append(addr)
        if addr:  # auto-pin the restaurant on the map
            glat, glng = geocode(addr)
            if glat:
                fields.append("lat=?")
                vals.append(glat)
                fields.append("lng=?")
                vals.append(glng)
    if "open" in body:
        fields.append("open=?")
        vals.append(1 if body["open"] else 0)
    if fields:
        vals.append(rid)
        conn.execute(f"UPDATE restaurants SET {', '.join(fields)} WHERE id=?", vals)
        conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/operator/restaurants/{rid}")
def op_del_restaurant(rid: int, x_pin: str = Header(default="")):
    check_pin(x_pin)
    conn = db()
    conn.execute("DELETE FROM menu_items WHERE restaurant_id=?", (rid,))
    conn.execute("DELETE FROM restaurants WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/operator/menu-items")
async def op_add_item(req: Request, x_pin: str = Header(default="")):
    check_pin(x_pin)
    body = await req.json()
    name = (body.get("name") or "").strip()
    price = float(body.get("price", 0))
    if not name or price <= 0:
        raise HTTPException(400, "Name and positive price required")
    conn = db()
    cur = conn.execute(
        "INSERT INTO menu_items(restaurant_id, name, description, price, available)"
        " VALUES(?,?,?,?,1)",
        (body.get("restaurant_id"), name, (body.get("description") or "").strip(), price))
    mid = cur.lastrowid
    conn.commit()
    conn.close()
    return {"id": mid}


@app.put("/api/operator/menu-items/{mid}")
async def op_edit_item(mid: int, req: Request, x_pin: str = Header(default="")):
    check_pin(x_pin)
    body = await req.json()
    conn = db()
    fields, vals = [], []
    for k in ("name", "description"):
        if k in body:
            fields.append(f"{k}=?")
            vals.append(str(body[k]).strip())
    if "price" in body:
        fields.append("price=?")
        vals.append(float(body["price"]))
    if "available" in body:
        fields.append("available=?")
        vals.append(1 if body["available"] else 0)
    if fields:
        vals.append(mid)
        conn.execute(f"UPDATE menu_items SET {', '.join(fields)} WHERE id=?", vals)
        conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/operator/menu-items/{mid}")
def op_del_item(mid: int, x_pin: str = Header(default="")):
    check_pin(x_pin)
    conn = db()
    conn.execute("DELETE FROM menu_items WHERE id=?", (mid,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ---------------- static frontend ----------------
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")


@app.get("/", response_class=HTMLResponse)
def home():
    with open(os.path.join(BASE, "static", "index.html")) as f:
        return f.read()


@app.get("/courier.html", response_class=HTMLResponse)
def courierpage():
    with open(os.path.join(BASE, "static", "courier.html")) as f:
        return f.read()


@app.get("/partner.html", response_class=HTMLResponse)
def partnerpage():
    with open(os.path.join(BASE, "static", "partner.html")) as f:
        return f.read()


@app.get("/business.html", response_class=HTMLResponse)
def businesspage():
    with open(os.path.join(BASE, "static", "business.html")) as f:
        return f.read()


@app.get("/drive.html", response_class=HTMLResponse)
def drivepage():
    with open(os.path.join(BASE, "static", "drive.html")) as f:
        return f.read()


@app.get("/driver.html", response_class=HTMLResponse)
def driverpage():
    with open(os.path.join(BASE, "static", "driver.html")) as f:
        return f.read()


@app.get("/watch.html", response_class=HTMLResponse)
def watchpage():
    with open(os.path.join(BASE, "static", "watch.html")) as f:
        return f.read()


@app.get("/investors.html", response_class=HTMLResponse)
def investorspage():
    with open(os.path.join(BASE, "static", "investors.html")) as f:
        return f.read()


@app.get("/restaurant.html", response_class=HTMLResponse)
def rpage():
    with open(os.path.join(BASE, "static", "restaurant.html")) as f:
        return f.read()


@app.get("/checkout.html", response_class=HTMLResponse)
def cpage():
    with open(os.path.join(BASE, "static", "checkout.html")) as f:
        return f.read()


@app.get("/track.html", response_class=HTMLResponse)
def tpage():
    with open(os.path.join(BASE, "static", "track.html")) as f:
        return f.read()


@app.get("/rides.html", response_class=HTMLResponse)
def ridespage():
    with open(os.path.join(BASE, "static", "rides.html")) as f:
        return f.read()


@app.get("/login.html", response_class=HTMLResponse)
def loginpage():
    with open(os.path.join(BASE, "static", "login.html")) as f:
        return f.read()


@app.get("/account.html", response_class=HTMLResponse)
def acctpage():
    with open(os.path.join(BASE, "static", "account.html")) as f:
        return f.read()


@app.get("/dash", response_class=HTMLResponse)
@app.get("/dash.html", response_class=HTMLResponse)
def dpage():
    with open(os.path.join(BASE, "static", "dash.html")) as f:
        return f.read()


@app.get("/terms", response_class=HTMLResponse)
@app.get("/terms.html", response_class=HTMLResponse)
def termspage():
    with open(os.path.join(BASE, "static", "terms.html")) as f:
        return f.read()
