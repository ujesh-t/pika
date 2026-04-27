import os
import re
import json
import threading
import time
import logging
import hashlib
from datetime import datetime
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, jsonify, abort
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from dotenv import load_dotenv

from trade_manager import (
    init_db, create_trade, update_trade_entry, update_trade_exit,
    cancel_trade, get_trade, get_active_trade, get_pending_trades, get_open_trades,
    get_all_trades, get_recent_logs, log_event, save_token, load_token,
    get_token_status, get_pnl_daily, get_pnl_weekly, get_pnl_monthly, get_pnl_total,
)

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

secret_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".flask_secret")
if os.path.exists(secret_file):
    with open(secret_file, "r") as f:
        app.secret_key = f.read().strip()
else:
    app.secret_key = hashlib.sha256(os.urandom(64)).hexdigest()
    with open(secret_file, "w") as f:
        f.write(app.secret_key)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

USERS = {"admin": "admin123"}


class User(UserMixin):
    def __init__(self, username):
        self.id = username


@login_manager.unauthorized_handler
def unauthorized():
    return redirect(url_for("login"))


@login_manager.user_loader
def load_user(username):
    if username in USERS:
        return User(username)
    return None


init_db()

# --- Bot State ---
bot_thread = None
bot_running = False
bot_stop_event = threading.Event()
fyers_instance = None
telegram_client = None


# --- Fyers Helper ---
def get_fyers():
    global fyers_instance
    token = load_token()
    if not token:
        return None
    try:
        from fyers_apiv3 import fyersModel
        client_id = os.getenv("FYERS_CLIENT_ID")
        fyers_instance = fyersModel.FyersModel(client_id=client_id, token=token, log_path="")
        return fyers_instance
    except Exception as e:
        logger.error(f"Fyers init error: {e}")
        return None


def get_symbol_map():
    return {
        "NIFTY": "NSE:NIFTY50-INDEX",
        "BANKNIFTY": "NSE:BANKNIFTY-INDEX",
        "FINNIFTY": "NSE:FINNIFTY-INDEX",
        "SENSEX": "BSE:SENSEX-INDEX",
    }


def parse_signal(message: str) -> dict:
    lines = [l.strip() for l in message.strip().split("\n") if l.strip()]
    result = {}
    symbol_line = lines[0] if lines else ""
    expiry_match = re.search(r"(\d{2})\s*(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)", symbol_line, re.I)
    strike_match = re.search(r"(\d{5,})", symbol_line)
    ce_pe_match = re.search(r"\b(CE|PE)\b", symbol_line, re.I)
    index_match = re.search(r"(NIFTY|BANKNIFTY|FINNIFTY|SENSEX)", symbol_line, re.I)

    index_name = index_match.group(1).upper() if index_match else "NIFTY"
    strike = strike_match.group(1) if strike_match else ""
    option_type = ce_pe_match.group(1).upper() if ce_pe_match else "CE"

    for line in lines[1:]:
        lower = line.upper()
        if "ABOVE" in lower or "BUY" in lower or "ENTRY" in lower:
            nums = re.findall(r"[\d.]+", line)
            if nums:
                result["trigger_price"] = float(nums[0])
        elif "SL" in lower or "STOPLOSS" in lower:
            nums = re.findall(r"[\d.]+", line)
            if nums:
                result["sl_price"] = float(nums[0])
        elif "TARGET" in lower or "TP" in lower:
            nums = re.findall(r"[\d.]+", line)
            if nums:
                result["tp_price"] = float(nums[-1])

    result["symbol"] = f"{index_name}{strike}{option_type}"
    result["side"] = "BUY"
    result.setdefault("trigger_price", 0)
    result.setdefault("sl_price", 0)
    result.setdefault("tp_price", 0)
    return result


def lookup_lot_size(symbol: str) -> int:
    if "BANKNIFTY" in symbol.upper():
        return 30
    return 65


# --- Bot Core Logic ---
def bot_worker():
    global bot_running
    logger.info("Bot worker started")
    log_event(None, "BOT_STARTED", "Trading bot started")
    bot_running = True

    import asyncio
    from telethon import TelegramClient

    async def run_telegram():
        global bot_running, telegram_client
        api_id = int(os.getenv("TG_API_ID", 0))
        api_hash = os.getenv("TG_API_HASH", "")
        target_chat = int(os.getenv("TARGET_CHAT_ID", 0))

        telegram_client = TelegramClient("coders_bot_session", api_id, api_hash)
        await telegram_client.start()

        me = await telegram_client.get_me()
        logger.info(f"Telegram logged in as: {me.username or me.first_name}")
        log_event(None, "TELEGRAM_CONNECTED", f"Connected as {me.username or me.first_name}")

        async for message in telegram_client.iter_messages(target_chat, limit=10):
            if bot_stop_event.is_set():
                break
            if not message.text:
                continue
            process_telegram_message(message.text)
            await asyncio.sleep(0.5)

        if not bot_stop_event.is_set():
            @telegram_client.on(events.NewMessage(chats=target_chat))
            async def handler(event):
                if bot_stop_event.is_set():
                    return
                if event.text:
                    process_telegram_message(event.text)

            logger.info("Listening for new Telegram messages...")
            await telegram_client.run_until_disconnected()

        await telegram_client.disconnect()
        telegram_client = None
        bot_running = False
        logger.info("Bot worker stopped")
        log_event(None, "BOT_STOPPED", "Trading bot stopped")

    try:
        from telethon import events
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(run_telegram())
    except Exception as e:
        logger.error(f"Bot worker error: {e}")
        log_event(None, "ERROR", f"Bot error: {e}")
        bot_running = False


def process_telegram_message(text: str):
    try:
        signal = parse_signal(text)
        if not signal.get("trigger_price") or signal["trigger_price"] == 0:
            logger.warning(f"Incomplete signal: {text[:50]}")
            return

        active = get_active_trade()
        if active:
            logger.info("Active trade exists, ignoring new signal")
            return

        lot_size = lookup_lot_size(signal["symbol"])
        trade_id = create_trade(
            symbol=signal["symbol"],
            trigger_price=signal["trigger_price"],
            sl_price=signal["sl_price"],
            tp_price=signal["tp_price"],
            quantity=lot_size,
            side=signal["side"],
            raw_signal=text,
        )
        logger.info(f"Trade #{trade_id} created: {signal['symbol']} @ {signal['trigger_price']}")

        threading.Thread(target=monitor_entry, args=(trade_id,), daemon=True).start()
    except Exception as e:
        logger.error(f"Signal processing error: {e}")
        log_event(None, "ERROR", f"Signal processing error: {e}")


def monitor_entry(trade_id: int):
    trade = get_trade(trade_id)
    if not trade:
        return
    trigger = trade["trigger_price"]
    symbol = trade["symbol"]
    logger.info(f"Monitoring entry for {symbol} at {trigger}")

    fyers = get_fyers()
    if not fyers:
        logger.error("No Fyers token available for entry monitoring")
        return

    while not bot_stop_event.is_set():
        try:
            price = get_ltp(fyers, symbol)
            if price and price >= trigger:
                place_entry_order(trade_id, fyers)
                return
        except Exception as e:
            logger.error(f"Entry monitor error: {e}")
        time.sleep(2)

    cancel_trade(trade_id)
    logger.info(f"Trade #{trade_id} cancelled (bot stopped)")


def get_ltp(fyers, symbol: str) -> float:
    try:
        quotes = fyers.quotes({"symbols": symbol})
        if quotes.get("s") == "ok" and quotes.get("d"):
            return float(quotes["d"][0]["v"]["lp"])
    except Exception:
        pass
    try:
        data = {"symbols": symbol}
        resp = fyers.quotes(data)
        if resp.get("s") == "ok":
            for item in resp.get("d", []):
                return float(item["v"]["lp"])
    except Exception:
        pass
    return 0.0


def place_entry_order(trade_id: int, fyers):
    trade = get_trade(trade_id)
    if not trade:
        return
    symbol = trade["symbol"]
    qty = trade["quantity"]

    try:
        order = fyers.place_order({
            "symbol": symbol,
            "qty": qty,
            "type": 2,
            "side": 1,
            "productType": "INTRADAY",
            "limitPrice": 0,
            "stopPrice": 0,
            "validity": "DAY",
            "offlineOrder": False,
        })
        if order.get("s") == "ok":
            order_id = order.get("id", "")
            price = get_ltp(fyers, symbol)
            update_trade_entry(trade_id, price, qty, order_id)
            logger.info(f"Entry filled for #{trade_id} at {price}")
            threading.Thread(target=monitor_exit, args=(trade_id, fyers), daemon=True).start()
        else:
            logger.error(f"Entry order failed: {order}")
            log_event(trade_id, "ERROR", f"Entry order failed: {order}")
    except Exception as e:
        logger.error(f"Entry order error: {e}")
        log_event(trade_id, "ERROR", f"Entry order error: {e}")


def monitor_exit(trade_id: int, fyers):
    trade = get_trade(trade_id)
    if not trade:
        return
    sl_price = trade["sl_price"]
    tp_price = trade["tp_price"]
    symbol = trade["symbol"]
    logger.info(f"Monitoring exit for {symbol} SL={sl_price} TP={tp_price}")

    while not bot_stop_event.is_set():
        try:
            price = get_ltp(fyers, symbol)
            if not price:
                time.sleep(2)
                continue
            if sl_price and price <= sl_price:
                place_exit_order(trade_id, fyers, "SL_HIT")
                return
            if tp_price and price >= tp_price:
                place_exit_order(trade_id, fyers, "TP_HIT")
                return
        except Exception as e:
            logger.error(f"Exit monitor error: {e}")
        time.sleep(2)

    logger.info(f"Monitoring stopped for trade #{trade_id}")


def place_exit_order(trade_id: int, fyers, reason: str):
    trade = get_trade(trade_id)
    if not trade:
        return
    symbol = trade["symbol"]
    qty = trade["quantity"]

    try:
        order = fyers.place_order({
            "symbol": symbol,
            "qty": qty,
            "type": 2,
            "side": -1,
            "productType": "INTRADAY",
            "limitPrice": 0,
            "stopPrice": 0,
            "validity": "DAY",
            "offlineOrder": False,
        })
        if order.get("s") == "ok":
            order_id = order.get("id", "")
            price = get_ltp(fyers, symbol)
            update_trade_exit(trade_id, price, order_id, reason)
            logger.info(f"Exit filled for #{trade_id} at {price} ({reason})")
        else:
            logger.error(f"Exit order failed: {order}")
            log_event(trade_id, "ERROR", f"Exit order failed: {order}")
    except Exception as e:
        logger.error(f"Exit order error: {e}")
        log_event(trade_id, "ERROR", f"Exit order error: {e}")


# --- Flask Routes ---
@app.route("/")
def index():
    if not current_user.is_authenticated:
        return redirect(url_for("login"))
    return render_template("dashboard.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username in USERS and USERS[username] == password:
            login_user(User(username))
            return redirect(url_for("index"))
        error = "Invalid credentials"
    return render_template("login.html", error=error)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/reports")
@login_required
def reports():
    return render_template("dashboard.html")


@app.route("/orders-positions")
@login_required
def orders_positions():
    return render_template("orders_positions.html")


@app.route("/generate-token")
@login_required
def token_page():
    return render_template("token.html")


# --- API Routes ---
@app.route("/api/bot-status")
def api_bot_status():
    return jsonify({"running": bot_running})


@app.route("/api/start-bot")
@login_required
def api_start_bot():
    global bot_thread, bot_running, bot_stop_event
    if bot_running:
        return jsonify({"status": "ok", "message": "Bot already running"})
    if not load_token():
        return jsonify({"status": "error", "message": "No valid Fyers token. Generate one first."})
    bot_stop_event.clear()
    bot_thread = threading.Thread(target=bot_worker, daemon=True)
    bot_thread.start()
    return jsonify({"status": "ok", "message": "Bot started"})


@app.route("/api/stop-bot")
@login_required
def api_stop_bot():
    global bot_running, bot_stop_event
    bot_stop_event.set()
    import asyncio
    global telegram_client
    if telegram_client:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(telegram_client.disconnect())
        except Exception:
            pass
        telegram_client = None
    bot_running = False
    log_event(None, "BOT_STOPPED", "Bot stopped via API")
    return jsonify({"status": "ok", "message": "Bot stopped"})


@app.route("/api/token-status")
def api_token_status():
    return jsonify(get_token_status())


@app.route("/api/generate-token-auth")
@login_required
def api_generate_token_auth():
    client_id = os.getenv("FYERS_CLIENT_ID")
    redirect_uri = os.getenv("FYERS_REDIRECT_URI")
    if not client_id or not redirect_uri:
        return jsonify({"status": "error", "message": "Fyers credentials not configured"})
    auth_url = (
        f"https://api-t1.fyers.in/api/v3/generate-authcode"
        f"?client_id={client_id}&redirect_uri={redirect_uri}&response_type=code&state=token_gen"
    )
    return jsonify({"status": "ok", "auth_url": auth_url})


@app.route("/api/save-token", methods=["POST"])
@login_required
def api_save_token():
    data = request.get_json()
    access_token = data.get("access_token", "")
    auth_code = data.get("auth_code", "")

    if access_token:
        save_token(access_token)
        return jsonify({"status": "ok", "message": "Token saved"})

    if auth_code:
        try:
            import requests
            import hashlib
            client_id = os.getenv("FYERS_CLIENT_ID")
            secret_key = os.getenv("FYERS_SECRET_KEY")
            app_id_hash = hashlib.sha256(f"{client_id}:{secret_key}".encode()).hexdigest()
            resp = requests.post("https://api-t1.fyers.in/api/v3/validate-authcode", json={
                "grant_type": "authorization_code",
                "appIdHash": app_id_hash,
                "code": auth_code,
            })
            result = resp.json()
            if result.get("s") == "ok" and result.get("access_token"):
                save_token(result["access_token"])
                return jsonify({"status": "ok", "message": "Token generated and saved"})
            return jsonify({"status": "error", "message": str(result)})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    return jsonify({"status": "error", "message": "No access_token or auth_code provided"})


@app.route("/api/trades")
@login_required
def api_trades():
    trades = get_all_trades(50)
    return jsonify(trades)


@app.route("/api/open-trades")
@login_required
def api_open_trades():
    trades = get_open_trades()
    fyers = get_fyers()
    if fyers:
        for t in trades:
            try:
                price = get_ltp(fyers, t["symbol"])
                if t["entry_price"] and price:
                    t["current_price"] = price
                    qty = t["quantity"] or 1
                    if t["side"] == "BUY":
                        t["unrealized_pnl"] = round((price - t["entry_price"]) * qty, 2)
                    else:
                        t["unrealized_pnl"] = round((t["entry_price"] - price) * qty, 2)
            except Exception:
                pass
    return jsonify(trades)


@app.route("/api/pending-signals")
@login_required
def api_pending_signals():
    signals = get_pending_trades()
    return jsonify(signals)


@app.route("/api/subscribed-symbols")
@login_required
def api_subscribed_symbols():
    return jsonify({"symbols": []})


@app.route("/api/orders")
@login_required
def api_orders():
    fyers = get_fyers()
    if not fyers:
        return jsonify([])
    try:
        resp = fyers.order_history({})
        if resp.get("s") == "ok":
            return jsonify(resp.get("orderBook", []))
        return jsonify([])
    except Exception as e:
        logger.error(f"Order history error: {e}")
        return jsonify([])


@app.route("/api/open-orders")
@login_required
def api_open_orders():
    fyers = get_fyers()
    if not fyers:
        return jsonify([])
    try:
        resp = fyers.order_book({})
        if resp.get("s") == "ok":
            orders = resp.get("orderBook", [])
            open_orders = [o for o in orders if o.get("status") in (1, 2, 5)]
            return jsonify(open_orders)
        return jsonify([])
    except Exception as e:
        logger.error(f"Open orders error: {e}")
        return jsonify([])


@app.route("/api/positions")
@login_required
def api_positions():
    fyers = get_fyers()
    if not fyers:
        return jsonify([])
    try:
        resp = fyers.positions({})
        if resp.get("s") == "ok":
            return jsonify(resp.get("netPositions", []))
        return jsonify([])
    except Exception as e:
        logger.error(f"Positions error: {e}")
        return jsonify([])


@app.route("/api/pnl/daily")
@login_required
def api_pnl_daily():
    return jsonify(get_pnl_daily())


@app.route("/api/pnl/weekly")
@login_required
def api_pnl_weekly():
    return jsonify(get_pnl_weekly())


@app.route("/api/pnl/monthly")
@login_required
def api_pnl_monthly():
    return jsonify(get_pnl_monthly())


@app.route("/api/pnl/total")
@login_required
def api_pnl_total():
    return jsonify(get_pnl_total())


@app.route("/api/account-balance")
@login_required
def api_account_balance():
    fyers = get_fyers()
    if not fyers:
        return jsonify({"total_collateral": None, "available_balance": None})
    try:
        resp = fyers.funds({})
        if resp.get("s") == "ok":
            funds = resp.get("fund_limit", [])
            total = None
            available = None
            for f in resp.get("fund_limit", []):
                if f.get("title") == "Total Collateral":
                    total = f.get("equityAmount")
                if f.get("title") == "Available Balance":
                    available = f.get("equityAmount")
            return jsonify({"total_collateral": total, "available_balance": available})
        return jsonify({"total_collateral": None, "available_balance": None})
    except Exception as e:
        logger.error(f"Funds error: {e}")
        return jsonify({"total_collateral": None, "available_balance": None})


@app.route("/api/logs")
@login_required
def api_logs():
    logs = get_recent_logs(30)
    return jsonify(logs)


@app.route("/api/close-position", methods=["POST"])
@login_required
def api_close_position():
    data = request.get_json()
    trade_id = data.get("trade_id")
    if not trade_id:
        return jsonify({"status": "error", "message": "No trade_id"})
    fyers = get_fyers()
    if not fyers:
        return jsonify({"status": "error", "message": "No Fyers connection"})
    trade = get_trade(trade_id)
    if not trade:
        return jsonify({"status": "error", "message": "Trade not found"})
    place_exit_order(trade_id, fyers, "MANUAL_CLOSE")
    return jsonify({"status": "ok"})


@app.route("/api/exit-position", methods=["POST"])
@login_required
def api_exit_position():
    return api_close_position()


@app.route("/api/cancel-order", methods=["POST"])
@login_required
def api_cancel_order():
    data = request.get_json()
    order_id = data.get("order_id")
    if not order_id:
        return jsonify({"status": "error", "message": "No order_id"})
    fyers = get_fyers()
    if not fyers:
        return jsonify({"status": "error", "message": "No Fyers connection"})
    try:
        resp = fyers.cancel_order({"id": order_id})
        return jsonify(resp)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


# --- Fyers OAuth Callback ---
@app.route("/fyers-callback")
def fyers_callback():
    auth_code = request.args.get("auth_code", "")
    if auth_code:
        try:
            import requests
            import hashlib
            client_id = os.getenv("FYERS_CLIENT_ID")
            secret_key = os.getenv("FYERS_SECRET_KEY")
            app_id_hash = hashlib.sha256(f"{client_id}:{secret_key}".encode()).hexdigest()
            resp = requests.post("https://api-t1.fyers.in/api/v3/validate-authcode", json={
                "grant_type": "authorization_code",
                "appIdHash": app_id_hash,
                "code": auth_code,
            })
            result = resp.json()
            if result.get("s") == "ok" and result.get("access_token"):
                save_token(result["access_token"])
                return redirect(url_for("token_page"))
        except Exception as e:
            logger.error(f"Callback error: {e}")
    return redirect(url_for("login"))


@app.errorhandler(403)
def forbidden(e):
    return render_template("login.html", error="Session expired. Please login again."), 403


@app.errorhandler(404)
def not_found(e):
    return "Page not found", 404


@app.errorhandler(500)
def server_error(e):
    logger.error(f"500 error: {e}")
    return "Internal server error. Check logs for details.", 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    logger.info(f"Starting Alexandria Trading Bot on port {port}")
    app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)
