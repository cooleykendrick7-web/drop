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
    "confirmed": "Confirmed by Ken",
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
    cur = conn.execute(
        """INSERT INTO orders(restaurant_id, items_json, customer_name, phone, address,
                              notes, food_total, delivery_fee, tip, discount, promo_code,
                              user_id, total, status, stripe_status, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'placed','pending',?,?)""",
        (rid, json.dumps(priced), name, phone, address, notes,
         food_total, fee, tip, discount, promo_code, user_id, total, now_iso(), now_iso()))
    oid = cur.lastrowid
    conn.commit()
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
    rest = conn.execute("SELECT name FROM restaurants WHERE id=?",
                        (o["restaurant_id"],)).fetchone()
    pos = 0
    if o["status"] not in ("delivered", "cancelled"):
        pos = conn.execute(
            "SELECT COUNT(*) c FROM orders WHERE status NOT IN ('delivered','cancelled') AND id <= ?",
            (oid,)).fetchone()["c"]
    conn.close()
    od = order_dict(o)
    return {
        "id": od["id"], "restaurant": rest["name"] if rest else "",
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
        """SELECT o.*, r.name AS restaurant_name FROM orders o
           JOIN restaurants r ON r.id = o.restaurant_id
           ORDER BY o.id DESC""").fetchall()
    out = []
    for r in rows:
        d = order_dict(r)
        d["restaurant_name"] = r["restaurant_name"]
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
    for k in ("name", "cuisine", "emoji", "eta"):
        if k in body:
            fields.append(f"{k}=?")
            vals.append(str(body[k]).strip()[:20] if k == "eta" else str(body[k]).strip())
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
