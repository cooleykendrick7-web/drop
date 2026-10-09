# Drop — "We drop it at your door."

Single-operator food delivery. You (Ken) are the only driver. Customers order
on the site, pay online, and every order comes straight to you. You pick up
the food, drop it at the door, and keep the delivery fee on every order.

## What's in here

- `app.py` — the whole backend (FastAPI) + API
- `static/` — the customer site and your operator dashboard (plain HTML/JS, phone-first)
- `drop.db` — the real database (SQLite). Every order, menu change, and setting lives here.
- `venv/` — Python environment with everything installed

## Run it locally (your computer)

```bash
cd ~/workspace/drop
./venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
```

Then open:

- Customers: `http://localhost:8000/`
- Your operator dashboard: `http://localhost:8000/dash` (PIN: `1234` — change it in Settings)

## How the money works

Right now the app is in **TEST MODE** — payments are simulated with a "TEST PAY"
button so the whole order flow works without real money. No fake data: orders,
settings, and earnings are all really saved in `drop.db`.

To take **real** money:

1. Make a free Stripe account at https://stripe.com (no card needed to start).
2. In the Stripe dashboard, go to Developers → API keys and copy:
   - **Publishable key** (starts with `pk_test_...`)
   - **Secret key** (starts with `sk_test_...`)
3. Start the app with your keys:
   ```bash
   STRIPE_SECRET_KEY='sk_test_...' STRIPE_PUBLISHABLE_KEY='pk_test_...' \
     ./venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
   ```
   The test-pay button disappears automatically and customers go through real
   Stripe checkout (you can use Stripe's test card `4242 4242 4242 4242` while
   the keys start with `sk_test_` — still no real money).
4. When you're ready for real dollars, flip the same keys to the **live**
   versions (`pk_live_...` / `sk_live_...`) in the Stripe dashboard.

Never put keys in chat, screenshots, or this README. They stay in your
terminal command only.

## Get phone alerts for new orders (free, no signup)

1. Install the free **ntfy** app on your phone (iOS App Store / Google Play).
2. In the app, tap **+ Subscribe** and type a secret topic name only you know,
   e.g. `drop-ken-orders-xyz123`.
3. Open your Drop dashboard → **Settings** → paste the same topic name → Save.
4. Every paid order will buzz your phone instantly. The dashboard also beeps
   and flashes a banner when it's open.

## Your dashboard can do

- See every order live (customer name, phone, address, notes, items, payout)
- Tap buttons to move an order: Confirmed → Picked up → On the way → Delivered
- **Earnings**: total delivery fees collected + per-day breakdown
- **Settings**: delivery fee (default **$3.99**), your PIN, ntfy topic
- **Menu**: add/edit/delete restaurants and menu items, mark open/closed

The 4 restaurants in there now (burger joint, soul food, Mexican, pizza) are
**samples** — replace them with your real local spots in the Menu tab.

## Put it on the internet (so customers can actually reach it)

Free options (pick one):

**Render (easiest)**
1. Push this folder to a GitHub repo.
2. Go to https://render.com → New → Web Service → connect the repo.
3. Build command: `pip install -r requirements.txt`
   Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
4. Add environment variables `STRIPE_SECRET_KEY` / `STRIPE_PUBLISHABLE_KEY`.
5. Render gives you a public URL like `https://drop-xxxx.onrender.com`.

**Railway** — same idea: https://railway.app → New Project → deploy from repo,
same build/start commands, add the Stripe env vars.

⚠️ One important note: the free tiers **sleep** after ~15 minutes of no traffic, and on Render's free plan there is **no persistent disk** — the SQLite file is wiped every time the service wakes up or redeploys, so orders placed today would be gone tomorrow. For real orders you need at least Render's Starter plan (~$7/month) with a disk mounted at the app folder, or switch `DROP_DB` to a managed Postgres. (Free tier is fine for testing the checkout flow — just not for real orders.) Ask and I can wire that up.

## Checklist before you take real orders

- [ ] Stripe account made, live keys added as env vars
- [ ] Delivery fee set (Settings tab) — $3.99 default
- [ ] PIN changed from 1234 (Settings tab)
- [ ] ntfy app installed + topic subscribed + saved in Settings
- [ ] Sample restaurants replaced with real ones (Menu tab)
- [ ] Decide your **delivery zone** (which neighborhoods/towns you'll serve —
      put it on the home page tagline in Settings)
- [ ] Test order placed with a real $1 Stripe test payment, tracked, delivered

## Order flow (what the customer sees)

Home → pick restaurant → add food to cart → checkout (name, phone, address)
→ pay with Stripe → confirmation + tracking link → live status updates as
you tap the buttons on your dashboard.
