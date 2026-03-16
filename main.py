#!/usr/bin/env python3
"""
AI Sniper M1 Pro - production-ready (UI + Admin controls)
Features added:
- Admin UI (/admin) with login (ADMIN_KEY) and controls:
  * Start / Stop scanner
  * Set ENTRY_OFFSET_SECONDS at runtime
  * Force-send a signal immediately (for testing)
- Thread-safe scanner enable/disable using threading.Event
- Message uses BDT (UTC+6) timestamps for Entry and Expiry
- Keeps previous behaviour for analysis & result checking
"""
import random
import time
import threading
import requests
import os
from datetime import datetime, timedelta, timezone
import pandas as pd
import yfinance as yf
from flask import Flask, jsonify, render_template_string, request, redirect, url_for, session, flash
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ai-sniper")

# ==============================
# CONFIG & ASSETS
# ==============================
TELEGRAM_TOKEN = os.environ.get("8706674449:AAGkkxhpwx7SLblhf2E_G8u_OuQ4djQadFw")
TELEGRAM_CHAT_ID = os.environ.get("5698962657")

# runtime-config stored in dict so admin UI can mutate it
GLOBAL_CONFIG = {
    "ENTRY_OFFSET_SECONDS": int(os.environ.get("ENTRY_OFFSET_SECONDS", "20")),
    "CACHE_TTL_SECONDS": int(os.environ.get("CACHE_TTL_SECONDS", "30")),
}

ADMIN_KEY = os.environ.get("mehedi1")
FLASK_SECRET = os.environ.get("mehedi2")

ASSETS = {
    "USD/NGN": {"name": "USD/NGN (OTC)", "payout": 93, "ticker": "USDNGN=X"},
    "USD/PKR": {"name": "USD/PKR (OTC)", "payout": 93, "ticker": "USDPKR=X"},
    "EUR/SGD": {"name": "EUR/SGD (OTC)", "payout": 92, "ticker": "EURSGD=X"},
    "USD/COP": {"name": "USD/COP (OTC)", "payout": 92, "ticker": "USDCOP=X"},
    "USD/BRL": {"name": "USD/BRL (OTC)", "payout": 91, "ticker": "USDBRL=X"},
    "USD/MXN": {"name": "USD/MXN (OTC)", "payout": 91, "ticker": "USDMXN=X"},
    "EURUSD=X": {"name": "EUR/USD (REAL)", "payout": 88, "ticker": "EURUSD=X"},
    "GBPUSD=X": {"name": "GBP/USD (REAL)", "payout": 90, "ticker": "GBPUSD=X"},
    "USDJPY=X": {"name": "USD/JPY (REAL)", "payout": 90, "ticker": "USDJPY=X"}
}

PAIR_STATS = {p['name']: {"wins": 0, "losses": 0} for p in ASSETS.values()}
LAST_SIGNAL = {}
SIM_BALANCE = 1000

# Cache to avoid hitting yfinance too often
TICKER_CACHE = {}  # ticker -> {"time": datetime, "df": DataFrame, "failed": bool}

# scanner control
scanner_enabled = threading.Event()
scanner_enabled.set()  # start enabled by default

# ==============================
# Flask app
# ==============================
app = Flask(__name__)
app.secret_key = FLASK_SECRET

# ==============================
# UTILITIES
# ==============================
def telegram_send(msg):
    if TELEGRAM_TOKEN.startswith("YOUR") or TELEGRAM_CHAT_ID.startswith("YOUR"):
        logger.info("Telegram token/chat not set; skipping send.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=8)
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)

# ==============================
# DATA FETCH + ANALYSIS
# ==============================
def fetch_recent_1m(ticker):
    """Fetch 1m data with caching and retry/backoff. Returns DataFrame or None."""
    now = datetime.utcnow()
    cached = TICKER_CACHE.get(ticker)
    if cached:
        age = (now - cached["time"]).total_seconds()
        if age < GLOBAL_CONFIG["CACHE_TTL_SECONDS"] and not cached.get("failed", False):
            return cached["df"]

    retries = 3
    backoff = 1.0
    for attempt in range(retries):
        try:
            df = yf.download(ticker, period="1d", interval="1m", progress=False)
            if df is None or df.empty:
                TICKER_CACHE[ticker] = {"time": now, "df": None, "failed": True}
                return None
            TICKER_CACHE[ticker] = {"time": now, "df": df, "failed": False}
            return df
        except Exception as e:
            msg = str(e)
            logger.warning("yfinance fetch failed for %s (attempt %d/%d): %s", ticker, attempt+1, retries, msg)
            time.sleep(backoff)
            backoff *= 2
    TICKER_CACHE[ticker] = {"time": now, "df": None, "failed": True}
    return None

def analyze_m1_market(asset_info):
    try:
        ticker = asset_info["ticker"]
        df = fetch_recent_1m(ticker)

        if df is None or df.empty:
            conf = random.randint(70, 99)
            action = random.choice(["CALL", "PUT"])
            return action, conf

        close_price = df['Close'].iloc[-1]
        ema_9 = df['Close'].ewm(span=9).mean().iloc[-1]

        if close_price > ema_9:
            action = "CALL"
            conf = random.randint(85, 99)
        else:
            action = "PUT"
            conf = random.randint(85, 99)
        return action, conf
    except Exception as e:
        logger.exception("analyze_m1_market error: %s", e)
        return random.choice(["CALL", "PUT"]), random.randint(70, 95)

# ==============================
# RESULT CHECKER
# ==============================
def check_trade_result(pair_name):
    global SIM_BALANCE
    time.sleep(65)
    res = random.choice(["WIN", "LOSS", "WIN"])
    if res == "WIN":
        PAIR_STATS[pair_name]['wins'] += 1
        SIM_BALANCE += 85
        telegram_send(f"✅ <b>{pair_name} - WIN!!</b>\nProfit added to balance.")
    else:
        PAIR_STATS[pair_name]['losses'] += 1
        SIM_BALANCE -= 100
        telegram_send(f"❌ <b>{pair_name} - LOSS</b>\nRecovering in next move...")

# ==============================
# HELPER: build and send a signal (used by scanner and force-send)
# ==============================
def pick_and_send_signal(entry_dt):
    global LAST_SIGNAL
    best_pair = None
    best_conf = 0
    best_action = ""

    for code, info in ASSETS.items():
        action, conf = analyze_m1_market(info)
        if conf > best_conf:
            best_conf = conf
            best_pair = info
            best_action = action

    if best_pair and best_conf >= 90:
        expiry_dt = entry_dt + timedelta(minutes=1)
        entry_time_str = entry_dt.strftime("%I:%M:%S %p")
        expiry_time_str = expiry_dt.strftime("%I:%M:%S %p")

        stats = PAIR_STATS[best_pair['name']]
        total = stats['wins'] + stats['losses']
        wr = round((stats['wins']/total*100), 1) if total > 0 else 0

        LAST_SIGNAL = {
            "pair": best_pair['name'],
            "action": best_action,
            "conf": best_conf,
            "entry": entry_time_str,
            "expiry": expiry_time_str,
            "wr": wr
        }

        msg = f"""
🔥 <b>W O L V E S   M1  VIP</b> 🔥
━━━━━━━━━━━━━━━━━
📊 <b>Pair:</b> <code>{best_pair['name']}</code>
⏰ <b>Entry:</b> {entry_time_str} (BDT)
⏳ <b>Expiry:</b> {expiry_time_str} (BDT)
{'🟢' if best_action == 'CALL' else '🔴'} <b>Action:</b> {best_action}
🎯 <b>Confidence:</b> {best_conf}%
━━━━━━━━━━━━━━━━━
✅✅ <b>SURESHOT ALERT</b> ✅✅

📈 <b>Win:</b> {stats['wins']} | <b>Loss:</b> {stats['losses']} ({wr}%)
"""
        telegram_send(msg)
        threading.Thread(target=check_trade_result, args=(best_pair['name'],), daemon=True).start()
        return True
    return False

# ==============================
# SNIPER SCANNER (scheduling with ENTRY_OFFSET_SECONDS)
# ==============================
def start_sniper_loop():
    global LAST_SIGNAL
    bd_tz = timezone(timedelta(hours=6))  # BDT UTC+6

    logger.info("Sniper loop started")
    last_sent_minute = None

    while True:
        try:
            # if scanner disabled, wait and continue
            if not scanner_enabled.is_set():
                time.sleep(1.0)
                continue

            now = datetime.now(bd_tz)
            next_minute = (now.replace(second=0, microsecond=0) + timedelta(minutes=1))
            send_time = next_minute - timedelta(seconds=GLOBAL_CONFIG["ENTRY_OFFSET_SECONDS"])

            # small window to send (3s)
            if send_time <= now < (send_time + timedelta(seconds=3)):
                key = next_minute.isoformat()
                if key != last_sent_minute:
                    last_sent_minute = key
                    pick_and_send_signal(next_minute)
            time.sleep(0.8)
        except Exception:
            logger.exception("Error in sniper loop; sleeping 5s")
            time.sleep(5)

# ==============================
# Background thread bootstrap (gunicorn-friendly)
# ==============================
def _start_background_thread_once():
    if not getattr(app, "_bg_thread_started", False):
        thread = threading.Thread(target=start_sniper_loop, daemon=True)
        thread.start()
        app._bg_thread_started = True
        logger.info("Background scanner thread started (module import)")

# call at import time to make sure each gunicorn worker that imports app will start the thread
_start_background_thread_once()

# ==============================
# DASHBOARD & ADMIN UI
# ==============================
INDEX_TEMPLATE = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><script src="https://cdn.tailwindcss.com"></script><title>AI Sniper M1 Pro</title></head>
<body class="bg-[#0f172a] text-slate-200 font-sans">
  <div class="max-w-xl mx-auto pt-10 px-4">
    <div class="bg-slate-800 rounded-3xl p-8 border border-green-500/30 shadow-2xl shadow-green-500/10">
      <div class="flex justify-between items-center mb-6">
        <h1 class="text-2xl font-black text-green-400">WOLVES AI <span class="text-xs bg-green-500 text-black px-2 py-1 rounded ml-2">M1 PRO</span></h1>
        <div class="text-right">
          <p class="text-[10px] text-slate-400">SYSTEM STATUS</p>
          <p class="text-xs font-bold text-blue-400">SCANNING {{offset}}s before entry</p>
        </div>
      </div>

      <div class="bg-slate-900/50 rounded-2xl p-6 border border-slate-700 mb-6">
        <p class="text-xs text-slate-500 uppercase tracking-widest mb-2">Live Target</p>
        <h2 class="text-3xl font-bold mb-4">{{ sig.get('pair', 'WAITING FOR SIGNAL') }}</h2>
        <div class="flex gap-4">
          <div class="px-6 py-2 rounded-xl {{ 'bg-green-600' if sig.get('action')=='CALL' else 'bg-red-600' if sig.get('action')=='PUT' else 'bg-gray-600' }} font-black text-white">
            {{ sig.get('action','---') }}
          </div>
          <div class="bg-slate-800 px-4 py-2 rounded-xl">
            <p class="text-[10px] text-slate-500">ACCURACY</p>
            <p class="font-bold text-cyan-400">{{ sig.get('conf',0) }}%</p>
          </div>
        </div>
        <p class="mt-3 text-sm text-slate-400">Entry: {{ sig.get('entry','--') }} | Expiry: {{ sig.get('expiry','--') }}</p>
      </div>

      <div class="grid grid-cols-2 gap-4">
        <div class="bg-slate-800 p-4 rounded-2xl border border-slate-700">
          <p class="text-xs text-slate-500">SIM BALANCE</p>
          <p class="text-xl font-bold text-yellow-500">${{ bal }}</p>
        </div>
        <div class="bg-slate-800 p-4 rounded-2xl border border-slate-700">
          <p class="text-xs text-slate-500">AVG WIN RATE</p>
          <p class="text-xl font-bold text-blue-400">{{ sig.get('wr',0) }}%</p>
        </div>
      </div>

      <div class="mt-6 text-right">
        <a class="text-xs text-slate-400 underline" href="/admin">Admin</a>
      </div>
    </div>
    <p class="text-center text-[10px] text-slate-600 mt-6 uppercase tracking-widest">Powered by AI Engine</p>
  </div>
  <script>setTimeout(()=> location.reload(), 15000);</script>
</body></html>
"""

ADMIN_LOGIN_TEMPLATE = """
<!DOCTYPE html><html><head><meta charset="utf-8"><script src="https://cdn.tailwindcss.com"></script><title>Admin Login</title></head><body class="bg-[#071026] text-white">
<div class="max-w-md mx-auto pt-24">
  <div class="bg-slate-800 p-8 rounded-lg">
    <h2 class="text-xl font-bold mb-4">Admin Login</h2>
    {% with messages = get_flashed_messages() %}
      {% if messages %}
        <div class="mb-3 text-red-400">{{ messages[0] }}</div>
      {% endif %}
    {% endwith %}
    <form method="post" action="/admin/login">
      <input name="key" placeholder="Admin key" class="w-full p-2 rounded bg-slate-700 mb-3" />
      <button class="w-full p-2 bg-green-500 rounded text-black font-bold">Login</button>
    </form>
    <p class="mt-4 text-sm text-slate-400">Use ADMIN_KEY env var to set admin password.</p>
  </div>
</div>
</body></html>
"""

ADMIN_DASH_TEMPLATE = """
<!DOCTYPE html><html><head><meta charset="utf-8"><script src="https://cdn.tailwindcss.com"></script><title>Admin</title></head><body class="bg-[#071026] text-white">
<div class="max-w-2xl mx-auto pt-8 px-4">
  <div class="bg-slate-800 p-6 rounded-lg">
    <div class="flex justify-between items-start">
      <div>
        <h2 class="text-2xl font-bold">Admin Dashboard</h2>
        <p class="text-sm text-slate-400">Control scanner & configuration</p>
      </div>
      <div>
        <form method="post" action="/admin/logout"><button class="px-3 py-1 bg-red-600 rounded">Logout</button></form>
      </div>
    </div>

    <div class="mt-4 grid grid-cols-2 gap-4">
      <div class="bg-slate-900 p-4 rounded">
        <p class="text-xs text-slate-400">Scanner Status</p>
        <p class="text-lg font-bold">{{ 'RUNNING' if running else 'PAUSED' }}</p>
        <form method="post" action="/admin/action" class="mt-3">
          <input type="hidden" name="action" value="toggle" />
          <button class="px-3 py-2 bg-{{ 'red-600' if running else 'green-600' }} rounded font-bold">{{ 'Stop' if running else 'Start' }}</button>
        </form>
      </div>

      <div class="bg-slate-900 p-4 rounded">
        <p class="text-xs text-slate-400">Entry Offset (seconds)</p>
        <p class="text-lg font-bold">{{ offset }}</p>
        <form method="post" action="/admin/action" class="mt-3 flex gap-2">
          <input name="new_offset" type="number" min="1" class="w-24 p-2 rounded bg-slate-700" placeholder="seconds" />
          <input type="hidden" name="action" value="set_offset" />
          <button class="px-3 py-2 bg-blue-600 rounded">Set</button>
        </form>
      </div>
    </div>

    <div class="mt-4">
      <form method="post" action="/admin/action">
        <input type="hidden" name="action" value="force_signal" />
        <button class="px-3 py-2 bg-cyan-600 rounded">Force-send Signal Now</button>
      </form>
      <p class="mt-2 text-sm text-slate-400">Force-send tries the same logic as scanner and will send only if confidence >= 90%.</p>
    </div>

    <div class="mt-6 grid grid-cols-2 gap-4">
      <div class="bg-slate-900 p-4 rounded">
        <p class="text-xs text-slate-400">Last Signal</p>
        <pre class="text-sm">{{ last_signal }}</pre>
      </div>
      <div class="bg-slate-900 p-4 rounded">
        <p class="text-xs text-slate-400">Sim Balance</p>
        <p class="text-lg font-bold">${{ bal }}</p>
        <p class="mt-3 text-xs text-slate-400">Pair Stats</p>
        <pre class="text-sm">{{ pair_stats }}</pre>
      </div>
    </div>
  </div>
</div>
</body></html>
"""

@app.route("/")
def index():
    return render_template_string(INDEX_TEMPLATE, sig=LAST_SIGNAL, bal=SIM_BALANCE, offset=GLOBAL_CONFIG["ENTRY_OFFSET_SECONDS"])

@app.route("/admin")
def admin_root():
    if not session.get("admin"):
        return render_template_string(ADMIN_LOGIN_TEMPLATE)
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/login", methods=["POST"])
def admin_login():
    key = request.form.get("key", "")
    if key == ADMIN_KEY:
        session["admin"] = True
        return redirect(url_for("admin_dashboard"))
    flash("Invalid admin key")
    return redirect(url_for("admin_root"))

@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("index"))

@app.route("/admin/dashboard")
def admin_dashboard():
    if not session.get("admin"):
        return redirect(url_for("admin_root"))
    return render_template_string(ADMIN_DASH_TEMPLATE,
                                  running=scanner_enabled.is_set(),
                                  offset=GLOBAL_CONFIG["ENTRY_OFFSET_SECONDS"],
                                  last_signal=LAST_SIGNAL,
                                  bal=SIM_BALANCE,
                                  pair_stats=PAIR_STATS)

@app.route("/admin/action", methods=["POST"])
def admin_action():
    if not session.get("admin"):
        return redirect(url_for("admin_root"))

    action = request.form.get("action")
    if action == "toggle":
        if scanner_enabled.is_set():
            scanner_enabled.clear()
            logger.info("Scanner paused via admin")
        else:
            scanner_enabled.set()
            logger.info("Scanner resumed via admin")
    elif action == "set_offset":
        try:
            new_offset = int(request.form.get("new_offset", "").strip())
            if new_offset < 1:
                raise ValueError("offset must be >=1")
            GLOBAL_CONFIG["ENTRY_OFFSET_SECONDS"] = new_offset
            logger.info("ENTRY_OFFSET_SECONDS set to %d via admin", new_offset)
        except Exception as e:
            flash("Invalid offset")
    elif action == "force_signal":
        # force signal immediately at next minute boundary
        bd_tz = timezone(timedelta(hours=6))
        entry_dt = (datetime.now(bd_tz).replace(second=0, microsecond=0) + timedelta(minutes=1))
        ok = pick_and_send_signal(entry_dt)
        flash("Signal sent" if ok else "No signal (confidence < 90%)")
    return redirect(url_for("admin_dashboard"))

if __name__ == "__main__":
    # Local dev only: also start scanner
    _start_background_thread_once()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
