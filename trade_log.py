"""
trade_log.py — Shared trade logger
Writes every open/close event to trade_log.json.
Thread-safe. All other modules import this.
"""

import json
import uuid
import threading
from datetime import datetime, timezone

TRADE_LOG_FILE = "trade_log.json"

_lock = threading.Lock()


def _load():
    try:
        with open(TRADE_LOG_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _save(trades):
    with open(TRADE_LOG_FILE, "w") as f:
        json.dump(trades, f, indent=2, default=str)


def open_trade(stream, symbol, side, entry_price, quantity,
               stop_loss=None, take_profit=None, meta=None):
    """Record a new open trade. Returns trade_id."""
    trade_id = str(uuid.uuid4())[:8]
    trade = {
        "id":           trade_id,
        "stream":       stream,
        "symbol":       symbol,
        "side":         side,
        "entry_price":  round(float(entry_price), 6),
        "quantity":     round(float(quantity), 6),
        "stop_loss":    stop_loss,
        "take_profit":  take_profit,
        "entry_time":   datetime.now(timezone.utc).isoformat(),
        "exit_price":   None,
        "exit_time":    None,
        "pnl":          None,
        "status":       "open",
        "meta":         meta or {},
    }
    with _lock:
        trades = _load()
        trades.append(trade)
        _save(trades)
    return trade_id


def close_trade(trade_id, exit_price):
    """Close a trade by id. Returns realised P&L."""
    with _lock:
        trades = _load()
        for t in trades:
            if t["id"] == trade_id and t["status"] == "open":
                direction      = 1 if t["side"].lower() == "buy" else -1
                pnl            = round(direction * (float(exit_price) - t["entry_price"]) * t["quantity"], 4)
                t["exit_price"] = round(float(exit_price), 6)
                t["exit_time"]  = datetime.now(timezone.utc).isoformat()
                t["pnl"]        = pnl
                t["status"]     = "closed"
                _save(trades)
                return pnl
    return 0.0


def close_by_symbol(stream, symbol, exit_price):
    """Close the most recent open trade for a stream+symbol. Returns P&L."""
    with _lock:
        trades = _load()
        for t in reversed(trades):
            if t["symbol"] == symbol and t["stream"] == stream and t["status"] == "open":
                direction       = 1 if t["side"].lower() == "buy" else -1
                pnl             = round(direction * (float(exit_price) - t["entry_price"]) * t["quantity"], 4)
                t["exit_price"] = round(float(exit_price), 6)
                t["exit_time"]  = datetime.now(timezone.utc).isoformat()
                t["pnl"]        = pnl
                t["status"]     = "closed"
                _save(trades)
                return pnl
    return 0.0


def get_open(stream=None, symbol=None):
    """Return open trades, optionally filtered."""
    trades = _load()
    result = [t for t in trades if t["status"] == "open"]
    if stream:
        result = [t for t in result if t["stream"] == stream]
    if symbol:
        result = [t for t in result if t["symbol"] == symbol]
    return result


def get_all():
    return _load()
