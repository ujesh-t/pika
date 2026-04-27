import sqlite3
import json
import os
from datetime import datetime, date, timedelta
from typing import Optional

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            trigger_price REAL,
            entry_price REAL,
            exit_price REAL,
            quantity INTEGER NOT NULL DEFAULT 0,
            side TEXT NOT NULL DEFAULT 'BUY',
            status TEXT NOT NULL DEFAULT 'PENDING',
            pnl REAL DEFAULT 0,
            sl_price REAL,
            tp_price REAL,
            entry_order_id TEXT,
            sl_order_id TEXT,
            tp_order_id TEXT,
            exit_order_id TEXT,
            signal_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            entry_time TIMESTAMP,
            exit_time TIMESTAMP,
            exit_reason TEXT,
            raw_signal TEXT
        );
        CREATE TABLE IF NOT EXISTS trade_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id INTEGER,
            event_type TEXT NOT NULL,
            message TEXT,
            price REAL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (trade_id) REFERENCES trades(id)
        );
    """)
    conn.commit()
    conn.close()


def log_event(trade_id: Optional[int], event_type: str, message: str, price: Optional[float] = None):
    conn = get_db()
    conn.execute(
        "INSERT INTO trade_logs (trade_id, event_type, message, price) VALUES (?, ?, ?, ?)",
        (trade_id, event_type, message, price),
    )
    conn.commit()
    conn.close()


def create_trade(symbol: str, trigger_price: float, sl_price: float, tp_price: float,
                 quantity: int, side: str = "BUY", raw_signal: str = "") -> Optional[int]:
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO trades (symbol, trigger_price, sl_price, tp_price, quantity, side, status, raw_signal)
           VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?)""",
        (symbol, trigger_price, sl_price, tp_price, quantity, side, raw_signal),
    )
    trade_id = cur.lastrowid
    conn.commit()
    conn.close()
    log_event(trade_id, "SIGNAL_RECEIVED",
              f"Signal: {symbol} {side} Trigger={trigger_price} SL={sl_price} TP={tp_price} Qty={quantity}")
    return trade_id


def update_trade_entry(trade_id: int, entry_price: float, quantity: int, entry_order_id: str):
    conn = get_db()
    conn.execute(
        """UPDATE trades SET status='OPEN', entry_price=?, quantity=?, entry_order_id=?,
           entry_time=CURRENT_TIMESTAMP WHERE id=?""",
        (entry_price, quantity, entry_order_id, trade_id),
    )
    conn.commit()
    conn.close()
    log_event(trade_id, "ENTRY_EXECUTED", f"Entry at {entry_price} Qty={quantity}", entry_price)


def update_trade_exit(trade_id: int, exit_price: float, exit_order_id: str, exit_reason: str):
    conn = get_db()
    trade = get_trade(trade_id)
    if not trade:
        return
    quantity = trade["quantity"]
    entry_price = trade["entry_price"] or 0
    side = trade["side"]
    if side == "BUY":
        pnl = round((exit_price - entry_price) * quantity, 2)
    else:
        pnl = round((entry_price - exit_price) * quantity, 2)
    conn.execute(
        """UPDATE trades SET status='CLOSED', exit_price=?, exit_order_id=?, exit_reason=?,
           pnl=?, exit_time=CURRENT_TIMESTAMP WHERE id=?""",
        (exit_price, exit_order_id, exit_reason, pnl, trade_id),
    )
    conn.commit()
    conn.close()
    log_event(trade_id, "EXIT_EXECUTED", f"Exit at {exit_price} Reason={exit_reason} PnL={pnl}", exit_price)


def cancel_trade(trade_id: int):
    conn = get_db()
    conn.execute("UPDATE trades SET status='CANCELLED' WHERE id=?", (trade_id,))
    conn.commit()
    conn.close()
    log_event(trade_id, "TRADE_CANCELLED", "Trade cancelled")


def get_trade(trade_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_active_trade():
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM trades WHERE status IN ('PENDING', 'OPEN') ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_pending_trades():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM trades WHERE status='PENDING' ORDER BY signal_time DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_open_trades():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM trades WHERE status='OPEN' ORDER BY entry_time DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_trades(limit: int = 50):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM trades ORDER BY signal_time DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_recent_logs(limit: int = 50):
    conn = get_db()
    rows = conn.execute(
        """SELECT tl.*, t.symbol FROM trade_logs tl
           LEFT JOIN trades t ON tl.trade_id = t.id
           ORDER BY tl.timestamp DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_pnl_daily():
    conn = get_db()
    today = date.today().isoformat()
    rows = conn.execute(
        "SELECT pnl FROM trades WHERE status='CLOSED' AND date(exit_time)=?",
        (today,),
    ).fetchall()
    conn.close()
    pnl = sum(r["pnl"] for r in rows if r["pnl"] is not None)
    count = len(rows)
    wins = sum(1 for r in rows if r["pnl"] is not None and r["pnl"] > 0)
    return {"pnl": round(pnl, 2), "total": count, "wins": wins}


def get_pnl_weekly():
    conn = get_db()
    monday = (date.today() - timedelta(days=date.today().weekday())).isoformat()
    rows = conn.execute(
        "SELECT pnl FROM trades WHERE status='CLOSED' AND date(exit_time)>=?",
        (monday,),
    ).fetchall()
    conn.close()
    pnl = sum(r["pnl"] for r in rows if r["pnl"] is not None)
    return {"pnl": round(pnl, 2), "total": len(rows)}


def get_pnl_monthly():
    conn = get_db()
    first = date.today().replace(day=1).isoformat()
    rows = conn.execute(
        "SELECT pnl FROM trades WHERE status='CLOSED' AND date(exit_time)>=?",
        (first,),
    ).fetchall()
    conn.close()
    pnl = sum(r["pnl"] for r in rows if r["pnl"] is not None)
    return {"pnl": round(pnl, 2), "total": len(rows)}


def get_pnl_total():
    conn = get_db()
    rows = conn.execute(
        "SELECT pnl FROM trades WHERE status='CLOSED'"
    ).fetchall()
    conn.close()
    all_pnls = [r["pnl"] for r in rows if r["pnl"] is not None]
    total_pnl = sum(all_pnls)
    total = len(all_pnls)
    wins = sum(1 for p in all_pnls if p > 0)
    losses = sum(1 for p in all_pnls if p < 0)
    win_rate = round((wins / total * 100), 1) if total > 0 else 0
    return {
        "pnl": round(total_pnl, 2),
        "total": total,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
    }


def save_token(access_token: str):
    token_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fyers_token.json")
    data = {
        "access_token": access_token,
        "created_at": datetime.now().isoformat(),
        "expires_at": (datetime.now() + timedelta(hours=24)).isoformat(),
    }
    with open(token_file, "w") as f:
        json.dump(data, f)


def load_token() -> Optional[str]:
    token_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fyers_token.json")
    if not os.path.exists(token_file):
        return None
    try:
        with open(token_file, "r") as f:
            data = json.load(f)
        expires_at = datetime.fromisoformat(data["expires_at"])
        if datetime.now() > expires_at:
            return None
        return data["access_token"]
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def get_token_status() -> dict:
    token_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fyers_token.json")
    if not os.path.exists(token_file):
        return {"valid": False, "message": "No token found"}
    try:
        with open(token_file, "r") as f:
            data = json.load(f)
        expires_at = datetime.fromisoformat(data["expires_at"])
        now = datetime.now()
        if now > expires_at:
            return {"valid": False, "message": "Token expired"}
        remaining = (expires_at - now).total_seconds()
        hours = int(remaining // 3600)
        minutes = int((remaining % 3600) // 60)
        return {"valid": True, "message": f"Expires in {hours}h {minutes}m", "hours": hours, "minutes": minutes}
    except Exception:
        return {"valid": False, "message": "Invalid token file"}
