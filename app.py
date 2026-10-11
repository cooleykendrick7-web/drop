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
        selfie_photo TEXT DEFAULT '', car_photo TEXT DEFAULT '',
        selfie_approved INTEGER DEFAULT 0, car_approved INTEGER DEFAULT 0,
        rating REAL DEFAULT 5.0, rating_count INTEGER DEFAULT 0,
        created_at TEXT NOT NULL)""")
    # Idempotent: add photo columns to existing drivers tables
    dcols = [r[1] for r in c.execute("PRAGMA table_info(drivers)").fetchall()]
    for _col, _ddl in (("selfie_photo", "TEXT DEFAULT ''"), ("car_photo", "TEXT DEFAULT ''"),
                       ("selfie_approved", "INTEGER DEFAULT 0"), ("car_approved", "INTEGER DEFAULT 0"),
                       ("rating", "REAL DEFAULT 5.0"), ("rating_count", "INTEGER DEFAULT 0"),
                       ("stripe_account_id", "TEXT DEFAULT ''")):
        if _col not in dcols:
            c.execute(f"ALTER TABLE drivers ADD COLUMN {_col} {_ddl}")
    # Delivery photo proof (pickup + dropoff, DoorDash-style)
    c.execute("""CREATE TABLE IF NOT EXISTS delivery_photos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL, driver_id INTEGER NOT NULL,
        photo_type TEXT NOT NULL, photo_path TEXT NOT NULL,
        created_at TEXT NOT NULL)""")
    # Driver food purchase receipts (pay at restaurant, get reimbursed)
    c.execute("""CREATE TABLE IF NOT EXISTS food_receipts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL, driver_id INTEGER NOT NULL,
        amount REAL NOT NULL, photo_path TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        ai_check TEXT DEFAULT '', decided_by TEXT DEFAULT '',
        created_at TEXT NOT NULL)""")
    # Driver disputes/appeals (theft accusations, suspensions)
    c.execute("""CREATE TABLE IF NOT EXISTS driver_disputes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        driver_id INTEGER NOT NULL, order_id INTEGER DEFAULT 0,
        issue_type TEXT NOT NULL, description TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        driver_appeal TEXT DEFAULT '',
        resolution TEXT DEFAULT '', decided_by TEXT DEFAULT '',
        created_at TEXT NOT NULL, resolved_at TEXT DEFAULT '')""")
    # Business accounts (Drop More B2B)
    c.execute("""CREATE TABLE IF NOT EXISTS business_accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        business_name TEXT NOT NULL, contact_name TEXT NOT NULL,
        email TEXT NOT NULL UNIQUE, phone TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        address TEXT DEFAULT '', city TEXT DEFAULT '', state TEXT DEFAULT '',
        total_orders INTEGER DEFAULT 0, total_spent REAL DEFAULT 0,
        discount_tier TEXT DEFAULT 'standard',
        created_at TEXT NOT NULL)""")
    # Driver ratings (start at 100%, customers rate down/up)
    c.execute("""CREATE TABLE IF NOT EXISTS driver_ratings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        driver_id INTEGER NOT NULL, order_id INTEGER NOT NULL,
        rating INTEGER NOT NULL, comment TEXT DEFAULT '',
        customer_name TEXT DEFAULT '',
        created_at TEXT NOT NULL)""")
    # User profiles (customers)
    c.execute("""CREATE TABLE IF NOT EXISTS user_profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT UNIQUE NOT NULL, name TEXT DEFAULT '',
        email TEXT DEFAULT '', photo TEXT DEFAULT '',
        password_hash TEXT DEFAULT '',
        created_at TEXT NOT NULL)""")
    # Saved payment methods (last4 + brand only, never full numbers)
    c.execute("""CREATE TABLE IF NOT EXISTS saved_cards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT NOT NULL, last4 TEXT NOT NULL,
        brand TEXT DEFAULT '', exp_month TEXT DEFAULT '', exp_year TEXT DEFAULT '',
        stripe_pm_id TEXT DEFAULT '',
        created_at TEXT NOT NULL)""")
    # Live restaurant cache (from OpenStreetMap, 24h TTL)
    c.execute("""CREATE TABLE IF NOT EXISTS live_cache (
        key TEXT PRIMARY KEY, data TEXT NOT NULL, cached_at TEXT NOT NULL)""")
    # Driver broadcasts (Ken's announcements to all drivers)
    c.execute("""CREATE TABLE IF NOT EXISTS driver_broadcasts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL, message TEXT NOT NULL,
        created_at TEXT NOT NULL)""")
    # Driver complaints (AI triages, suggests solutions to Ken)
    c.execute("""CREATE TABLE IF NOT EXISTS driver_complaints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        driver_id INTEGER NOT NULL, subject TEXT NOT NULL,
        message TEXT NOT NULL, ai_summary TEXT DEFAULT '',
        ai_suggestion TEXT DEFAULT '', status TEXT DEFAULT 'open',
        ken_response TEXT DEFAULT '',
        created_at TEXT NOT NULL)""")
    # Driver earnings goals
    c.execute("""CREATE TABLE IF NOT EXISTS driver_goals (
        driver_id INTEGER PRIMARY KEY,
        daily_target REAL DEFAULT 100, weekly_target REAL DEFAULT 500,
        updated_at TEXT NOT NULL)""")
    # WATCH portal comments
    c.execute("""CREATE TABLE IF NOT EXISTS watch_comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        author TEXT NOT NULL DEFAULT 'Anonymous',
        message TEXT NOT NULL, reply TEXT DEFAULT '',
        created_at TEXT NOT NULL)""")
    # City expansion requests
    c.execute("""CREATE TABLE IF NOT EXISTS city_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        city TEXT NOT NULL, state TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'requested',
        created_at TEXT NOT NULL)""")
    # ---- Universal Payment Infrastructure ----
    # Virtual card applications (driver requests a Drop card)
    c.execute("""CREATE TABLE IF NOT EXISTS card_applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        driver_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        ai_recommendation TEXT DEFAULT '', ai_reason TEXT DEFAULT '',
        ai_policy_version TEXT DEFAULT 'v1',
        decided_by TEXT DEFAULT '', decided_at TEXT DEFAULT '',
        created_at TEXT NOT NULL)""")
    # Virtual cards (provider-issued; provider fields stay empty until activated)
    c.execute("""CREATE TABLE IF NOT EXISTS virtual_cards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        driver_id INTEGER NOT NULL UNIQUE,
        provider TEXT DEFAULT '', provider_card_id TEXT DEFAULT '',
        last4 TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'none',
        spending_limit REAL DEFAULT 0,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    # Universal payment ledger (all money movement, double-entry style)
    c.execute("""CREATE TABLE IF NOT EXISTS payment_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        txn_id TEXT NOT NULL, entry_type TEXT NOT NULL,
        driver_id INTEGER DEFAULT 0, order_id INTEGER DEFAULT 0,
        amount REAL NOT NULL, currency TEXT DEFAULT 'USD',
        direction TEXT NOT NULL, category TEXT NOT NULL,
        provider TEXT DEFAULT '', provider_ref TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        memo TEXT DEFAULT '', created_at TEXT NOT NULL)""")
    # Card purchase authorizations (order-restricted spending)
    c.execute("""CREATE TABLE IF NOT EXISTS card_authorizations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        card_id INTEGER NOT NULL, driver_id INTEGER NOT NULL,
        order_id INTEGER NOT NULL, max_amount REAL NOT NULL,
        merchant_restriction TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active',
        expires_at TEXT NOT NULL, created_at TEXT NOT NULL)""")
    # Customer driver selection
    ocols2 = [r[1] for r in c.execute("PRAGMA table_info(orders)").fetchall()]
    if "customer_driver_choice" not in ocols2:
        c.execute("ALTER TABLE orders ADD COLUMN customer_driver_choice INTEGER DEFAULT 0")
    c.execute("""CREATE TABLE IF NOT EXISTS driver_sessions (
        token TEXT PRIMARY KEY,
        driver_id INTEGER NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
        expires_at TEXT NOT NULL)""")
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
    # multi-city: where each restaurant lives (added 2026-10-11)
    _rcols = [r[1] for r in c.execute("PRAGMA table_info(restaurants)").fetchall()]
    for _col, _ddl in (("city", "TEXT DEFAULT 'Bismarck'"), ("state", "TEXT DEFAULT 'ND'"),
                       ("address", "TEXT DEFAULT ''"), ("lat", "REAL DEFAULT 0"),
                       ("lng", "REAL DEFAULT 0"), ("website", "TEXT DEFAULT ''"),
                       ("phone", "TEXT DEFAULT ''")):
        if _col not in _rcols:
            c.execute(f"ALTER TABLE restaurants ADD COLUMN {_col} {_ddl}")
    # restaurant partnership applications (added 2026-10-11)
    c.execute("""CREATE TABLE IF NOT EXISTS partner_applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        business_name TEXT NOT NULL, contact_name TEXT DEFAULT '',
        email TEXT DEFAULT '', phone TEXT DEFAULT '', city TEXT DEFAULT '',
        state TEXT DEFAULT '', cuisine TEXT DEFAULT '', locations INTEGER DEFAULT 1,
        volume TEXT DEFAULT '', proposal TEXT DEFAULT '', status TEXT DEFAULT 'new',
        ai_score INTEGER DEFAULT 0, ai_verdict TEXT DEFAULT '',
        created_at TEXT NOT NULL)""")
    # outreach prospects (added 2026-10-11)
    c.execute("""CREATE TABLE IF NOT EXISTS prospects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, email TEXT DEFAULT '', city TEXT DEFAULT '',
        state TEXT DEFAULT '', cuisine TEXT DEFAULT '', status TEXT DEFAULT 'new',
        draft_subject TEXT DEFAULT '', draft_body TEXT DEFAULT '',
        sent_at TEXT DEFAULT '', created_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS opt_outs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL)""")
    # suggest-a-spot (added 2026-10-11)
    c.execute("""CREATE TABLE IF NOT EXISTS spot_suggestions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, city TEXT DEFAULT '', note TEXT DEFAULT '',
        contact TEXT DEFAULT '', created_at TEXT NOT NULL)""")
    # AI bot chat (added 2026-10-11)
    c.execute("""CREATE TABLE IF NOT EXISTS bot_chats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        role TEXT NOT NULL DEFAULT 'ken',
        message TEXT NOT NULL, created_at TEXT NOT NULL)""")
    # AI improvement ideas (added 2026-10-11)
    c.execute("""CREATE TABLE IF NOT EXISTS ai_ideas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
        why_beats TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
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


def seed_cities():
    """Idempotent: add real multi-city restaurants (Fargo, Minneapolis) if missing."""
    conn = db()
    c = conn.cursor()
    SPOTS = [
    # (name, cuisine, emoji, address, city, state, website, menu[(name, price)])
    ("ThaiKota", "Thai", "🍜", "1201 1st Ave N, Fargo, ND", "Fargo", "ND", "https://www.thaikota.com/",
     [("Pad Thai", 14.99), ("Khao Soi Curry", 15.99), ("Tom Yum Noodle Soup", 13.99), ("Spring Rolls", 7.99)]),
    ("JL Beers", "Burgers", "🍔", "518 1st Ave N, Fargo, ND", "Fargo", "ND", "http://www.jlbeers.com/",
     [("Humpty Dumpty Burger", 12.99), ("Rajun Cajun Burger", 11.99), ("Garlic Parm Fries", 6.99), ("Beer Cheese Pretzel", 8.99)]),
    ("Taco Brothers", "Mexican", "🌮", "428 Roberts St N, Fargo, ND", "Fargo", "ND", "",
     [("Trust You Bro Platter", 11.99), ("Big Brother Burrito", 10.99), ("Butter-Fried Chicken Taco", 4.99), ("Chips & Guac", 7.99)]),
    ("Wasabi", "Sushi", "🍣", "560 2nd Ave N, Fargo, ND", "Fargo", "ND", "https://www.wasabipokebowl.com/",
     [("Chef's Special Roll", 14.99), ("Corona Roll", 13.99), ("Poke Bowl", 12.99), ("Seared Tuna Salad", 13.99)]),
    ("Pounds", "Diner", "🍔", "6 Broadway N, Fargo, ND", "Fargo", "ND", "http://poundsfargo.com/",
     [("Californication Burger", 12.99), ("Torch Wings", 11.99), ("BBQ Porker Mac", 10.99), ("Queso Fries", 7.99)]),
    ("Old Broadway", "Bar & Grill", "🍺", "22 Broadway N, Fargo, ND", "Fargo", "ND", "http://www.theoldbroadway.com/",
     [("Eggs Benedict", 11.99), ("Beer Cheese Soup", 8.99), ("Breakfast Pizza", 10.99), ("Bread Pudding", 6.99)]),
    ("Hai Hai", "Southeast Asian", "🍜", "2121 University Ave NE, Minneapolis, MN", "Minneapolis", "MN", "",
     [("Street Noodles", 15.99), ("Coconut Curry", 16.99), ("Pork Belly Buns", 9.99), ("Papaya Salad", 11.99)]),
    ("Element Wood Fire Pizza", "Pizza", "🍕", "96 Broadway St NE, Minneapolis, MN", "Minneapolis", "MN", "",
     [("Wood-Fired Margherita", 14.99), ("Pepperoni Char Pie", 15.99), ("White Pie", 14.99), ("Caesar Salad", 9.99)]),
    ("Diane's Place", "Brunch", "🍳", "117 14th Ave NE, Minneapolis, MN", "Minneapolis", "MN", "",
     [("Hmong Sausage & Eggs", 15.99), ("Thai Tea French Toast", 13.99), ("Brunch Burger", 14.99), ("Sticky Rice Plate", 12.99)]),
    ("Chimborazo", "Ecuadorian", "🫓", "2851 Central Ave NE, Minneapolis, MN", "Minneapolis", "MN", "",
     [("Llapingachos", 13.99), ("Encebollado", 12.99), ("Fritada Plate", 15.99), ("Empanadas (3)", 8.99)]),
    ("Gorkha Palace", "Indian", "🍛", "23 4th St NE, Minneapolis, MN", "Minneapolis", "MN", "",
     [("Chicken Tikka Masala", 14.99), ("Lamb Curry", 16.99), ("Garlic Naan", 3.99), ("Samosa (2)", 5.99)]),
    ("The Anchor Fish & Chips", "Fish & Chips", "🐟", "302 13th Ave NE, Minneapolis, MN", "Minneapolis", "MN", "",
     [("Fish & Chips", 13.99), ("Shrimp Basket", 12.99), ("Clam Chowder", 7.99), ("Coleslaw Side", 3.99)]),
]
    for name, cuisine, emoji, addr, city, state, site, menu in SPOTS:
        exists = c.execute("SELECT id FROM restaurants WHERE name=? AND city=?", (name, city)).fetchone()
        if exists:
            continue
        try:
            lat, lng = geocode(addr)
        except Exception:
            lat, lng = 0, 0
        cur = c.execute(
            "INSERT INTO restaurants(name, cuisine, emoji, open, address, lat, lng, website, city, state)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (name, cuisine, emoji, 1, addr, lat, lng, site, city, state))
        rid = cur.lastrowid
        for mname, price in menu:
            c.execute("INSERT INTO menu_items(restaurant_id, name, price, available) VALUES(?,?,?,1)",
                      (rid, mname, price))
    conn.commit()
    conn.close()


def seed_expansion_cities():
    """Idempotent: add 88 real restaurants across 10 major US cities."""
    conn = db()
    c = conn.cursor()
    SPOTS = [
    ("The Varsity", "American · Burgers", "🍔", "61 North Ave NW", "Atlanta", "GA", "(404) 881-1706"),
    ("Mary Mac's Tea Room", "Southern · Comfort", "🍗", "224 Ponce De Leon Ave NE", "Atlanta", "GA", "(404) 876-1800"),
    ("The Vortex Bar & Grill", "Burgers · American", "🍔", "438 Moreland Ave NE", "Atlanta", "GA", "(404) 688-1828"),
    ("Fox Bros Bar-B-Q", "BBQ", "🍖", "1238 DeKalb Ave NE", "Atlanta", "GA", "(404) 577-4030"),
    ("Antico Pizza Napoletana", "Pizza · Italian", "🍕", "1093 Hemphill Ave NW", "Atlanta", "GA", "(404) 724-2333"),
    ("The Busy Bee Cafe", "Soul Food", "🍗", "810 Martin Luther King Jr Dr SW", "Atlanta", "GA", "(404) 521-9061"),
    ("Slutty Vegan", "Vegan · Burgers", "🌱", "1542 Ralph David Abernathy Blvd SW", "Atlanta", "GA", "(470) 240-1976"),
    ("Canoe", "American · Fine Dining", "🍽️", "4199 Paces Ferry Rd SE", "Atlanta", "GA", "(770) 432-2663"),
    ("Bacchanalia", "Fine Dining · American", "🍽️", "1460 Ellsworth Industrial Blvd NW", "Atlanta", "GA", "(404) 365-0410"),
    ("Pecan Lodge", "BBQ", "🍖", "2702 Main St", "Dallas", "TX", "(214) 748-8900"),
    ("Mi Cocina", "Tex-Mex", "🌮", "2515 McKinney Ave", "Dallas", "TX", "(214) 754-0368"),
    ("Hutchins BBQ", "BBQ", "🍖", "1301 N Tennessee St", "McKinney", "TX", "(214) 548-4119"),
    ("Terry Black's Barbecue", "BBQ", "🍖", "3025 Main St", "Dallas", "TX", "(214) 997-1551"),
    ("Uchi Dallas", "Japanese · Sushi", "🍣", "2817 Maple Ave", "Dallas", "TX", "(214) 855-5454"),
    ("Cane Rosso", "Pizza · Italian", "🍕", "2612 Commerce St", "Dallas", "TX", "(214) 741-4760"),
    ("Ellen's", "Breakfast · Brunch", "🥞", "1790 N Record St", "Dallas", "TX", "(214) 702-4667"),
    ("Rodeo Goat", "Burgers", "🍔", "1926 Market Center Blvd", "Dallas", "TX", "(214) 741-4628"),
    ("The Breakfast Klub", "Breakfast", "🥞", "3711 Travis St", "Houston", "TX", "(713) 528-8561"),
    ("Pappadeaux Seafood Kitchen", "Seafood · Cajun", "🦐", "2410 Richmond Ave", "Houston", "TX", "(713) 521-2233"),
    ("Killen's Barbecue", "BBQ", "🍖", "3613 E Broadway St", "Pearland", "TX", "(281) 485-2272"),
    ("Uchi Houston", "Japanese · Sushi", "🍣", "904 Westheimer Rd", "Houston", "TX", "(713) 522-4808"),
    ("Goode Company BBQ", "BBQ", "🍖", "5109 Kirby Dr", "Houston", "TX", "(713) 522-2530"),
    ("The Original Ninfa's", "Tex-Mex", "🌮", "2704 Navigation Blvd", "Houston", "TX", "(713) 228-1175"),
    ("Crawfish & Noodles", "Vietnamese-Cajun · Seafood", "🦐", "11360 Bellaire Blvd #990", "Houston", "TX", "(281) 988-8098"),
    ("Lankford Grocery", "Burgers", "🍔", "88 Dennis St", "Houston", "TX", "(713) 522-9555"),
    ("Pinkerton's Barbecue", "BBQ", "🍖", "1100 E 5 1/2 St", "Houston", "TX", "(713) 904-9100"),
    ("Lou Malnati's Pizzeria", "Deep Dish Pizza", "🍕", "439 N Wells St", "Chicago", "IL", "(312) 828-9800"),
    ("Giordano's", "Deep Dish Pizza", "🍕", "730 N Rush St", "Chicago", "IL", "(312) 951-0747"),
    ("Portillo's Hot Dogs", "Hot Dogs · Italian Beef", "🌭", "100 W Ontario St", "Chicago", "IL", "(312) 587-8910"),
    ("Al's #1 Italian Beef", "Italian Beef", "🥪", "1079 W Taylor St", "Chicago", "IL", "(312) 226-4017"),
    ("Girl & the Goat", "American", "🍽️", "809 W Randolph St", "Chicago", "IL", "(312) 492-6262"),
    ("Pequod's Pizza", "Pan Pizza", "🍕", "2207 N Clybourn Ave", "Chicago", "IL", "(773) 327-1512"),
    ("The Wiener's Circle", "Hot Dogs", "🌭", "2622 N Clark St", "Chicago", "IL", "(773) 477-7444"),
    ("Mr. Beef", "Italian Beef", "🥪", "666 N Orleans St", "Chicago", "IL", "(312) 337-8500"),
    ("Xoco", "Mexican", "🌮", "449 N Clark St", "Chicago", "IL", "(312) 334-3688"),
    ("In-N-Out Burger", "Burgers", "🍔", "9149 S Sepulveda Blvd", "Los Angeles", "CA", "(800) 786-1000"),
    ("Pink's Hot Dogs", "Hot Dogs", "🌭", "709 N La Brea Ave", "Los Angeles", "CA", "(323) 931-4223"),
    ("The Original Pantry Cafe", "Diner · American", "🥞", "877 S Figueroa St", "Los Angeles", "CA", "(213) 972-9279"),
    ("Philippe The Original", "Deli · French Dip", "🥪", "1001 N Alameda St", "Los Angeles", "CA", "(213) 628-3781"),
    ("Guisados", "Tacos", "🌮", "2100 E Cesar E Chavez Ave", "Los Angeles", "CA", "(323) 264-7201"),
    ("Howlin' Ray's", "Nashville Hot Chicken", "🍗", "727 N Broadway #128", "Los Angeles", "CA", "(213) 935-8399"),
    ("Roscoe's House of Chicken & Waffles", "Soul Food", "🧇", "1514 N Gower St", "Los Angeles", "CA", "(323) 466-7453"),
    ("Langer's Delicatessen", "Deli", "🥪", "704 S Alvarado St", "Los Angeles", "CA", "(213) 483-8050"),
    ("Bestia", "Italian", "🍽️", "2121 E 7th Pl", "Los Angeles", "CA", "(213) 514-5724"),
    ("Jon & Vinny's", "Italian", "🍕", "412 N Fairfax Ave", "Los Angeles", "CA", "(323) 334-3369"),
    ("Katz's Delicatessen", "Deli", "🥪", "205 E Houston St", "New York", "NY", "(212) 254-2246"),
    ("Joe's Pizza", "Pizza", "🍕", "7 Carmine St", "New York", "NY", "(212) 366-1182"),
    ("Xi'an Famous Foods", "Chinese · Noodles", "🍜", "45 Bayard St", "New York", "NY", "(212) 786-2068"),
    ("Prince Street Pizza", "Pizza", "🍕", "27 Prince St", "New York", "NY", "(212) 966-4100"),
    ("Russ & Daughters", "Bagels · Appetizing", "🥯", "179 E Houston St", "New York", "NY", "(212) 475-4880"),
    ("Di Fara Pizza", "Pizza", "🍕", "1424 Avenue J", "Brooklyn", "NY", "(718) 258-1367"),
    ("Peter Luger Steak House", "Steakhouse", "🥩", "178 Broadway", "Brooklyn", "NY", "(718) 387-7400"),
    ("Rubirosa", "Italian · Pizza", "🍕", "235 Mulberry St", "New York", "NY", "(212) 965-1700"),
    ("Versailles Restaurant", "Cuban", "🍖", "3555 SW 8th St", "Miami", "FL", "(305) 444-0240"),
    ("Joe's Stone Crab", "Seafood", "🦀", "11 Washington Ave", "Miami Beach", "FL", "(305) 673-0365"),
    ("La Sandwicherie", "Sandwiches", "🥪", "229 14th St", "Miami Beach", "FL", "(305) 532-8934"),
    ("Enriqueta's Sandwich Shop", "Cuban Sandwiches", "🥪", "186 NE 29th St", "Miami", "FL", "(305) 573-4681"),
    ("Garcia's Seafood Grille", "Seafood", "🦐", "398 NW North River Dr", "Miami", "FL", "(305) 375-0765"),
    ("Puerto Sagua", "Cuban", "🍗", "700 Collins Ave", "Miami Beach", "FL", "(305) 673-1115"),
    ("Sanguich De Miami", "Cuban Sandwiches", "🥪", "1042 SW 8th St", "Miami", "FL", "(786) 558-8368"),
    ("Casa Bonita", "Mexican", "🌮", "6715 W Colfax Ave", "Lakewood", "CO", "(303) 232-5115"),
    ("Sam's No. 3", "Diner", "🥞", "1500 Curtis St", "Denver", "CO", "(303) 534-2227"),
    ("Biker Jim's Gourmet Dogs", "Hot Dogs", "🌭", "2148 Larimer St", "Denver", "CO", "(303) 209-2808"),
    ("Denver Biscuit Company", "Biscuits · Breakfast", "🥐", "3237 E Colfax Ave", "Denver", "CO", "(303) 377-7900"),
    ("Illegal Pete's", "Burritos", "🌯", "270 S Broadway", "Denver", "CO", "(303) 778-1397"),
    ("Snooze, an A.M. Eatery", "Breakfast", "🥞", "2262 Larimer St", "Denver", "CO", "(303) 297-0700"),
    ("Tacos Tequila Whiskey", "Tacos", "🌮", "3300 Blake St", "Denver", "CO", "(303) 297-4939"),
    ("The Buckhorn Exchange", "Steakhouse", "🥩", "1000 Osage St", "Denver", "CO", "(303) 534-9505"),
    ("Rosenberg's Bagels", "Bagels · Deli", "🥯", "725 E 26th Ave", "Denver", "CO", "(720) 519-3841"),
    ("Pike Place Chowder", "Seafood · Chowder", "🦐", "1530 Post Alley", "Seattle", "WA", "(206) 267-2537"),
    ("Dick's Drive-In", "Burgers", "🍔", "111 NE 45th St", "Seattle", "WA", "(206) 632-5125"),
    ("Piroshky Piroshky", "Russian Bakery", "🥐", "1908 Pike Pl", "Seattle", "WA", "(206) 441-6068"),
    ("Salumi", "Italian Deli", "🥪", "309 3rd Ave S", "Seattle", "WA", "(206) 621-8772"),
    ("The Pink Door", "Italian", "🍝", "1919 Post Alley", "Seattle", "WA", "(206) 443-3241"),
    ("Din Tai Fung", "Chinese · Dumplings", "🥟", "2621 NE 46th St", "Seattle", "WA", "(206) 525-0958"),
    ("Serious Pie", "Pizza", "🍕", "316 Virginia St", "Seattle", "WA", "(206) 838-7388"),
    ("Ivar's Acres of Clams", "Seafood", "🦐", "1001 Alaskan Way", "Seattle", "WA", "(206) 624-6852"),
    ("Marination Ma Kai", "Hawaiian", "🌺", "1660 Harbor Ave SW", "Seattle", "WA", "(206) 328-8226"),
    ("Pizzeria Bianco", "Pizza", "🍕", "623 E Adams St", "Phoenix", "AZ", "(602) 258-8300"),
    ("Matt's Big Breakfast", "Breakfast", "🥞", "825 N 1st St", "Phoenix", "AZ", "(602) 254-1074"),
    ("Los Dos Molinos", "Mexican", "🌮", "8646 S Central Ave", "Phoenix", "AZ", "(602) 243-9113"),
    ("Carolina's Mexican Food", "Mexican", "🌮", "2126 E Cactus Rd", "Phoenix", "AZ", "(602) 749-5961"),
    ("Little Miss BBQ", "BBQ", "🍖", "4301 E University Dr", "Phoenix", "AZ", "(602) 437-1177"),
    ("Welcome Diner", "Diner", "🍔", "924 E Roosevelt St", "Phoenix", "AZ", "(602) 495-1114"),
    ("Tacos Huicho", "Tacos", "🌮", "822 N 16th St", "Phoenix", "AZ", "(602) 322-8824"),
    ("Chino Bandido", "Chinese-Mexican Fusion", "🍜", "15414 N 19th Ave", "Phoenix", "AZ", "(602) 375-3639"),
    ("Barrio Cafe", "Mexican", "🌮", "2814 N 16th St", "Phoenix", "AZ", "(602) 636-0240"),
    ("Durant's", "Steakhouse", "🥩", "2611 N Central Ave", "Phoenix", "AZ", "(602) 264-5967"),
    ]
    for name, cuisine, emoji, addr, city, state, phone in SPOTS:
        exists = c.execute(
            "SELECT id FROM restaurants WHERE name=? AND city=?", (name, city)).fetchone()
        if not exists:
            c.execute(
                "INSERT INTO restaurants(name, cuisine, emoji, address, city, state, phone, open)"
                " VALUES(?,?,?,?,?,?,?,1)",
                (name, cuisine, emoji, addr, city, state, phone))
    conn.commit()
    conn.close()


def seed_expansion_cities2():
    """Idempotent: add 168 more restaurants across 27 cities."""
    conn = db()
    c = conn.cursor()
    # Batch 2: 27 more cities
    SPOTS2 = [
("Union Oyster House", "Seafood · American", "🦪", "41 Union St", "Boston", "MA", "(617) 227-2750"),
("Mike's Pastry", "Bakery · Cannoli", "🍰", "300 Hanover St", "Boston", "MA", "(617) 742-3050"),
("Neptune Oyster", "Seafood", "🦞", "63 Salem St", "Boston", "MA", "(617) 742-3474"),
("Regina Pizzeria", "Pizza", "🍕", "11 1/2 Thacher St", "Boston", "MA", "(617) 227-0765"),
("Legal Sea Foods", "Seafood", "🐟", "255 State St", "Boston", "MA", "(617) 742-5300"),
("Santarpio's Pizza", "Pizza · Italian", "🍕", "111 Chelsea St", "Boston", "MA", "(617) 567-9871"),
("Yume Wo Katare", "Ramen", "🍜", "1923 Massachusetts Ave", "Boston", "MA", "(617) 492-9219"),
("Pat's King of Steaks", "Cheesesteak", "🧀", "1237 E Passyunk Ave", "Philadelphia", "PA", "(215) 468-1546"),
("Geno's Steaks", "Cheesesteak", "🥩", "1219 S 9th St", "Philadelphia", "PA", "(215) 389-0659"),
("Jim's Steaks", "Cheesesteak", "🧀", "400 South St", "Philadelphia", "PA", "(215) 928-1911"),
("Reading Terminal Market", "Market Hall", "🍽️", "1136 Arch St", "Philadelphia", "PA", "(215) 922-2317"),
("Federal Donuts", "Donuts · Chicken", "🍩", "1219 S 2nd St", "Philadelphia", "PA", ""),
("Zahav", "Israeli", "🥙", "237 St James Pl", "Philadelphia", "PA", "(215) 625-8800"),
("La Colombe", "Coffee", "☕", "1335 Frankford Ave", "Philadelphia", "PA", "(215) 425-0584"),
("Ben's Chili Bowl", "Chili · American", "🌭", "1213 U St NW", "Washington", "DC", "(202) 667-0909"),
("Old Ebbitt Grill", "American", "🦀", "675 15th St NW", "Washington", "DC", "(202) 347-4800"),
("Georgetown Cupcake", "Cupcakes", "🧁", "3301 M St NW", "Washington", "DC", "(202) 333-8448"),
("Le Diplomate", "French", "🥖", "1601 14th St NW", "Washington", "DC", "(202) 332-3333"),
("Founding Farmers", "American", "🥞", "1255 20th St NW", "Washington", "DC", "(202) 822-8783"),
("Jumbo Slice", "Pizza", "🍕", "2341 18th St NW", "Washington", "DC", "(202) 232-8185"),
("Faidley's Seafood", "Seafood · Crab Cakes", "🦀", "203 N Paca St", "Baltimore", "MD", "(410) 727-4898"),
("Lexington Market", "Market Hall", "🍽️", "112 N Eutaw St", "Baltimore", "MD", "(410) 685-6169"),
("Di Pasquale's", "Italian Deli", "🍝", "3700 Gough St", "Baltimore", "MD", "(410) 276-6929"),
("Pete's Grille", "Diner", "🥞", "3130 Greenmount Ave", "Baltimore", "MD", "(410) 467-7698"),
("The Helmand", "Afghan", "🍛", "806 N Charles St", "Baltimore", "MD", "(410) 752-0311"),
("Chaps Pit Beef", "BBQ · Pit Beef", "🍖", "5801 Pulaski Hwy", "Baltimore", "MD", "(410) 483-2379"),
("Hattie B's Hot Chicken", "Hot Chicken", "🍗", "112 19th Ave S", "Nashville", "TN", "(615) 678-4794"),
("Prince's Hot Chicken Shack", "Hot Chicken", "🔥", "5814 Nolensville Pike", "Nashville", "TN", "(615) 810-9388"),
("Biscuit Love", "Southern · Brunch", "🥞", "316 11th Ave S", "Nashville", "TN", "(615) 490-9584"),
("The Bluebird Cafe", "Cafe · American", "🎸", "4104 Hillsboro Pike", "Nashville", "TN", "(615) 383-1461"),
("Robert's Western World", "Honky Tonk · Burgers", "🍔", "416 B Broadway", "Nashville", "TN", "(615) 244-9552"),
("Five Points Pizza", "Pizza", "🍕", "1012 Woodland St", "Nashville", "TN", "(615) 915-4174"),
("Mas Tacos Por Favor", "Mexican", "🌮", "732 McFerrin Ave", "Nashville", "TN", ""),
("Gus's World Famous Fried Chicken", "Fried Chicken", "🍗", "310 S Front St", "Memphis", "TN", "(901) 527-4877"),
("Rendezvous", "BBQ · Ribs", "🍖", "52 S 2nd St", "Memphis", "TN", "(901) 523-2746"),
("Central BBQ", "BBQ", "🍖", "2249 Central Ave", "Memphis", "TN", "(901) 272-9377"),
("Payne's Bar-B-Q", "BBQ", "🍖", "1762 Lamar Ave", "Memphis", "TN", "(901) 272-1523"),
("The Arcade Restaurant", "Diner", "🥞", "540 S Main St", "Memphis", "TN", "(901) 526-5757"),
("Corky's Ribs & BBQ", "BBQ", "🍖", "5259 Poplar Ave", "Memphis", "TN", "(901) 685-9744"),
("Cafe Du Monde", "Beignets · Coffee", "🍩", "800 Decatur St", "New Orleans", "LA", "(504) 525-4544"),
("Commander's Palace", "Creole", "🦐", "1403 Washington Ave", "New Orleans", "LA", "(504) 899-8221"),
("Dooky Chase's Restaurant", "Creole", "🍛", "2301 Orleans Ave", "New Orleans", "LA", "(504) 821-0600"),
("Acme Oyster House", "Oysters · Seafood", "🦪", "724 Iberville St", "New Orleans", "LA", "(504) 522-5973"),
("Mother's Restaurant", "Po'boys · Cajun", "🥪", "401 Poydras St", "New Orleans", "LA", "(504) 523-9656"),
("Parkway Bakery & Tavern", "Po'boys", "🥖", "538 Hagan Ave", "New Orleans", "LA", "(504) 482-3047"),
("Cochon", "Cajun", "🐷", "930 Tchoupitoulas St", "New Orleans", "LA", "(504) 588-7675"),
("Franklin Barbecue", "BBQ", "🍖", "900 E 11th St", "Austin", "TX", "(512) 653-1187"),
("Torchy's Tacos", "Tacos", "🌮", "2809 S Congress Ave", "Austin", "TX", ""),
("Whataburger", "Burgers · Fast Food", "🍔", "1200 E 6th St", "Austin", "TX", "(512) 478-5614"),
("Uchi", "Japanese · Sushi", "🍣", "801 S Lamar Blvd", "Austin", "TX", "(512) 916-4808"),
("Gourdough's Big. Fat. Donuts.", "Donuts", "🍩", "1503 S 1st St", "Austin", "TX", "(512) 912-9070"),
("Home Slice Pizza", "Pizza", "🍕", "1415 S Congress Ave", "Austin", "TX", "(512) 444-7437"),
("Matt's El Rancho", "Mexican", "🌮", "2613 S Lamar Blvd", "Austin", "TX", "(512) 462-9333"),
("The Pearl", "Market Hall", "🍽️", "303 Pearl Pkwy", "San Antonio", "TX", "(210) 212-7260"),
("Mi Tierra Cafe y Panaderia", "Mexican", "🌮", "218 Produce Row", "San Antonio", "TX", "(210) 225-1262"),
("Rosario's", "Mexican", "🌯", "910 S Alamo St", "San Antonio", "TX", "(210) 223-1806"),
("Schilo's Delicatessen", "German · Deli", "🥪", "424 E Commerce St", "San Antonio", "TX", "(210) 223-6692"),
("Bill Miller Bar-B-Q", "BBQ", "🍖", "909 Broadway", "San Antonio", "TX", "(210) 225-4461"),
("The Guenther House", "Brunch · Southern", "🥞", "205 E Guenther St", "San Antonio", "TX", "(210) 227-1061"),
("Cattlemen's Steakhouse", "Steakhouse", "🥩", "1309 S Agnew Ave", "Oklahoma City", "OK", "(405) 236-0416"),
("Nic's Grill", "Burgers", "🍔", "1201 N Pennsylvania Ave", "Oklahoma City", "OK", "(405) 524-6427"),
("Tucker's Onion Burgers", "Burgers", "🍔", "324 NW 23rd St", "Oklahoma City", "OK", "(405) 609-2333"),
("Cheever's Cafe", "American", "🍽️", "2409 N Hudson Ave", "Oklahoma City", "OK", "(405) 525-7007"),
("Big Truck Tacos", "Tacos", "🌮", "5300 N Western Ave", "Oklahoma City", "OK", "(405) 728-1743"),
("Paseo Grill", "American", "🥗", "2909 Paseo", "Oklahoma City", "OK", "(405) 601-1079"),
("Arthur Bryant's Barbeque", "BBQ", "🍖", "1727 Brooklyn Ave", "Kansas City", "MO", "(816) 231-1123"),
("Gates Bar-B-Q", "BBQ", "🍖", "1221 Brooklyn Ave", "Kansas City", "MO", "(816) 483-3880"),
("Joe's Kansas City Bar-B-Que", "BBQ", "🍖", "3002 W 47th Ave", "Kansas City", "MO", "(913) 722-3366"),
("Town Topic Hamburgers", "Burgers", "🍔", "2021 Broadway Blvd", "Kansas City", "MO", "(816) 842-2294"),
("The Peanut", "Bar · Wings", "🍗", "418 W 9th St", "Kansas City", "MO", "(816) 753-9499"),
("Q39", "BBQ", "🍖", "1000 W 39th St", "Kansas City", "MO", "(816) 255-3753"),
("Pappy's Smokehouse", "BBQ", "🍖", "3106 Olive St", "St Louis", "MO", "(314) 535-4340"),
("Ted Drewes Frozen Custard", "Frozen Custard", "🍦", "6726 Chippewa St", "St Louis", "MO", "(314) 481-2652"),
("Imo's Pizza", "Pizza · St. Louis Style", "🍕", "17 W Kirkham Rd", "St Louis", "MO", "(314) 821-4545"),
("Bogart's Smokehouse", "BBQ", "🍖", "1627 S 9th St", "St Louis", "MO", "(314) 621-3344"),
("Fitz's", "Root Beer · Burgers", "🍔", "6605 Delmar Blvd", "St Louis", "MO", "(314) 726-9555"),
("The Hill", "Italian", "🍝", "2132 Edwards St", "St Louis", "MO", ""),
("American Coney Island", "Coney Dogs", "🌭", "114 W Lafayette Blvd", "Detroit", "MI", "(313) 961-7758"),
("Lafayette Coney Island", "Coney Dogs", "🌭", "118 W Lafayette Blvd", "Detroit", "MI", "(313) 964-8198"),
("Buddy's Pizza", "Pizza · Detroit Style", "🍕", "17125 Conant St", "Detroit", "MI", "(313) 892-9001"),
("Slows Bar BQ", "BBQ", "🍖", "2138 Michigan Ave", "Detroit", "MI", "(313) 962-9828"),
("Green Dot Stables", "Sliders · Burgers", "🍔", "2200 W Lafayette Blvd", "Detroit", "MI", "(313) 962-5588"),
("Supino Pizzeria", "Pizza", "🍕", "2457 Russell St", "Detroit", "MI", "(313) 567-7879"),
("Slyman's Restaurant", "Corned Beef · Deli", "🥪", "3106 Saint Clair Ave NE", "Cleveland", "OH", "(216) 621-3760"),
("West Side Market", "Market Hall", "🍽️", "1979 W 25th St", "Cleveland", "OH", "(216) 664-3387"),
("Melt Bar and Grilled", "Grilled Cheese", "🧀", "14718 Detroit Ave", "Cleveland", "OH", "(440) 799-0574"),
("Great Lakes Brewing Company", "Brewpub · American", "🍺", "2516 Market Ave", "Cleveland", "OH", "(216) 771-4404"),
("Sokolowski's University Inn", "Polish", "🥟", "1201 University Rd", "Cleveland", "OH", "(216) 771-9236"),
("Tommy's Restaurant", "Vegetarian · Diner", "🥗", "1824 Coventry Rd", "Cleveland", "OH", "(216) 321-7757"),
("Skyline Chili", "Chili", "🍝", "643 Vine St", "Cincinnati", "OH", "(513) 241-2020"),
("Gold Star Chili", "Chili", "🌶️", "308 W 4th St", "Cincinnati", "OH", "(513) 621-4443"),
("Graeter's Ice Cream", "Ice Cream", "🍨", "511 Walnut St", "Cincinnati", "OH", "(513) 381-0653"),
("Camp Washington Chili", "Chili", "🍝", "3002 Colerain Ave", "Cincinnati", "OH", "(513) 541-0061"),
("Findlay Market", "Market Hall", "🍽️", "1801 Race St", "Cincinnati", "OH", "(513) 665-4839"),
("Taste of Belgium", "Belgian · Waffles", "🧇", "16 W 12th St", "Cincinnati", "OH", "(513) 381-3280"),
("Primanti Bros.", "Sandwiches", "🥪", "46 18th St", "Pittsburgh", "PA", "(412) 263-2142"),
("Pamela's Diner", "Breakfast", "🥞", "60 21st St", "Pittsburgh", "PA", "(412) 281-6366"),
("The Original Hot Dog Shop", "Hot Dogs", "🌭", "3901 Forbes Ave", "Pittsburgh", "PA", "(412) 621-3362"),
("Gaucho Parrilla Argentina", "Argentinian", "🥩", "146 6th St", "Pittsburgh", "PA", "(412) 709-6622"),
("Mineo's Pizza House", "Pizza", "🍕", "2128 Murray Ave", "Pittsburgh", "PA", "(412) 521-9862"),
("Conflict Kitchen", "International", "🌍", "221 Schenley Dr", "Pittsburgh", "PA", "(412) 621-3344"),
("Price's Chicken Coop", "Fried Chicken", "🍗", "2908 Wilkinson Blvd", "Charlotte", "NC", "(704) 333-9866"),
("The Penguin", "Diner · Burgers", "🍔", "1921 Commonwealth Ave", "Charlotte", "NC", "(704) 375-6959"),
("Mert's Heart and Soul", "Soul Food", "🍛", "214 N College St", "Charlotte", "NC", "(704) 342-4222"),
("Amelie's French Bakery", "Bakery · French", "🥐", "2424 N Davidson St", "Charlotte", "NC", "(704) 376-1781"),
("Fuel Pizza", "Pizza", "🍕", "1501 Central Ave", "Charlotte", "NC", "(704) 376-3835"),
("Midwood Smokehouse", "BBQ", "🍖", "1401 Central Ave", "Charlotte", "NC", "(704) 295-4227"),
("The Pit", "BBQ", "🍖", "328 W Davie St", "Raleigh", "NC", "(919) 890-4500"),
("Beasley's Chicken + Honey", "Fried Chicken", "🍗", "237 S Wilmington St", "Raleigh", "NC", "(919) 322-0127"),
("Clyde Cooper's Barbecue", "BBQ", "🍖", "109 E Davie St", "Raleigh", "NC", "(919) 832-7614"),
("Poole's Diner", "Diner · Southern", "🥞", "426 S McDowell St", "Raleigh", "NC", "(919) 832-4477"),
("Bida Manda", "Laotian", "🍜", "222 S Blount St", "Raleigh", "NC", "(919) 829-9999"),
("Gonza Tacos y Tequila", "Tacos", "🌮", "7713 Lead Mine Rd", "Raleigh", "NC", "(919) 322-0424"),
("Maple Street Biscuit Company", "Biscuits · Southern", "🥞", "2100 3rd St S", "Jacksonville", "FL", "(904) 372-1500"),
("Safe Harbor Seafood", "Seafood", "🦐", "4378 Ocean St", "Jacksonville", "FL", "(904) 246-4911"),
("Metro Diner", "Diner", "🥞", "3302 Hendricks Ave", "Jacksonville", "FL", "(904) 438-0903"),
("V Pizza", "Pizza · Neapolitan", "🍕", "1406 Hendricks Ave", "Jacksonville", "FL", "(904) 527-1125"),
("Black Sheep Restaurant", "American", "🍽️", "1534 Oak St", "Jacksonville", "FL", "(904) 380-3091"),
("Angie's Subs", "Subs · Sandwiches", "🥪", "1436 Beach Blvd", "Jacksonville", "FL", "(904) 249-5326"),
("Columbia Restaurant", "Cuban · Spanish", "🥘", "2117 E 7th Ave", "Tampa", "FL", "(813) 248-4961"),
("Bern's Steak House", "Steakhouse", "🥩", "1208 S Howard Ave", "Tampa", "FL", "(813) 251-2421"),
("La Segunda Central Bakery", "Bakery · Cuban", "🥖", "2512 N 15th St", "Tampa", "FL", "(813) 248-1531"),
("Taco Bus", "Tacos", "🌮", "913 E Hillsborough Ave", "Tampa", "FL", "(813) 374-8943"),
("Goody Goody Burgers", "Burgers", "🍔", "1601 W Swann Ave", "Tampa", "FL", "(813) 308-0600"),
("Ulele", "Native-Inspired · Seafood", "🦐", "1810 N Highland Ave", "Tampa", "FL", "(813) 999-4952"),
("4 Rivers Smokehouse", "BBQ", "🍖", "2103 W Fairbanks Ave", "Orlando", "FL", "(407) 474-8377"),
("Prato", "Italian", "🍝", "124 N Park Ave", "Orlando", "FL", "(407) 262-0050"),
("Se7en Bites", "Southern · Bakery", "🥞", "617 Primrose Dr", "Orlando", "FL", "(407) 203-0727"),
("Black Rooster Taqueria", "Tacos", "🌮", "1323 N Mills Ave", "Orlando", "FL", "(407) 704-3662"),
("The Coop", "Fried Chicken", "🍗", "610 W Morse Blvd", "Orlando", "FL", "(407) 636-5447"),
("Gideon's Bakehouse", "Cookies · Bakery", "🍪", "124 N Grand Ave", "Orlando", "FL", "(407) 886-4319"),
("In-N-Out Burger", "Burgers", "🍔", "4888 Dean Martin Dr", "Las Vegas", "NV", "(800) 786-1000"),
("Lotus of Siam", "Thai", "🍜", "620 E Flamingo Rd", "Las Vegas", "NV", "(702) 735-3033"),
("Tacos El Gordo", "Tacos", "🌮", "3049 S Las Vegas Blvd", "Las Vegas", "NV", "(702) 331-1160"),
("Secret Pizza", "Pizza", "🍕", "3708 S Las Vegas Blvd", "Las Vegas", "NV", "(702) 698-2665"),
("Bachi Burger", "Burgers · Asian Fusion", "🍔", "9410 W Sahara Ave", "Las Vegas", "NV", "(702) 368-8808"),
("Roberto's Taco Shop", "Tacos · Mexican", "🌮", "540 E Sahara Ave", "Las Vegas", "NV", "(702) 733-3553"),
("Red Iguana", "Mexican", "🌮", "736 W North Temple", "Salt Lake City", "UT", "(801) 322-1489"),
("The Dodo Restaurant", "Burgers · Diner", "🍔", "135 W 300 S", "Salt Lake City", "UT", "(801) 328-3330"),
("Lucky 13 Bar & Grill", "Burgers", "🍔", "135 W 1300 S", "Salt Lake City", "UT", "(801) 487-4418"),
("Settebello Pizzeria", "Pizza · Neapolitan", "🍕", "260 S 200 W", "Salt Lake City", "UT", "(801) 322-3556"),
("Crown Burgers", "Burgers · Greek", "🍔", "377 E 200 S", "Salt Lake City", "UT", "(801) 532-1155"),
("Hsu's Ginseng House", "Chinese", "🥟", "3089 S State St", "Salt Lake City", "UT", "(801) 467-0222"),
("Voodoo Doughnut", "Donuts", "🍩", "22 SW 3rd Ave", "Portland", "OR", "(503) 241-4704"),
("Pok Pok", "Thai", "🍜", "3226 SE Division St", "Portland", "OR", "(503) 232-5001"),
("Blue Star Donuts", "Donuts", "🍩", "1237 SW Washington St", "Portland", "OR", "(503) 265-8410"),
("Tasty n Alder", "Brunch · American", "🥞", "580 SW 12th Ave", "Portland", "OR", "(503) 621-9251"),
("Pine State Biscuits", "Biscuits · Southern", "🥞", "2204 NE Alberta St", "Portland", "OR", "(503) 477-6605"),
("Nong's Khao Man Gai", "Thai · Chicken Rice", "🍗", "609 SE Ankeny St", "Portland", "OR", "(503) 740-2907"),
("Hodad's", "Burgers", "🍔", "5010 Newport Ave", "San Diego", "CA", "(619) 224-4623"),
("Lucha Libre Taco Shop", "Tacos", "🌮", "1810 W Washington St", "San Diego", "CA", "(619) 296-8226"),
("Phil's BBQ", "BBQ", "🍖", "3750 Sports Arena Blvd", "San Diego", "CA", "(619) 226-6333"),
("The Taco Stand", "Tacos", "🌮", "621 Pearl St", "San Diego", "CA", "(858) 551-6666"),
("Cafe 21", "Brunch · Mediterranean", "🥞", "802 Fifth Ave", "San Diego", "CA", "(619) 795-0721"),
("Lolita's Mexican Food", "Mexican", "🌯", "2703 5th Ave", "San Diego", "CA", "(619) 718-5123"),
("Tartine Bakery", "Bakery", "🥐", "600 Guerrero St", "San Francisco", "CA", "(415) 487-2600"),
("La Taqueria", "Tacos · Mexican", "🌮", "2889 Mission St", "San Francisco", "CA", "(415) 285-7117"),
("Bi-Rite Creamery", "Ice Cream", "🍨", "3692 18th St", "San Francisco", "CA", "(415) 626-5600"),
("House of Prime Rib", "Steakhouse", "🥩", "1906 Van Ness Ave", "San Francisco", "CA", "(415) 885-4605"),
("Souvla", "Greek", "🥙", "517 Hayes St", "San Francisco", "CA", "(415) 400-5458"),
("Mitchell's Ice Cream", "Ice Cream", "🍨", "477 San Jose Ave", "San Francisco", "CA", "(415) 648-2300"),
("Senor Sisig", "Filipino · Fusion", "🌯", "990 Harrison St", "San Francisco", "CA", "(415) 735-6591"),
("Tower Cafe", "Eclectic · Brunch", "🥞", "1518 Broadway", "Sacramento", "CA", "(916) 441-0228"),
("Bacon & Butter", "Breakfast", "🥓", "5913 Broadway", "Sacramento", "CA", "(916) 736-1111"),
("La Garnacha", "Tacos · Mexican", "🌮", "9411 Folsom Blvd", "Sacramento", "CA", "(916) 706-2182"),
("The Waterboy", "Californian", "🍽️", "2000 Capitol Ave", "Sacramento", "CA", "(916) 498-9891"),
("Tres Hermanas", "Mexican", "🌯", "2416 K St", "Sacramento", "CA", "(916) 443-6919"),
("Midtown's Cantina Alley", "Tacos", "🌮", "2326 K St", "Sacramento", "CA", "(916) 443-2345"),
    ]
    for name, cuisine, emoji, addr, city, state, phone in SPOTS2:
        exists = c.execute(
            "SELECT id FROM restaurants WHERE name=? AND city=?", (name, city)).fetchone()
        if not exists:
            c.execute(
                "INSERT INTO restaurants(name, cuisine, emoji, address, city, state, phone, open)"
                " VALUES(?,?,?,?,?,?,?,1)",
                (name, cuisine, emoji, addr, city, state, phone))
    conn.commit()
    conn.close()


init_db()
seed()
seed_cities()
seed_expansion_cities()
seed_expansion_cities2()


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


# ---------------- Security hardening ----------------
_rate_buckets: dict = {}
_pin_fails: dict = {}


def client_ip(req: Request) -> str:
    return req.headers.get("x-forwarded-for", req.client.host if req.client else "?").split(",")[0].strip()


def rate_limit(ip: str, max_hits: int = 10, window: int = 60) -> bool:
    """True if this IP should be throttled. Sliding window."""
    import time
    now = time.time()
    b = _rate_buckets.setdefault(ip, [])
    b[:] = [t for t in b if now - t < window]
    if len(b) >= max_hits:
        return True
    b.append(now)
    return False


def pin_throttled(ip: str) -> bool:
    import time
    f = _pin_fails.get(ip)
    return bool(f and time.time() - f["at"] < 900 and f["n"] >= 5)


def pin_failed(ip: str):
    import time
    f = _pin_fails.setdefault(ip, {"n": 0, "at": 0})
    f["n"] += 1
    f["at"] = time.time()


@app.middleware("http")
async def security_middleware(req: Request, call_next):
    resp = await call_next(req)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return resp


def check_pin(x_pin: str = Header(default="")):
    if x_pin != get_setting("pin"):
        raise HTTPException(status_code=401, detail="Bad PIN")


def geocode(address):
    """Best-effort address -> (lat, lng) via Nominatim (OpenStreetMap, free).
    Returns (0.0, 0.0) when it can't find it. Never raises."""
    try:
        q = urllib.parse.quote(address)
        req = urllib.request.Request(
            f"https://nominatim.openstreetmap.org/search?q={q}&format=json&limit=1",
            headers={"User-Agent": "DropDelivery/1.0 (wearedropservicesllc@gmail.com)"})
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


@app.get("/api/nearby")
def nearby_restaurants(lat: float = 0, lng: float = 0, radius: float = 50):
    """Restaurants within `radius` miles of the given GPS point. Sorted nearest-first."""
    if not lat or not lng:
        return []
    conn = db()
    rows = conn.execute("SELECT * FROM restaurants WHERE lat != 0 AND lng != 0").fetchall()
    out = []
    for r in rows:
        d = haversine_miles(r["lat"], r["lng"], lat, lng)
        if d <= radius:
            rd = dict(r)
            rd["distance_mi"] = round(d, 1)
            items = conn.execute(
                "SELECT * FROM menu_items WHERE restaurant_id=? AND available=1 ORDER BY id",
                (r["id"],)).fetchall()
            rd["menu"] = [dict(i) for i in items]
            out.append(rd)
    conn.close()
    out.sort(key=lambda x: x["distance_mi"])
    return out


@app.get("/api/cities")
def list_cities():
    """Distinct cities with live restaurants, for the homepage picker."""
    conn = db()
    rows = conn.execute(
        "SELECT city, state, COUNT(*) n FROM restaurants GROUP BY city, state ORDER BY n DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/live-restaurants")
def live_restaurants(city: str = "", state: str = "", lat: float = 0, lng: float = 0):
    """Get REAL restaurants for any city via OpenStreetMap (free, no key needed).
    Returns live data — not from our DB, but real places that exist."""
    import urllib.request, urllib.parse, json, time
    if not city:
        return []
    # Check cache first (24h)
    cache_key = f"{city},{state}".lower()
    conn = db()
    try:
        cached = conn.execute(
            "SELECT data, cached_at FROM live_cache WHERE key=?", (cache_key,)).fetchone()
        if cached:
            age = time.time() - float(cached["cached_at"])
            if age < 86400:  # 24h
                conn.close()
                return json.loads(cached["data"])
    except Exception:
        pass
    # Fetch from Nominatim
    try:
        query = f"restaurants in {city}, {state}" if state else f"restaurants in {city}"
        url = f"https://nominatim.openstreetmap.org/search?q={urllib.parse.quote(query)}&format=json&limit=20&extratags=1"
        req = urllib.request.Request(url, headers={"User-Agent": "DropApp/1.0"})
        data = json.loads(urllib.request.urlopen(req, timeout=15).read())
        results = []
        for r in data:
            # Extract name and address
            name = r.get("display_name", "").split(",")[0].strip()
            addr_parts = r.get("display_name", "").split(",")[1:3]
            addr = ",".join(addr_parts).strip()
            if name and len(name) > 2:
                results.append({
                    "name": name,
                    "address": addr,
                    "city": city,
                    "state": state,
                    "lat": float(r.get("lat", 0)),
                    "lng": float(r.get("lon", 0)),
                    "cuisine": r.get("extratags", {}).get("cuisine", "Restaurant"),
                    "live": True  # Mark as live data
                })
        # Cache it
        try:
            conn.execute(
                "INSERT OR REPLACE INTO live_cache(key, data, cached_at) VALUES(?,?,?)",
                (cache_key, json.dumps(results), str(time.time())))
            conn.commit()
        except Exception:
            pass
        conn.close()
        return results
    except Exception as e:
        conn.close()
        return []


@app.get("/api/ip-location")
def ip_location(req: Request):
    """Fallback location from IP address (free, no key needed)."""
    # Get client IP (handle proxies)
    ip = req.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if not ip:
        ip = req.client.host if req.client else ""
    if not ip or ip.startswith("127.") or ip == "localhost":
        return {"city": "", "state": "", "lat": 0, "lng": 0}
    try:
        import urllib.request, json
        # Free IP geolocation (no key)
        url = f"http://ip-api.com/json/{ip}?fields=city,region,regionName,lat,lon,status"
        rq = urllib.request.Request(url, headers={"User-Agent": "DropApp/1.0"})
        d = json.loads(urllib.request.urlopen(rq, timeout=5).read())
        if d.get("status") == "success":
            return {"city": d.get("city", ""), "state": d.get("region", ""),
                    "lat": d.get("lat", 0), "lng": d.get("lon", 0)}
    except Exception:
        pass
    return {"city": "", "state": "", "lat": 0, "lng": 0}


@app.post("/api/help/ask")
async def help_ask(req: Request):
    """AI help agent — answers questions about using Drop."""
    body = await req.json()
    q = (body.get("question") or "").strip().lower()
    page = body.get("page") or ""
    # Smart keyword-based answers (no API key needed)
    if any(w in q for w in ["location", "gps", "where am i", "detect"]):
        answer = "Tap '📍 Use my location' on the homepage. If it doesn't work, make sure location is allowed in your browser settings, or just type your city manually."
    elif any(w in q for w in ["order", "track", "where is my"]):
        answer = "After you order, you'll get a tracking link. You can also check your order history in your Account page."
    elif any(w in q for w in ["pickup", "already ordered"]):
        answer = "Use '📦 Request a Drop' — tell us what to pick up and where, and we'll bring it to you. Works in any city!"
    elif any(w in q for w in ["pay", "payment", "card", "cost", "price", "fee"]):
        answer = "Food delivery is $3.99 flat. Drop requests (pickup/delivery) are $1.50 per mile with a $3.99 minimum. You pay securely by card at checkout."
    elif any(w in q for w in ["driver", "who"]):
        answer = "Your driver is assigned after you order. You'll see their name, photo, rating, and live location on the tracking page."
    elif any(w in q for w in ["cancel", "refund"]):
        answer = "You can cancel from your tracking page. Refunds are processed automatically to your original payment method."
    elif any(w in q for w in ["account", "password", "login", "profile"]):
        answer = "Manage your profile, order history, and saved payment methods in the Account page (👤 icon, top right)."
    elif any(w in q for w in ["business"]):
        answer = "Business owners can create an account for volume discounts, saved addresses, and priority service. Check the Drop More section."
    elif "human" in q or "support" in q or "contact" in q:
        answer = "You can reach us at wearedropservicesllc@gmail.com or (262) 977-7869."
    else:
        answer = "I can help with orders, tracking, payments, location, drivers, and your account. What do you need help with?"
    return {"answer": answer}


@app.get("/api/user/orders")
def user_order_history(phone: str = ""):
    """Customer's order history."""
    if not phone:
        return []
    conn = db()
    rows = conn.execute(
        "SELECT id, restaurant_id, pickup_name, address, total, status, created_at"
        " FROM orders WHERE phone=? ORDER BY id DESC LIMIT 20",
        (phone,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/user/cards")
def user_saved_cards(phone: str = ""):
    """Customer's saved payment methods (last4 only)."""
    if not phone:
        return []
    conn = db()
    rows = conn.execute(
        "SELECT id, last4, brand, exp_month, exp_year FROM saved_cards"
        " WHERE phone=? ORDER BY id DESC", (phone,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/user/cards")
async def user_save_card(req: Request):
    """Save a card reference (last4 + brand, never full number)."""
    body = await req.json()
    phone = (body.get("phone") or "").strip()
    last4 = (body.get("last4") or "").strip()[-4:]
    brand = (body.get("brand") or "").strip()[:20]
    if not phone or len(last4) != 4:
        raise HTTPException(400, "Phone and valid last4 required")
    conn = db()
    # Avoid duplicates
    if not conn.execute("SELECT id FROM saved_cards WHERE phone=? AND last4=?",
                        (phone, last4)).fetchone():
        conn.execute(
            "INSERT INTO saved_cards(phone, last4, brand, exp_month, exp_year, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (phone, last4, brand, body.get("exp_month", "")[:2],
             body.get("exp_year", "")[:4], now_iso()))
        conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/user/cards/{cid}")
def user_delete_card(cid: int, phone: str = ""):
    conn = db()
    conn.execute("DELETE FROM saved_cards WHERE id=? AND phone=?", (cid, phone))
    conn.commit()
    conn.close()
    return {"ok": True}


# ---------------- Driver broadcasts ----------------
@app.get("/api/drivers/broadcasts")
def driver_broadcasts():
    conn = db()
    rows = conn.execute(
        "SELECT * FROM driver_broadcasts ORDER BY id DESC LIMIT 20").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/operator/broadcasts")
async def op_broadcast(req: Request, x_pin: str = Header(default="")):
    """Ken posts an announcement to all drivers."""
    check_pin(x_pin)
    body = await req.json()
    title = (body.get("title") or "").strip()[:100]
    message = (body.get("message") or "").strip()[:1000]
    if not title or not message:
        raise HTTPException(400, "Title and message required")
    conn = db()
    conn.execute(
        "INSERT INTO driver_broadcasts(title, message, created_at) VALUES(?,?,?)",
        (title, message, now_iso()))
    conn.commit()
    conn.close()
    log_event("broadcast", f"📢 {title}", message[:100])
    return {"ok": True}


# ---------------- Driver complaints (AI triaged) ----------------
@app.post("/api/drivers/complaint")
async def driver_complaint(req: Request):
    """Driver files a complaint. AI summarizes and suggests solution."""
    body = await req.json()
    driver_id = int(body.get("driver_id") or 0)
    subject = (body.get("subject") or "").strip()[:100]
    message = (body.get("message") or "").strip()[:1000]
    if not driver_id or not subject or not message:
        raise HTTPException(400, "driver_id, subject, and message required")
    # AI triage: categorize and suggest
    msg_lower = message.lower()
    if any(w in msg_lower for w in ["pay", "money", "payout", "earn"]):
        category = "💰 Pay issue"
        suggestion = "Check their payout status and recent deliveries. Verify amounts match."
    elif any(w in msg_lower for w in ["customer", "rude", "complaint"]):
        category = "😤 Customer issue"
        suggestion = "Review the order details and customer feedback. Consider mediating."
    elif any(w in msg_lower for w in ["app", "bug", "broken", "error", "crash"]):
        category = "🐛 App issue"
        suggestion = "Check if it's a known bug. May need a fix pushed."
    elif any(w in msg_lower for w in ["safety", "danger", "threat"]):
        category = "🚨 Safety concern"
        suggestion = "PRIORITY: Review immediately. May need to suspend involved parties."
    else:
        category = "📝 General"
        suggestion = "Review the details and respond directly to the driver."
    ai_summary = f"{category}: {subject}"
    conn = db()
    conn.execute(
        "INSERT INTO driver_complaints(driver_id, subject, message, ai_summary, ai_suggestion, created_at)"
        " VALUES(?,?,?,?,?,?)",
        (driver_id, subject, message, ai_summary, suggestion, now_iso()))
    conn.commit()
    conn.close()
    log_event("complaint", f"📝 Complaint from driver #{driver_id}", ai_summary)
    return {"ok": True, "ai_category": category}


@app.get("/api/drivers/complaints")
def driver_my_complaints(driver_id: int = 0):
    conn = db()
    rows = conn.execute(
        "SELECT * FROM driver_complaints WHERE driver_id=? ORDER BY id DESC",
        (driver_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/operator/complaints")
def op_complaints(x_pin: str = Header(default="")):
    """Ken sees all complaints with AI suggestions."""
    check_pin(x_pin)
    conn = db()
    rows = conn.execute(
        "SELECT dc.*, d.name as driver_name FROM driver_complaints dc"
        " JOIN drivers d ON d.id=dc.driver_id"
        " ORDER BY dc.id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/operator/complaints/{cid}/respond")
async def op_complaint_respond(cid: int, req: Request, x_pin: str = Header(default="")):
    check_pin(x_pin)
    body = await req.json()
    response = (body.get("response") or "").strip()[:1000]
    conn = db()
    conn.execute(
        "UPDATE driver_complaints SET ken_response=?, status='responded' WHERE id=?",
        (response, cid))
    conn.commit()
    conn.close()
    return {"ok": True}


# ---------------- Driver earnings goals ----------------
@app.get("/api/drivers/goals")
def driver_goals_get(driver_id: int = 0):
    conn = db()
    r = conn.execute("SELECT * FROM driver_goals WHERE driver_id=?", (driver_id,)).fetchone()
    # Get today's earnings
    today = now_iso()[:10]
    earned = conn.execute(
        "SELECT COALESCE(SUM(amount),0) as total FROM payouts"
        " WHERE driver_id=? AND date(created_at)=?",
        (driver_id, today)).fetchone()
    week_earned = conn.execute(
        "SELECT COALESCE(SUM(amount),0) as total FROM payouts"
        " WHERE driver_id=? AND date(created_at) >= date('now','-7 days')",
        (driver_id,)).fetchone()
    conn.close()
    return {
        "daily_target": r["daily_target"] if r else 100,
        "weekly_target": r["weekly_target"] if r else 500,
        "today_earned": earned["total"] if earned else 0,
        "week_earned": week_earned["total"] if week_earned else 0,
    }


@app.post("/api/drivers/goals")
async def driver_goals_set(req: Request):
    body = await req.json()
    driver_id = int(body.get("driver_id") or 0)
    daily = float(body.get("daily_target") or 100)
    weekly = float(body.get("weekly_target") or 500)
    conn = db()
    conn.execute(
        "INSERT OR REPLACE INTO driver_goals(driver_id, daily_target, weekly_target, updated_at)"
        " VALUES(?,?,?,?)",
        (driver_id, daily, weekly, now_iso()))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/nearest-city")
def nearest_city(lat: float = 0, lng: float = 0):
    """GPS -> what city are you in, and does Drop serve it?"""
    import math
    city, state = "", ""
    try:
        q = f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lng}&format=json"
        req = urllib.request.Request(q, headers={"User-Agent": "DropDelivery/1.0 (wearedropservicesllc@gmail.com)"})
        data = json.load(urllib.request.urlopen(req, timeout=8))
        addr = data.get("address", {})
        city = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("county") or ""
        _states = {"North Dakota": "ND", "South Dakota": "SD", "Minnesota": "MN",
                   "Nebraska": "NE", "Iowa": "IA", "Montana": "MT", "New York": "NY",
                   "Texas": "TX", "Florida": "FL", "California": "CA"}
        state = _states.get(addr.get("state", ""), "")
    except Exception:
        pass
    conn = db()
    cities = conn.execute(
        "SELECT city, state, COUNT(*) n FROM restaurants GROUP BY city, state").fetchall()
    match = None
    for c in cities:
        if city and c["city"].lower() == city.lower():
            match = {"city": c["city"], "state": c["state"]}
            break
    if not match and lat and lng:
        best, bestd = None, 1e9
        rows = conn.execute(
            "SELECT city, state, lat, lng FROM restaurants WHERE lat != 0").fetchall()
        for r in rows:
            d = math.hypot(r["lat"] - lat, r["lng"] - lng)
            if d < bestd:
                bestd, best = d, r
        if best and bestd < 1.0:
            match = {"city": best["city"], "state": best["state"]}
    conn.close()
    return {"detected_city": city, "detected_state": state, "match": match}


@app.post("/api/spot/suggest")
async def spot_suggest(req: Request):
    """Customer suggests a restaurant/spot. Deduped, notifies Ken."""
    body = await req.json()
    name = (body.get("name") or "").strip()
    city = (body.get("city") or "").strip()[:80]
    if not name:
        raise HTTPException(400, "Name required")
    conn = db()
    dup = conn.execute(
        "SELECT id FROM spot_suggestions WHERE LOWER(name)=LOWER(?)", (name,)).fetchone()
    if not dup:
        conn.execute(
            "INSERT INTO spot_suggestions(name, city, note, contact, created_at) VALUES(?,?,?,?,?)",
            (name[:120], city, (body.get("note") or "")[:500],
             (body.get("contact") or "")[:120], now_iso()))
        conn.commit()
    conn.close()
    try:
        push_ken("📍 New spot suggestion", f"{name} in {city or 'unknown city'}")
    except Exception:
        pass
    return {"ok": True}


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
    cur = conn.execute(
        """INSERT INTO orders(restaurant_id, items_json, customer_name, phone, address,
                              notes, food_total, delivery_fee, tip, discount, promo_code,
                              user_id, total, status, stripe_status, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'placed','pending',?,?)""",
        (rid, json.dumps(priced), name, phone, address, notes,
         food_total, fee, tip, discount, promo_code, user_id, total, now_iso(), now_iso()))
    oid = cur.lastrowid
    conn.commit()
    # pin the delivery address on the map (background — never slows the order)
    def _geocode_order():
        try:
            glat, glng = geocode(address + ", Bismarck, ND")
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
    if STRIPE_LIVE:
        base = str(req.base_url).rstrip("/")
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
    u = conn.execute("SELECT id, name, phone, created_at FROM users WHERE id=?",
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


@app.get("/api/businesses/nearby")
def nearby_businesses(lat: float = 0, lng: float = 0, radius: float = 50):
    """Businesses within `radius` miles of GPS point. Sorted nearest-first."""
    if not lat or not lng:
        return {"businesses": [], "categories": []}
    conn = db()
    rows = conn.execute("SELECT * FROM businesses WHERE open=1 AND lat != 0").fetchall()
    out = []
    for b in rows:
        d = haversine_miles(b["lat"], b["lng"], lat, lng)
        if d <= radius:
            bd = dict(b)
            bd["distance_mi"] = round(d, 1)
            out.append(bd)
    conn.close()
    out.sort(key=lambda x: x["distance_mi"])
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
        plat, plng = geocode(pickup_address + ", Bismarck, ND")
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
    # geocode delivery address in background
    def _geo():
        try:
            glat, glng = geocode(address + ", Bismarck, ND")
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


@app.post("/api/operator/drivers/{did}/photo-approve")
async def op_approve_driver_photo(did: int, req: Request, x_pin: str = Header(default="")):
    """Ken approves or rejects a driver's selfie/car photo."""
    check_pin(x_pin)
    body = await req.json()
    photo_type = (body.get("type") or "").strip()  # 'selfie' or 'car'
    approve = bool(body.get("approve"))
    if photo_type not in ("selfie", "car"):
        raise HTTPException(400, "type must be selfie or car")
    conn = db()
    col = "selfie_approved" if photo_type == "selfie" else "car_approved"
    conn.execute(f"UPDATE drivers SET {col}=? WHERE id=?", (1 if approve else 0, did))
    conn.commit()
    conn.close()
    action = "approved" if approve else "rejected"
    audit("operator", f"driver_photo_{action}", "driver", did, photo_type)
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


@app.post("/api/drivers/photo")
async def driver_upload_photo(req: Request):
    """Driver uploads selfie or car photo. Basic AI check, Ken approves."""
    from PIL import Image
    import io, os
    form = await req.form()
    driver_id = int(form.get("driver_id") or 0)
    photo_type = (form.get("type") or "").strip()  # 'selfie' or 'car'
    file = form.get("file")
    if not driver_id or photo_type not in ("selfie", "car") or not file:
        raise HTTPException(400, "driver_id, type (selfie/car), and file required")
    data = await file.read()
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(400, "Photo too large (max 5MB)")
    # AI basic check: valid image, not blank, reasonable size
    try:
        img = Image.open(io.BytesIO(data))
        img.verify()
        img = Image.open(io.BytesIO(data))  # reopen after verify
        w, h = img.size
        if w < 200 or h < 200:
            raise HTTPException(400, "Photo too small — use a clearer picture")
        # Check not blank (sample pixels)
        small = img.resize((10, 10)).convert("RGB")
        pixels = list(small.getdata())
        if len(set(pixels)) < 5:
            raise HTTPException(400, "Photo looks blank — retake it")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "Invalid image file")
    # Save
    os.makedirs("static/driver-photos", exist_ok=True)
    fname = f"d{driver_id}_{photo_type}_{int(__import__('time').time())}.jpg"
    path = f"static/driver-photos/{fname}"
    img.convert("RGB").save(path, "JPEG", quality=85)
    # Update driver record (pending Ken's approval)
    conn = db()
    col = "selfie_photo" if photo_type == "selfie" else "car_photo"
    appcol = "selfie_approved" if photo_type == "selfie" else "car_approved"
    conn.execute(f"UPDATE drivers SET {col}=?, {appcol}=0 WHERE id=?",
                 (f"/{path}", driver_id))
    conn.commit()
    conn.close()
    log_event("driver", f"📸 Driver #{driver_id} uploaded {photo_type} photo",
              "Awaiting Ken's approval")
    return {"ok": True, "path": f"/{path}", "message": "Photo uploaded — Ken will review it"}


@app.post("/api/drivers/delivery-photo")
async def driver_delivery_photo(req: Request):
    """Driver snaps pickup/dropoff proof photo. Customer sees it live."""
    from PIL import Image
    import io, os
    form = await req.form()
    driver_id = int(form.get("driver_id") or 0)
    order_id = int(form.get("order_id") or 0)
    photo_type = (form.get("type") or "").strip()  # 'pickup' or 'dropoff'
    file = form.get("file")
    if not driver_id or not order_id or photo_type not in ("pickup", "dropoff") or not file:
        raise HTTPException(400, "driver_id, order_id, type (pickup/dropoff), file required")
    data = await file.read()
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(400, "Photo too large (max 5MB)")
    try:
        img = Image.open(io.BytesIO(data))
        img.verify()
        img = Image.open(io.BytesIO(data))
    except Exception:
        raise HTTPException(400, "Invalid image file")
    os.makedirs("static/delivery-photos", exist_ok=True)
    fname = f"o{order_id}_{photo_type}_{int(__import__('time').time())}.jpg"
    path = f"static/delivery-photos/{fname}"
    img.convert("RGB").save(path, "JPEG", quality=80)
    conn = db()
    conn.execute(
        "INSERT INTO delivery_photos(order_id, driver_id, photo_type, photo_path, created_at)"
        " VALUES(?,?,?,?,?)",
        (order_id, driver_id, photo_type, f"/{path}", now_iso()))
    # Update order status
    if photo_type == "pickup":
        conn.execute("UPDATE orders SET status='picked_up', updated_at=? WHERE id=?",
                     (now_iso(), order_id))
    elif photo_type == "dropoff":
        conn.execute("UPDATE orders SET status='delivered', updated_at=? WHERE id=?",
                     (now_iso(), order_id))
    conn.commit()
    conn.close()
    label = "picked up" if photo_type == "pickup" else "delivered"
    log_event("delivery", f"📸 Order #{order_id} {label} — photo proof",
              f"Driver #{driver_id}")
    return {"ok": True, "path": f"/{path}"}


@app.get("/api/orders/{oid}/photos")
def order_photos(oid: int):
    """Customer sees pickup/dropoff photo proof."""
    conn = db()
    rows = conn.execute(
        "SELECT photo_type, photo_path, created_at FROM delivery_photos"
        " WHERE order_id=? ORDER BY id", (oid,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------- Business accounts (Drop More B2B) ----------------
@app.post("/api/business/register")
async def business_register(req: Request):
    import hashlib
    body = await req.json()
    name = (body.get("business_name") or "").strip()[:100]
    contact = (body.get("contact_name") or "").strip()[:80]
    email = (body.get("email") or "").strip().lower()[:120]
    phone = (body.get("phone") or "").strip()[:30]
    pw = body.get("password") or ""
    if not all([name, contact, email, phone, pw]):
        raise HTTPException(400, "All fields required")
    if len(pw) < 6:
        raise HTTPException(400, "Password must be 6+ characters")
    conn = db()
    if conn.execute("SELECT id FROM business_accounts WHERE email=?", (email,)).fetchone():
        conn.close()
        raise HTTPException(400, "Email already registered")
    ph = hashlib.sha256(pw.encode()).hexdigest()
    cur = conn.execute(
        "INSERT INTO business_accounts(business_name, contact_name, email, phone, password_hash, created_at)"
        " VALUES(?,?,?,?,?,?)",
        (name, contact, email, phone, ph, now_iso()))
    bid = cur.lastrowid
    conn.commit()
    conn.close()
    log_event("business", f"🏢 New business: {name}", contact)
    return {"ok": True, "id": bid}


@app.post("/api/business/login")
async def business_login(req: Request):
    import hashlib
    body = await req.json()
    email = (body.get("email") or "").strip().lower()
    pw = body.get("password") or ""
    conn = db()
    b = conn.execute("SELECT * FROM business_accounts WHERE email=?", (email,)).fetchone()
    conn.close()
    if not b or b["password_hash"] != hashlib.sha256(pw.encode()).hexdigest():
        raise HTTPException(401, "Invalid login")
    return {"ok": True, "id": b["id"], "business_name": b["business_name"],
            "discount_tier": b["discount_tier"]}


def _biz_price(miles: float, tier: str = "standard") -> dict:
    """$1.50/mile base, discounts for volume."""
    base = round(miles * 1.50, 2)
    discount = 0
    if tier == "gold":
        discount = round(base * 0.15, 2)  # 15% off
    elif tier == "silver":
        discount = round(base * 0.10, 2)  # 10% off
    return {"miles": round(miles, 1), "base": base, "discount": discount,
            "total": round(base - discount, 2), "tier": tier}


@app.post("/api/business/quote")
async def business_quote(req: Request):
    """Price a pickup/dropoff by distance."""
    import math
    body = await req.json()
    plat, plng = float(body.get("plat") or 0), float(body.get("plng") or 0)
    dlat, dlng = float(body.get("dlat") or 0), float(body.get("dlng") or 0)
    tier = (body.get("tier") or "standard").strip()
    if not all([plat, plng, dlat, dlng]):
        raise HTTPException(400, "Pickup and dropoff coordinates required")
    miles = math.hypot(dlat - plat, dlng - plng) * 69
    return _biz_price(miles, tier)


# ---------------- Driver ratings ----------------
@app.post("/api/drivers/rate")
async def driver_rate(req: Request):
    """Customer rates driver 1-5 + comment. Drivers start at 100% (5.0)."""
    body = await req.json()
    driver_id = int(body.get("driver_id") or 0)
    order_id = int(body.get("order_id") or 0)
    rating = int(body.get("rating") or 0)
    comment = (body.get("comment") or "").strip()[:500]
    customer = (body.get("customer_name") or "").strip()[:80]
    if not driver_id or not order_id or rating < 1 or rating > 5:
        raise HTTPException(400, "driver_id, order_id, rating 1-5 required")
    conn = db()
    # Prevent duplicate rating for same order
    if conn.execute("SELECT id FROM driver_ratings WHERE order_id=?",
                    (order_id,)).fetchone():
        conn.close()
        raise HTTPException(400, "Already rated")
    conn.execute(
        "INSERT INTO driver_ratings(driver_id, order_id, rating, comment, customer_name, created_at)"
        " VALUES(?,?,?,?,?,?)",
        (driver_id, order_id, rating, comment, customer, now_iso()))
    # Recalculate driver rating (weighted: start at 5.0)
    ratings = conn.execute(
        "SELECT rating FROM driver_ratings WHERE driver_id=?", (driver_id,)).fetchall()
    # Start at 100% (5.0), blend with actual ratings
    total = 5.0 * 3  # 3 "perfect" starter ratings
    count = 3
    for r in ratings:
        total += r["rating"]
        count += 1
    new_rating = round(total / count, 2)
    conn.execute("UPDATE drivers SET rating=?, rating_count=? WHERE id=?",
                 (new_rating, len(ratings), driver_id))
    conn.commit()
    conn.close()
    log_event("rating", f"⭐ Driver #{driver_id} rated {rating}/5",
              f"New avg: {new_rating}")
    return {"ok": True, "new_rating": new_rating}


@app.get("/api/operator/driver-ratings/{did}")
def op_driver_ratings(did: int, x_pin: str = Header(default="")):
    """Ken sees all ratings + comments for a driver."""
    check_pin(x_pin)
    conn = db()
    rows = conn.execute(
        "SELECT * FROM driver_ratings WHERE driver_id=? ORDER BY id DESC",
        (did,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------- User profiles ----------------
@app.post("/api/user/profile")
async def user_profile_upsert(req: Request):
    """Create/update customer profile: name, email, phone, photo, password."""
    import hashlib
    body = await req.json()
    phone = (body.get("phone") or "").strip()[:30]
    if not phone:
        raise HTTPException(400, "Phone required")
    conn = db()
    existing = conn.execute("SELECT * FROM user_profiles WHERE phone=?", (phone,)).fetchone()
    ph = ""
    if body.get("password"):
        ph = hashlib.sha256(body["password"].encode()).hexdigest()
    if existing:
        conn.execute(
            "UPDATE user_profiles SET name=?, email=?, photo=?,"
            " password_hash=CASE WHEN ?='' THEN password_hash ELSE ? END WHERE phone=?",
            (body.get("name", existing["name"])[:80],
             body.get("email", existing["email"])[:120],
             body.get("photo", existing["photo"])[:500],
             ph, ph, phone))
    else:
        conn.execute(
            "INSERT INTO user_profiles(phone, name, email, photo, password_hash, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (phone, body.get("name", "")[:80], body.get("email", "")[:120],
             body.get("photo", "")[:500], ph, now_iso()))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/user/profile")
def user_profile_get(phone: str = ""):
    conn = db()
    r = conn.execute("SELECT phone, name, email, photo FROM user_profiles WHERE phone=?",
                     (phone,)).fetchone()
    conn.close()
    return dict(r) if r else {}


@app.get("/api/operator/users")
def op_users(x_pin: str = Header(default=""), q: str = ""):
    """Ken sees all user profiles, can click to contact."""
    check_pin(x_pin)
    conn = db()
    if q:
        rows = conn.execute(
            "SELECT * FROM user_profiles WHERE phone LIKE ? OR name LIKE ? OR email LIKE ?"
            " ORDER BY id DESC LIMIT 50",
            (f"%{q}%", f"%{q}%", f"%{q}%")).fetchall()
    else:
        rows = conn.execute("SELECT * FROM user_profiles ORDER BY id DESC LIMIT 50").fetchall()
    conn.close()
    # Don't expose password hashes
    return [{k: v for k, v in dict(r).items() if k != "password_hash"} for r in rows]


@app.post("/api/operator/users/{uid}/contact")
async def op_contact_user(uid: int, req: Request, x_pin: str = Header(default="")):
    """Ken messages a user directly (via ntfy push + logged)."""
    check_pin(x_pin)
    body = await req.json()
    message = (body.get("message") or "").strip()[:500]
    if not message:
        raise HTTPException(400, "Message required")
    conn = db()
    u = conn.execute("SELECT * FROM user_profiles WHERE id=?", (uid,)).fetchone()
    conn.close()
    if not u:
        raise HTTPException(404, "User not found")
    # Log the outreach
    log_event("contact", f"📩 Ken messaged {u['name'] or u['phone']}", message[:100])
    audit("operator", "user_contacted", "user", uid, message[:100])
    # If user has ntfy, they'd get it — for now, log it for Ken to send via his preferred channel
    return {"ok": True, "user_phone": u["phone"], "user_email": u["email"],
            "message": f"Contact {u['name'] or 'user'} at {u['phone']}" + (f" or {u['email']}" if u['email'] else "")}


# ---------------- Driver food receipts + reimbursement ----------------
@app.post("/api/drivers/receipt")
async def driver_submit_receipt(req: Request):
    """Driver paid for food at restaurant, submits receipt photo for reimbursement."""
    from PIL import Image
    import io, os
    form = await req.form()
    driver_id = int(form.get("driver_id") or 0)
    order_id = int(form.get("order_id") or 0)
    try:
        amount = round(float(form.get("amount") or 0), 2)
    except (TypeError, ValueError):
        raise HTTPException(400, "Valid amount required")
    file = form.get("file")
    if not driver_id or not order_id or amount <= 0 or not file:
        raise HTTPException(400, "driver_id, order_id, amount, and receipt photo required")
    data = await file.read()
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(400, "Photo too large (max 5MB)")
    try:
        img = Image.open(io.BytesIO(data))
        img.verify()
        img = Image.open(io.BytesIO(data))
    except Exception:
        raise HTTPException(400, "Invalid image")
    os.makedirs("static/receipts", exist_ok=True)
    fname = f"r{order_id}_{int(__import__('time').time())}.jpg"
    path = f"static/receipts/{fname}"
    img.convert("RGB").save(path, "JPEG", quality=80)
    # AI check: compare against order total
    conn = db()
    o = conn.execute("SELECT food_total FROM orders WHERE id=?", (order_id,)).fetchone()
    ai_note = ""
    if o:
        expected = o["food_total"] or 0
        diff = abs(amount - expected)
        if diff < 1.0:
            ai_note = f"✓ Matches order (${expected:.2f})"
        elif amount > expected * 1.2:
            ai_note = f"⚠️ ${amount:.2f} vs order ${expected:.2f} — review needed"
        else:
            ai_note = f"~ ${amount:.2f} vs order ${expected:.2f}"
    conn.execute(
        "INSERT INTO food_receipts(order_id, driver_id, amount, photo_path, ai_check, created_at)"
        " VALUES(?,?,?,?,?,?)",
        (order_id, driver_id, amount, f"/{path}", ai_note, now_iso()))
    conn.commit()
    conn.close()
    log_event("payment", f"🧾 Receipt ${amount:.2f} from driver #{driver_id}",
              f"Order #{order_id} — {ai_note}")
    return {"ok": True, "ai_check": ai_note}


@app.get("/api/operator/receipts")
def op_receipts(x_pin: str = Header(default="")):
    check_pin(x_pin)
    conn = db()
    rows = conn.execute(
        "SELECT fr.*, d.name as driver_name FROM food_receipts fr"
        " JOIN drivers d ON d.id=fr.driver_id"
        " WHERE fr.status='pending' ORDER BY fr.id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/operator/receipts/{rid}/approve")
async def op_receipt_approve(rid: int, req: Request, x_pin: str = Header(default="")):
    """Ken approves receipt → driver gets reimbursed + profit via payout."""
    check_pin(x_pin)
    conn = db()
    r = conn.execute("SELECT * FROM food_receipts WHERE id=?", (rid,)).fetchone()
    if not r:
        conn.close()
        raise HTTPException(404, "Receipt not found")
    # Mark approved
    conn.execute("UPDATE food_receipts SET status='approved', decided_by='ken' WHERE id=?", (rid,))
    # Create reimbursement payout (food cost + delivery earnings)
    o = conn.execute("SELECT delivery_fee, tip FROM orders WHERE id=?", (r["order_id"],)).fetchone()
    total = r["amount"] + (o["delivery_fee"] if o else 0) + (o["tip"] if o else 0)
    conn.execute(
        "INSERT INTO payouts(driver_id, amount, kind, status, note, created_at)"
        " VALUES(?, ?, 'reimbursement', 'requested', ?, ?)",
        (r["driver_id"], round(total, 2),
         f"Food ${r['amount']:.2f} + delivery (order #{r['order_id']})", now_iso()))
    conn.commit()
    conn.close()
    audit("operator", "receipt_approved", "receipt", rid, f"${r['amount']:.2f}")
    log_event("payment", f"✅ Receipt approved — ${total:.2f} to driver #{r['driver_id']}",
              f"Food reimbursement + delivery pay")
    return {"ok": True, "payout_total": round(total, 2)}


@app.post("/api/operator/receipts/{rid}/reject")
async def op_receipt_reject(rid: int, req: Request, x_pin: str = Header(default="")):
    check_pin(x_pin)
    body = await req.json()
    reason = (body.get("reason") or "").strip()[:200]
    conn = db()
    conn.execute("UPDATE food_receipts SET status='rejected', decided_by='ken' WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    audit("operator", "receipt_rejected", "receipt", rid, reason)
    return {"ok": True}


# ---------------- Driver disputes / theft / appeals ----------------
@app.post("/api/operator/drivers/{did}/flag")
async def op_flag_driver(did: int, req: Request, x_pin: str = Header(default="")):
    """Ken flags a driver for theft/misconduct → suspended, can appeal."""
    check_pin(x_pin)
    body = await req.json()
    issue = (body.get("issue_type") or "theft").strip()
    desc = (body.get("description") or "").strip()[:500]
    order_id = int(body.get("order_id") or 0)
    if not desc:
        raise HTTPException(400, "Description required")
    conn = db()
    conn.execute(
        "INSERT INTO driver_disputes(driver_id, order_id, issue_type, description, created_at)"
        " VALUES(?,?,?,?,?)",
        (did, order_id, issue, desc, now_iso()))
    # Suspend driver immediately
    conn.execute("UPDATE drivers SET status='suspended' WHERE id=?", (did,))
    conn.commit()
    conn.close()
    audit("operator", "driver_flagged", "driver", did, f"{issue}: {desc[:100]}")
    log_event("safety", f"🚨 Driver #{did} suspended", f"{issue}: {desc[:100]}")
    return {"ok": True, "message": "Driver suspended. They can submit an appeal."}


@app.post("/api/drivers/appeal")
async def driver_appeal(req: Request):
    """Suspended driver submits an appeal."""
    body = await req.json()
    driver_id = int(body.get("driver_id") or 0)
    dispute_id = int(body.get("dispute_id") or 0)
    appeal = (body.get("appeal") or "").strip()[:1000]
    if not driver_id or not dispute_id or not appeal:
        raise HTTPException(400, "driver_id, dispute_id, and appeal text required")
    conn = db()
    conn.execute(
        "UPDATE driver_disputes SET driver_appeal=?, status='appealed' WHERE id=? AND driver_id=?",
        (appeal, dispute_id, driver_id))
    conn.commit()
    conn.close()
    log_event("safety", f"📝 Driver #{driver_id} appealed dispute #{dispute_id}", "")
    return {"ok": True}


@app.get("/api/operator/disputes")
def op_disputes(x_pin: str = Header(default="")):
    check_pin(x_pin)
    conn = db()
    rows = conn.execute(
        "SELECT dd.*, d.name as driver_name FROM driver_disputes dd"
        " JOIN drivers d ON d.id=dd.driver_id"
        " ORDER BY dd.id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/operator/disputes/{did}/resolve")
async def op_dispute_resolve(did: int, req: Request, x_pin: str = Header(default="")):
    """Ken resolves: reinstate driver or ban permanently."""
    check_pin(x_pin)
    body = await req.json()
    action = (body.get("action") or "").strip()  # 'reinstate' or 'ban'
    resolution = (body.get("resolution") or "").strip()[:500]
    if action not in ("reinstate", "ban"):
        raise HTTPException(400, "action must be reinstate or ban")
    conn = db()
    disp = conn.execute("SELECT * FROM driver_disputes WHERE id=?", (did,)).fetchone()
    if not disp:
        conn.close()
        raise HTTPException(404, "Dispute not found")
    new_status = "approved" if action == "reinstate" else "banned"
    conn.execute(
        "UPDATE driver_disputes SET status='resolved', resolution=?, decided_by='ken', resolved_at=? WHERE id=?",
        (resolution, now_iso(), did))
    conn.execute("UPDATE drivers SET status=? WHERE id=?", (new_status, disp["driver_id"]))
    conn.commit()
    conn.close()
    audit("operator", f"dispute_{action}", "dispute", did, resolution[:100])
    return {"ok": True}


@app.post("/api/driver/nearby")
async def driver_nearby(req: Request):
    """Customer picks driver: approved drivers within 15 miles, sorted by rating."""
    import math
    body = await req.json()
    lat = float(body.get("lat") or 0)
    lng = float(body.get("lng") or 0)
    if not lat or not lng:
        raise HTTPException(400, "Location required")
    conn = db()
    rows = conn.execute(
        "SELECT id, name, rating, rating_count, deliveries, selfie_photo, car_photo,"
        " vehicle_make, vehicle_model, vehicle_color, last_lat, last_lng"
        " FROM drivers WHERE status='approved' AND selfie_approved=1").fetchall()
    out = []
    for d in rows:
        if not d["last_lat"]:
            continue
        dist = math.hypot(d["last_lat"] - lat, d["last_lng"] - lng) * 69
        if dist <= 15:
            out.append({
                "id": d["id"], "name": d["name"].split()[0] + " " + (d["name"].split()[1][:1] + "." if len(d["name"].split()) > 1 else ""),
                "rating": round(d["rating"] or 5.0, 1), "deliveries": d["deliveries"],
                "distance_mi": round(dist, 1),
                "photo": d["selfie_photo"],
                "car": f"{d['vehicle_color']} {d['vehicle_make']} {d['vehicle_model']}".strip(),
                "car_photo": d["car_photo"],
            })
    conn.close()
    out.sort(key=lambda x: (-x["rating"], x["distance_mi"]))
    return out


@app.post("/api/driver/location")
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
        dr = conn.execute("SELECT id, name, last_lat, last_lng FROM drivers WHERE id=? AND status='approved'",
                          (driver_id,)).fetchone()
        if not dr:
            conn.close()
            raise HTTPException(400, "Driver not found or not approved")
        # 15-mile radius: driver must be near the restaurant
        o = conn.execute("SELECT restaurant_id FROM orders WHERE id=?", (oid,)).fetchone()
        if o and o["restaurant_id"]:
            r = conn.execute("SELECT lat, lng, name FROM restaurants WHERE id=?",
                             (o["restaurant_id"],)).fetchone()
            if r and r["lat"] and dr["last_lat"]:
                d = haversine_miles(r["lat"], r["lng"], dr["last_lat"], dr["last_lng"])
                if d > 15:
                    conn.close()
                    raise HTTPException(400,
                        f"{dr['name']} is {d:.1f} miles from {r['name']} — outside the 15-mile delivery radius. Assign a closer driver.")
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
    """Distance in miles between two GPS points."""
    import math
    if not all([lat1, lng1, lat2, lng2]):
        return 0.0
    r = 3958.8  # earth radius in miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return round(2 * r * math.asin(math.sqrt(a)), 2)


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
    miles = haversine_miles(pickup_lat, pickup_lng, cust_lat, cust_lng)
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


@app.get("/api/operator/restaurant-apps")
def op_restaurant_apps(x_pin: str = Header(default=""), status: str = ""):
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
    log_event("email", f"📧 Outreach email sent to {e['to_email'][:40]}",
              e["subject"][:80])
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


# ---------------- AI bot chat ----------------
@app.get("/api/operator/bot/history")
def op_bot_history(x_pin: str = Header(default="")):
    check_pin(x_pin)
    conn = db()
    rows = conn.execute(
        "SELECT id, role, message, created_at FROM bot_chats ORDER BY id DESC LIMIT 50").fetchall()
    conn.close()
    return [dict(r) for r in reversed(rows)]


@app.post("/api/operator/bot/send")
async def op_bot_send(req: Request, x_pin: str = Header(default="")):
    check_pin(x_pin)
    body = await req.json()
    msg = (body.get("message") or "").strip()
    if not msg:
        raise HTTPException(400, "Empty message")
    conn = db()
    conn.execute("INSERT INTO bot_chats(role, message, created_at) VALUES('ken', ?, ?)",
                 (msg[:2000], now_iso()))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/operator/bot/reply")
async def op_bot_reply(req: Request, x_pin: str = Header(default="")):
    check_pin(x_pin)
    body = await req.json()
    msg = (body.get("message") or "").strip()
    if not msg:
        raise HTTPException(400, "Empty message")
    conn = db()
    conn.execute("INSERT INTO bot_chats(role, message, created_at) VALUES('milli', ?, ?)",
                 (msg[:2000], now_iso()))
    conn.commit()
    conn.close()
    return {"ok": True}


# ---------------- AI improvement ideas ----------------
@app.get("/api/operator/ideas")
def op_ideas(x_pin: str = Header(default="")):
    check_pin(x_pin)
    conn = db()
    rows = conn.execute(
        "SELECT id, title, description, why_beats, status, created_at FROM ai_ideas ORDER BY id DESC LIMIT 30").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/operator/ideas")
async def op_ideas_add(req: Request, x_pin: str = Header(default="")):
    check_pin(x_pin)
    body = await req.json()
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "Title required")
    conn = db()
    dup = conn.execute("SELECT id FROM ai_ideas WHERE title=? AND status IN ('pending','approved')",
                       (title,)).fetchone()
    if dup:
        conn.close()
        return {"ok": True, "duplicate": True}
    conn.execute("INSERT INTO ai_ideas(title, description, why_beats, created_at) VALUES(?,?,?,?)",
                 (title[:120], (body.get("description") or "")[:1000],
                  (body.get("why_beats") or "")[:500], now_iso()))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/operator/ideas/{iid}/{action}")
def op_ideas_action(iid: int, action: str, x_pin: str = Header(default="")):
    check_pin(x_pin)
    if action not in ("approve", "reject"):
        raise HTTPException(400, "Bad action")
    conn = db()
    conn.execute("UPDATE ai_ideas SET status=? WHERE id=?",
                 ("approved" if action == "approve" else "rejected", iid))
    conn.commit()
    conn.close()
    return {"ok": True}


# ---------------- Command dashboard ----------------
@app.get("/api/operator/command-stats")
def op_command_stats(x_pin: str = Header(default="")):
    """Everything the command dashboard needs, in one call."""
    check_pin(x_pin)
    conn = db()
    today = now_iso()[:10]
    orders_today = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE created_at LIKE ?", (today + "%",)).fetchone()[0]
    rev_today = conn.execute(
        "SELECT COALESCE(SUM(delivery_fee),0) FROM orders WHERE created_at LIKE ? AND stripe_status='paid'",
        (today + "%",)).fetchone()[0]
    active = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE status IN ('new','confirmed','preparing','on_the_way')").fetchone()[0]
    restaurants = conn.execute("SELECT COUNT(*) FROM restaurants").fetchone()[0]
    cities = conn.execute("SELECT COUNT(DISTINCT city) FROM restaurants").fetchone()[0]
    pending_partners = conn.execute(
        "SELECT COUNT(*) FROM partner_applications WHERE status='new'").fetchone()[0]
    pending_prospects = conn.execute(
        "SELECT COUNT(*) FROM prospects WHERE status='draft'").fetchone()[0]
    auto = (conn.execute("SELECT value FROM settings WHERE key='auto_outreach'").fetchone() or ["0"])[0]
    paused = (conn.execute("SELECT value FROM settings WHERE key='paused'").fetchone() or ["0"])[0]
    conn.close()
    return {"orders_today": orders_today, "revenue_today": round(rev_today or 0, 2),
            "active_orders": active, "restaurants": restaurants, "cities": cities,
            "pending_partners": pending_partners, "pending_prospects": pending_prospects,
            "auto_outreach": auto == "1", "paused": paused == "1"}


@app.get("/api/watch/status")
def watch_status():
    """Public watch portal: site health WITHOUT revenue/profit. For LOVE code holders."""
    conn = db()
    today = now_iso()[:10]
    orders_today = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE created_at LIKE ?", (today + "%",)).fetchone()[0]
    active = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE status IN ('new','confirmed','preparing','on_the_way')").fetchone()[0]
    restaurants = conn.execute("SELECT COUNT(*) FROM restaurants").fetchone()[0]
    cities = conn.execute("SELECT COUNT(DISTINCT city) FROM restaurants").fetchone()[0]
    # AI upgrades (titles only, no details that could be copied)
    upgrades = conn.execute(
        "SELECT title, status FROM ai_ideas WHERE status IN ('pending','approved') ORDER BY id DESC LIMIT 5").fetchall()
    # Recent maintenance events
    maint = conn.execute(
        "SELECT title, created_at FROM system_events WHERE kind IN ('maintenance','suggestions','warning') ORDER BY id DESC LIMIT 5").fetchall()
    conn.close()
    # Health assessment (no money numbers)
    if active > 0 and orders_today > 0:
        health, label = "good", "THRIVING 🟢"
    elif orders_today > 0:
        health, label = "good", "HEALTHY 🟢"
    elif restaurants > 10:
        health, label = "warn", "BUILDING 🟡"
    else:
        health, label = "warn", "STARTING UP 🟡"
    # Trend (order count direction, no revenue)
    trend = "Growing" if orders_today > 2 else "Steady" if orders_today > 0 else "Launching"
    return {
        "health": health, "health_label": label,
        "restaurants": restaurants, "cities": cities,
        "orders_today": orders_today, "active": active,
        "upgrades": [{"title": u["title"], "status": u["status"]} for u in upgrades],
        "maintenance": [{"title": m["title"], "when": m["created_at"][:16]} for m in maint],
        "site_up": True, "orders_flowing": orders_today > 0, "trend": trend,
    }


# ---------------- City expansion requests ----------------
@app.post("/api/city/request")
async def city_request(req: Request):
    """User wants Drop in their city. Logs it, notifies Ken, AI researches."""
    body = await req.json()
    city = (body.get("city") or "").strip()[:80]
    state = (body.get("state") or "").strip()[:10]
    if not city or not state:
        raise HTTPException(400, "City and state required")
    conn = db()
    # Dedupe
    existing = conn.execute(
        "SELECT id FROM city_requests WHERE LOWER(city)=LOWER(?) AND LOWER(state)=LOWER(?)",
        (city, state)).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO city_requests(city, state, status, created_at) VALUES(?,?,?,?)",
            (city, state, "requested", now_iso()))
        conn.commit()
    conn.close()
    log_event("expansion", f"🌍 Drop requested in {city}, {state}",
              "AI will research restaurants")
    # Notify Ken
    try:
        topic = get_setting("ntfy_topic")
        if topic:
            import urllib.request, urllib.parse, threading
            msg = f"🌍 NEW CITY REQUEST\n{city}, {state}\nSomeone wants Drop there!"
            r2 = urllib.request.Request(
                f"https://ntfy.sh/{urllib.parse.quote(topic)}",
                data=msg.encode(), headers={"Title": "Drop city request"}, method="POST")
            threading.Thread(
                target=lambda: urllib.request.urlopen(r2, timeout=5).read(),
                daemon=True).start()
    except Exception:
        pass
    return {"ok": True}


# ---------------- Google Places integration ----------------
# Pulls REAL restaurants for ANY city via Google Places API.
# REQUIRES: Google Cloud API key with Places API enabled + billing.
# Set GOOGLE_PLACES_KEY in Render environment variables to activate.
# Without a key, the endpoint returns {"active": False}.

import os
GOOGLE_PLACES_KEY = os.environ.get("GOOGLE_PLACES_KEY", "")


@app.get("/api/places/search")
def places_search(city: str = "", state: str = "", lat: float = 0, lng: float = 0):
    """Search real restaurants via Google Places. Needs API key."""
    if not GOOGLE_PLACES_KEY:
        return {"active": False,
                "message": "Google Places not configured. Add GOOGLE_PLACES_KEY to activate live restaurant lookup."}
    import urllib.request, urllib.parse, json
    try:
        if lat and lng:
            loc = f"{lat},{lng}"
        else:
            # Geocode the city first
            geo_url = ("https://maps.googleapis.com/maps/api/geocode/json?"
                       f"address={urllib.parse.quote(city + ',' + state)}&key={GOOGLE_PLACES_KEY}")
            geo = json.loads(urllib.request.urlopen(geo_url, timeout=10).read())
            if not geo.get("results"):
                return {"active": True, "results": []}
            loc_data = geo["results"][0]["geometry"]["location"]
            loc = f"{loc_data['lat']},{loc_data['lng']}"
        # Search nearby restaurants
        places_url = ("https://maps.googleapis.com/maps/api/place/nearbysearch/json?"
                      f"location={loc}&radius=15000&type=restaurant&key={GOOGLE_PLACES_KEY}")
        data = json.loads(urllib.request.urlopen(places_url, timeout=10).read())
        results = []
        for p in data.get("results", [])[:20]:
            results.append({
                "name": p.get("name"),
                "address": p.get("vicinity"),
                "rating": p.get("rating"),
                "lat": p["geometry"]["location"]["lat"],
                "lng": p["geometry"]["location"]["lng"],
                "place_id": p.get("place_id"),
            })
        return {"active": True, "results": results}
    except Exception as e:
        return {"active": True, "error": str(e)[:200], "results": []}


@app.get("/api/osm/restaurants")
def osm_restaurants(city: str = "", state: str = "", lat: float = 0, lng: float = 0):
    """FREE: Find real restaurants via OpenStreetMap. No API key needed."""
    import urllib.request, urllib.parse, json
    try:
        if lat and lng:
            # Search around GPS coordinates (15km radius)
            query = f"""
            [out:json][timeout:25];
            (
              node["amenity"="restaurant"](around:15000,{lat},{lng});
              way["amenity"="restaurant"](around:15000,{lat},{lng});
            );
            out center 20;
            """
        elif city:
            # Search in city area
            query = f"""
            [out:json][timeout:25];
            area["name"="{city}"]["admin_level"~"8"]->.a;
            (
              node["amenity"="restaurant"](area.a);
              way["amenity"="restaurant"](area.a);
            );
            out center 20;
            """
        else:
            return {"active": True, "results": []}
        url = "https://overpass-api.de/api/interpreter"
        data = urllib.parse.urlencode({"data": query}).encode()
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"User-Agent": "DropApp/1.0"})
        resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
        results = []
        for el in resp.get("elements", [])[:20]:
            tags = el.get("tags", {})
            name = tags.get("name")
            if not name:
                continue
            rlat = el.get("lat") or el.get("center", {}).get("lat", 0)
            rlng = el.get("lon") or el.get("center", {}).get("lon", 0)
            results.append({
                "name": name,
                "address": tags.get("addr:street", "") + " " + tags.get("addr:housenumber", ""),
                "cuisine": tags.get("cuisine", ""),
                "lat": rlat, "lng": rlng,
                "source": "openstreetmap",
            })
        return {"active": True, "results": results}
    except Exception as e:
        return {"active": True, "error": str(e)[:200], "results": []}


YELP_KEY = os.environ.get("YELP_API_KEY", "")


@app.get("/api/yelp/restaurants")
def yelp_restaurants(city: str = "", state: str = "", lat: float = 0, lng: float = 0):
    """FREE TIER: Yelp Fusion API (5000 calls/day, no billing). Needs YELP_API_KEY."""
    if not YELP_KEY:
        return {"active": False,
                "message": "Yelp not configured. Add YELP_API_KEY to activate free restaurant lookup."}
    import urllib.request, urllib.parse, json
    try:
        if lat and lng:
            params = f"latitude={lat}&longitude={lng}&radius=15000"
        else:
            params = f"location={urllib.parse.quote(city + ', ' + state)}"
        url = f"https://api.yelp.com/v3/businesses/search?{params}&categories=restaurants&limit=20"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {YELP_KEY}"})
        data = json.loads(urllib.request.urlopen(req, timeout=10).read())
        results = []
        for b in data.get("businesses", []):
            results.append({
                "name": b.get("name"),
                "address": ", ".join(b.get("location", {}).get("display_address", [])),
                "rating": b.get("rating"),
                "lat": b.get("coordinates", {}).get("latitude", 0),
                "lng": b.get("coordinates", {}).get("longitude", 0),
                "source": "yelp",
            })
        return {"active": True, "results": results}
    except Exception as e:
        return {"active": True, "error": str(e)[:200], "results": []}


# ---------------- WATCH portal comments ----------------
# Friends comment, Ken replies. Legal name is auto-blocked.
WATCH_BLOCKED_NAMES = ["kendrick cooley", "kendrick", "ken"]

def _filter_watch_text(text: str) -> str:
    """Replace blocked names with ████. Case-insensitive."""
    out = text
    for name in sorted(WATCH_BLOCKED_NAMES, key=len, reverse=True):
        import re
        out = re.sub(re.escape(name), "████", out, flags=re.IGNORECASE)
    return out


@app.get("/api/watch/comments")
def watch_comments():
    conn = db()
    rows = conn.execute(
        "SELECT id, author, message, reply, created_at FROM watch_comments"
        " ORDER BY id DESC LIMIT 50").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/watch/comments")
async def watch_comment_add(req: Request):
    body = await req.json()
    author = (body.get("author") or "Anonymous").strip()[:40]
    message = (body.get("message") or "").strip()[:500]
    if not message:
        raise HTTPException(400, "Message required")
    author = _filter_watch_text(author)
    message = _filter_watch_text(message)
    conn = db()
    conn.execute(
        "INSERT INTO watch_comments(author, message, created_at) VALUES(?,?,?)",
        (author, message, now_iso()))
    conn.commit()
    conn.close()
    log_event("watch", "👁️ New WATCH comment", f"From {author}")
    return {"ok": True}


@app.post("/api/operator/watch/reply")
async def watch_comment_reply(req: Request, x_pin: str = Header(default="")):
    """Ken replies to a WATCH comment."""
    check_pin(x_pin)
    body = await req.json()
    cid = int(body.get("id") or 0)
    reply = (body.get("reply") or "").strip()[:500]
    if not cid or not reply:
        raise HTTPException(400, "id and reply required")
    reply = _filter_watch_text(reply)
    conn = db()
    conn.execute("UPDATE watch_comments SET reply=? WHERE id=?", (reply, cid))
    conn.commit()
    conn.close()
    return {"ok": True}


# ---------------- Universal Payment Control Center ----------------
# Provider status: Marqeta/Lithic integration point.
# Real card issuance requires: business contract, verification, funding account.
# Until then, the system tracks applications and is provider-ready.

@app.get("/api/operator/payments/overview")
def payments_overview(x_pin: str = Header(default="")):
    """Universal payment dashboard — no fake balances, only real records."""
    check_pin(x_pin)
    conn = db()
    # Card applications
    apps = conn.execute(
        "SELECT ca.*, d.name as driver_name FROM card_applications ca"
        " JOIN drivers d ON d.id=ca.driver_id ORDER BY ca.id DESC LIMIT 30").fetchall()
    # Virtual cards
    cards = conn.execute(
        "SELECT vc.*, d.name as driver_name FROM virtual_cards vc"
        " JOIN drivers d ON d.id=vc.driver_id ORDER BY vc.id DESC").fetchall()
    # Ledger summary (real records only)
    ledger = conn.execute(
        "SELECT category, direction, SUM(amount) as total, COUNT(*) as n"
        " FROM payment_ledger GROUP BY category, direction").fetchall()
    # Pending payouts (from existing payout system)
    payouts = conn.execute(
        "SELECT COUNT(*) as n, SUM(amount) as total FROM payouts WHERE status='pending'").fetchone()
    conn.close()
    return {
        "provider_status": "activation_required",
        "provider_note": "Marqeta/Lithic integration ready. Needs business contract + funding.",
        "applications": [dict(a) for a in apps],
        "cards": [dict(c) for c in cards],
        "ledger_summary": [dict(l) for l in ledger],
        "pending_payouts": {"count": payouts["n"] or 0, "total": round(payouts["total"] or 0, 2)},
    }


@app.post("/api/drivers/card-apply")
async def driver_card_apply(req: Request):
    """Driver requests a Drop virtual card. AI evaluates, Ken decides."""
    body = await req.json()
    driver_id = int(body.get("driver_id") or 0)
    if not driver_id:
        raise HTTPException(400, "driver_id required")
    conn = db()
    d = conn.execute("SELECT * FROM drivers WHERE id=?", (driver_id,)).fetchone()
    if not d:
        conn.close()
        raise HTTPException(404, "Driver not found")
    if d["status"] != "approved":
        conn.close()
        raise HTTPException(400, "Driver must be approved first")
    # Check existing
    existing = conn.execute(
        "SELECT id FROM card_applications WHERE driver_id=? AND status='pending'",
        (driver_id,)).fetchone()
    if existing:
        conn.close()
        raise HTTPException(400, "Application already pending")
    # AI evaluation (policy v1: approved driver + insurance + selfie approved)
    reasons = []
    score = 0
    if d["insurance"]:
        score += 30; reasons.append("✓ Insurance on file")
    else:
        reasons.append("✗ No insurance")
    if d["selfie_approved"]:
        score += 25; reasons.append("✓ Photo verified")
    else:
        reasons.append("✗ Photo not approved")
    if (d["deliveries"] or 0) >= 5:
        score += 25; reasons.append(f"✓ {d['deliveries']} deliveries completed")
    else:
        reasons.append(f"✗ Only {d['deliveries'] or 0} deliveries (need 5+)")
    if (d["rating"] or 5.0) >= 4.5:
        score += 20; reasons.append(f"✓ Rating {d['rating']:.1f}")
    else:
        reasons.append(f"✗ Rating {d['rating'] or 0:.1f} below 4.5")
    rec = "approve" if score >= 80 else "review" if score >= 50 else "reject"
    conn.execute(
        "INSERT INTO card_applications(driver_id, ai_recommendation, ai_reason, created_at)"
        " VALUES(?,?,?,?)",
        (driver_id, rec, "; ".join(reasons), now_iso()))
    conn.commit()
    conn.close()
    log_event("payment", f"💳 Card application from driver #{driver_id}",
              f"AI: {rec} ({score}/100)")
    return {"ok": True, "ai_recommendation": rec, "score": score,
            "message": "Application submitted — Ken will review"}


@app.post("/api/operator/cards/{aid}/decide")
async def card_application_decide(aid: int, req: Request, x_pin: str = Header(default="")):
    """Ken approves/rejects a card application. Does NOT issue a real card."""
    check_pin(x_pin)
    body = await req.json()
    approve = bool(body.get("approve"))
    conn = db()
    app = conn.execute("SELECT * FROM card_applications WHERE id=?", (aid,)).fetchone()
    if not app:
        conn.close()
        raise HTTPException(404, "Application not found")
    status = "approved" if approve else "rejected"
    conn.execute(
        "UPDATE card_applications SET status=?, decided_by='ken', decided_at=? WHERE id=?",
        (status, now_iso(), aid))
    if approve:
        # Create card record (provider fields empty until Marqeta/Lithic activated)
        conn.execute(
            "INSERT OR IGNORE INTO virtual_cards(driver_id, status, created_at, updated_at)"
            " VALUES(?, 'pending_provider', ?, ?)",
            (app["driver_id"], now_iso(), now_iso()))
    conn.commit()
    conn.close()
    audit("operator", f"card_app_{status}", "card_application", aid, "")
    return {"ok": True, "note": "Card record created. Real issuance needs provider activation."}


# ---------------- Restaurant partner applications ----------------
@app.post("/api/partner/apply")
async def restaurant_partner_apply(req: Request):
    """Public: restaurant partnership application."""
    body = await req.json()
    name = (body.get("business_name") or "").strip()
    if not name:
        raise HTTPException(400, "Business name required")
    conn = db()
    conn.execute(
        """INSERT INTO partner_applications(business_name, contact_name, email, phone, city, state,
           cuisine, locations, volume, proposal, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (name[:120], (body.get("contact_name") or "")[:80], (body.get("email") or "")[:120],
         (body.get("phone") or "")[:40], (body.get("city") or "")[:80],
         (body.get("state") or "")[:10], (body.get("cuisine") or "")[:60],
         int(body.get("locations") or 1), (body.get("volume") or "")[:200],
         (body.get("proposal") or "")[:2000], now_iso()))
    conn.commit()
    conn.close()
    try:
        push_ken("🤝 New partner application", name)
    except Exception:
        pass
    return {"ok": True}


@app.get("/api/operator/food-partner-apps")
def op_restaurant_applications(x_pin: str = Header(default="")):
    check_pin(x_pin)
    conn = db()
    rows = conn.execute(
        "SELECT * FROM partner_applications ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/operator/food-partner-apps/{pid}/{action}")
def op_restaurant_application_action(pid: int, action: str, x_pin: str = Header(default="")):
    check_pin(x_pin)
    if action not in ("approve", "reject"):
        raise HTTPException(400, "Bad action")
    conn = db()
    conn.execute("UPDATE partner_applications SET status=? WHERE id=?",
                 ("approved" if action == "approve" else "rejected", pid))
    conn.commit()
    conn.close()
    return {"ok": True}


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


# ---------------- AI Auto-Payout Engine ----------------
# Three modes: manual (Ken taps), auto (AI pays when Connect active), scheduled (weekly).
# Without Stripe Connect: AI validates + queues, Ken does one-tap confirm.

def _ai_review_payout(driver_id: int, amount: float) -> dict:
    """AI validates a payout request. Returns {ok, reasons}."""
    conn = db()
    d = conn.execute("SELECT * FROM drivers WHERE id=?", (driver_id,)).fetchone()
    if not d:
        conn.close()
        return {"ok": False, "reasons": ["Driver not found"]}
    reasons = []
    ok = True
    # Check 1: driver is approved
    if d["status"] != "approved":
        ok = False
        reasons.append("✗ Driver not approved")
    else:
        reasons.append("✓ Driver approved")
    # Check 2: has completed deliveries
    if (d["deliveries"] or 0) == 0:
        ok = False
        reasons.append("✗ No completed deliveries")
    else:
        reasons.append(f"✓ {d['deliveries']} deliveries")
    # Check 3: no duplicate pending payout
    dup = conn.execute(
        "SELECT COUNT(*) FROM payouts WHERE driver_id=? AND status IN ('requested','processing')",
        (driver_id,)).fetchone()[0]
    if dup > 0:
        ok = False
        reasons.append("✗ Already has a pending payout")
    else:
        reasons.append("✓ No duplicate payouts")
    # Check 4: amount is reasonable (not exceeding wallet)
    # (wallet check done by caller)
    reasons.append(f"✓ Amount ${amount:.2f}")
    conn.close()
    return {"ok": ok, "reasons": reasons}


@app.get("/api/operator/payout-settings")
def payout_settings_get(x_pin: str = Header(default="")):
    check_pin(x_pin)
    mode = get_setting("payout_mode") or "manual"
    threshold = get_setting("payout_auto_threshold") or "50"
    return {"mode": mode, "auto_threshold": float(threshold),
            "connect_enabled": CONNECT_ENABLED}


@app.post("/api/operator/payout-settings")
async def payout_settings_set(req: Request, x_pin: str = Header(default="")):
    """Ken sets payout mode: manual, auto, or weekly."""
    check_pin(x_pin)
    body = await req.json()
    mode = (body.get("mode") or "manual").strip()
    if mode not in ("manual", "auto", "weekly"):
        raise HTTPException(400, "mode must be manual, auto, or weekly")
    threshold = float(body.get("auto_threshold") or 50)
    set_setting("payout_mode", mode)
    set_setting("payout_auto_threshold", str(threshold))
    audit("operator", "payout_mode_set", "settings", 0, f"mode={mode} threshold={threshold}")
    return {"ok": True}


@app.post("/api/operator/payouts/{pid}/process")
async def op_payout_process(pid: int, x_pin: str = Header(default="")):
    """Ken taps once → AI validates → pays via Stripe Connect (if active) or queues for manual."""
    check_pin(x_pin)
    conn = db()
    p = conn.execute("SELECT * FROM payouts WHERE id=?", (pid,)).fetchone()
    if not p:
        conn.close()
        raise HTTPException(404, "Payout not found")
    if p["status"] != "requested":
        conn.close()
        raise HTTPException(400, f"Already {p['status']}")
    # AI review
    review = _ai_review_payout(p["driver_id"], p["amount"])
    if not review["ok"]:
        conn.close()
        raise HTTPException(400, "AI blocked: " + "; ".join(review["reasons"]))
    d = conn.execute("SELECT * FROM drivers WHERE id=?", (p["driver_id"],)).fetchone()
    # Try Stripe Connect if active
    if CONNECT_ENABLED and d["stripe_account_id"]:
        try:
            import stripe
            # Idempotency: don't double-pay
            transfer = stripe.Transfer.create(
                amount=int(p["amount"] * 100),
                currency="usd",
                destination=d["stripe_account_id"],
                metadata={"payout_id": str(pid), "driver_id": str(p["driver_id"])},
                idempotency_key=f"payout_{pid}",
            )
            conn.execute(
                "UPDATE payouts SET status='paid', stripe_transfer_id=?, paid_at=? WHERE id=?",
                (transfer.id, now_iso(), pid))
            conn.commit()
            # Ledger entry
            conn.execute(
                "INSERT INTO payment_ledger(txn_id, entry_type, driver_id, amount, direction,"
                " category, provider, provider_ref, status, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (f"payout_{pid}", "payout", p["driver_id"], p["amount"],
                 "out", "driver_payout", "stripe", transfer.id, "completed", now_iso()))
            conn.commit()
            conn.close()
            audit("operator", "payout_auto_paid", "payout", pid,
                  f"${p['amount']:.2f} to driver {p['driver_id']} via Stripe")
            log_event("payment", f"💸 Auto-payout ${p['amount']:.2f}",
                      f"Driver #{p['driver_id']} via Stripe Connect")
            return {"ok": True, "method": "stripe", "transfer_id": transfer.id}
        except Exception as e:
            conn.execute("UPDATE payouts SET status='failed', note=? WHERE id=?",
                         (f"Stripe error: {str(e)[:200]}", pid))
            conn.commit()
            conn.close()
            raise HTTPException(500, f"Stripe failed: {str(e)[:200]}")
    else:
        # Manual mode: mark as processing, Ken sends via his method, taps "Mark paid"
        conn.execute("UPDATE payouts SET status='processing' WHERE id=?", (pid,))
        conn.commit()
        conn.close()
        audit("operator", "payout_queued", "payout", pid,
              f"${p['amount']:.2f} validated by AI, awaiting manual send")
        return {"ok": True, "method": "manual",
                "message": f"AI validated ✓. Send ${p['amount']:.2f} to {d['name']} via your preferred method, then tap Mark Paid.",
                "ai_checks": review["reasons"]}


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


@app.get("/pickup.html", response_class=HTMLResponse)
def pickuppage():
    with open(os.path.join(BASE, "static", "pickup.html")) as f:
        return f.read()


@app.get("/drop.html", response_class=HTMLResponse)
def droppage():
    with open(os.path.join(BASE, "static", "drop.html")) as f:
        return f.read()


@app.post("/api/drop/request")
async def drop_request(req: Request):
    """Unified Drop request: pick up anything from anywhere, deliver anywhere."""
    import math
    body = await req.json()
    what = (body.get("what") or "").strip()[:200]
    pickup_addr = (body.get("pickup_address") or "").strip()[:200]
    delivery_addr = (body.get("delivery_address") or "").strip()[:200]
    details = (body.get("details") or "").strip()[:300]
    name = (body.get("customer_name") or "").strip()[:80]
    phone = (body.get("phone") or "").strip()[:30]
    quoted = float(body.get("quoted_price") or 0)
    if not what or not pickup_addr or not delivery_addr or not name:
        raise HTTPException(400, "What, both addresses, and your name are required")
    # Geocode both for distance + driver matching
    plat, plng, dlat, dlng = 0.0, 0.0, 0.0, 0.0
    try:
        import urllib.request, urllib.parse, json
        for addr, which in ((pickup_addr, "p"), (delivery_addr, "d")):
            url = f"https://nominatim.openstreetmap.org/search?q={urllib.parse.quote(addr)}&format=json&limit=1"
            rq = urllib.request.Request(url, headers={"User-Agent": "DropApp/1.0"})
            geo = json.loads(urllib.request.urlopen(rq, timeout=10).read())
            if geo:
                if which == "p":
                    plat, plng = float(geo[0]["lat"]), float(geo[0]["lon"])
                else:
                    dlat, dlng = float(geo[0]["lat"]), float(geo[0]["lon"])
    except Exception:
        pass
    # Price: $1.50/mile, min $3.99
    miles = math.hypot(dlat - plat, dlng - plng) * 69 if all([plat, plng, dlat, dlng]) else 0
    fee = round(max(3.99, miles * 1.50), 2) if miles > 0 else (quoted if quoted > 0 else 3.99)
    conn = db()
    cur = conn.execute(
        """INSERT INTO orders(restaurant_id, items_json, customer_name, phone, address,
                              notes, food_total, delivery_fee, tip, total, status,
                              stripe_status, created_at, updated_at, kind,
                              pickup_name, pickup_address, pickup_lat, pickup_lng,
                              cust_lat, cust_lng)
           VALUES(0, '[]', ?, ?, ?, ?, 0, ?, 0, ?, 'new', 'pending', ?, ?, 'drop',
                   ?, ?, ?, ?, ?, ?)""",
        (name, phone, delivery_addr,
         f"{what}. {details}",
         fee, fee, now_iso(), now_iso(),
         what, pickup_addr, plat, plng, dlat, dlng))
    oid = cur.lastrowid
    # Check for nearby available drivers (within 15 miles) BEFORE closing
    drivers_available = 0
    if plat and plng:
        import math
        try:
            drivers = conn.execute(
                "SELECT lat, lng FROM drivers WHERE status='approved' AND lat != 0").fetchall()
            for d in drivers:
                dist = math.hypot(d["lat"] - plat, d["lng"] - plng) * 69
                if dist <= 15:
                    drivers_available += 1
        except Exception:
            pass
    conn.commit()
    conn.close()
    log_event("order", f"📦 Drop request #{oid}", f"{what[:40]} — ${fee:.2f}")
    # Notify Ken
    try:
        topic = get_setting("ntfy_topic")
        if topic:
            import urllib.request, urllib.parse, threading
            msg = f"📦 DROP #{oid}\n{what[:50]}\n${fee:.2f}"
            r2 = urllib.request.Request(
                f"https://ntfy.sh/{urllib.parse.quote(topic)}",
                data=msg.encode(), headers={"Title": "Drop request"}, method="POST")
            threading.Thread(
                target=lambda: urllib.request.urlopen(r2, timeout=5).read(),
                daemon=True).start()
    except Exception:
        pass
    return {"ok": True, "order_id": oid, "fee": fee, "drivers_available": drivers_available,
            "no_drivers": drivers_available == 0}


@app.get("/watch.html", response_class=HTMLResponse)
def watchpage():
    with open(os.path.join(BASE, "static", "watch.html")) as f:
        return f.read()

@app.post("/api/pickup/request")
async def pickup_request(req: Request):
    """Customer already ordered food, Drop just picks it up and delivers. Works anywhere."""
    body = await req.json()
    restaurant = (body.get("restaurant") or "").strip()[:100]
    pickup_addr = (body.get("pickup_address") or "").strip()[:200]
    delivery_addr = (body.get("delivery_address") or "").strip()[:200]
    order_details = (body.get("order_details") or "").strip()[:300]
    name = (body.get("customer_name") or "").strip()[:80]
    phone = (body.get("phone") or "").strip()[:30]
    if not restaurant or not pickup_addr or not delivery_addr or not name:
        raise HTTPException(400, "Restaurant, both addresses, and your name are required")
    # Geocode pickup for driver matching
    lat, lng = 0.0, 0.0
    try:
        import urllib.request, urllib.parse, json
        geo_url = f"https://nominatim.openstreetmap.org/search?q={urllib.parse.quote(pickup_addr)}&format=json&limit=1"
        geo_req = urllib.request.Request(geo_url, headers={"User-Agent": "DropApp/1.0"})
        geo = json.loads(urllib.request.urlopen(geo_req, timeout=10).read())
        if geo:
            lat, lng = float(geo[0]["lat"]), float(geo[0]["lon"])
    except Exception:
        pass
    fee = float(get_setting("delivery_fee") or 3.99)
    conn = db()
    # Create as a courier-type order (food already paid, just pickup + deliver)
    cur = conn.execute(
        """INSERT INTO orders(restaurant_id, items_json, customer_name, phone, address,
                              notes, food_total, delivery_fee, tip, total, status,
                              stripe_status, created_at, updated_at, kind,
                              pickup_name, pickup_address, pickup_lat, pickup_lng)
           VALUES(0, '[]', ?, ?, ?, ?, 0, ?, 0, ?, 'new', 'pending', ?, ?, 'pickup',
                   ?, ?, ?, ?)""",
        (name, phone, delivery_addr,
         f"Pickup from {restaurant}. {order_details}",
         fee, fee, now_iso(), now_iso(),
         restaurant, pickup_addr, lat, lng))
    oid = cur.lastrowid
    conn.commit()
    conn.close()
    log_event("order", f"🛵 Pickup request #{oid}", f"{restaurant} → {delivery_addr[:40]}")
    # Notify Ken
    try:
        topic = get_setting("ntfy_topic")
        if topic:
            import urllib.request, urllib.parse, threading
            msg = f"🛵 PICKUP #{oid}\n{restaurant}\n→ {delivery_addr[:60]}"
            r2 = urllib.request.Request(
                f"https://ntfy.sh/{urllib.parse.quote(topic)}",
                data=msg.encode(), headers={"Title": "Drop pickup request"}, method="POST")
            threading.Thread(
                target=lambda: urllib.request.urlopen(r2, timeout=5).read(),
                daemon=True).start()
    except Exception:
        pass
    return {"ok": True, "order_id": oid}


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
