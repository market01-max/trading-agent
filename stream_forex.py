"""
stream_forex.py — Stream 1: Forex (Alpaca paper)

Rule-based RSI + momentum signal engine.
No LLM. No API costs. Deterministic and backtestable.

Entry rules:
  BUY  — RSI < 35 AND 4h momentum > 0 AND spread < 4 pips
  SELL — RSI > 65 AND 4h momentum < 0 AND spread < 4 pips

Exit rules:
  Close BUY  when RSI >= 55
  Close SELL when RSI <= 45

Position sizing:
  Risk 2% of equity per trade
  Stop = 0.15% from entry, target = 0.30% (2:1 R/R)
  USD/ZAR gets 2x allocation (strongest trend)
"""

import os
import time
import logging
import requests
import yfinance as yf
import pandas as pd
from datetime import datetime, timezone, date, timedelta

import trade_log

log = logging.getLogger(__name__)

# ── Alpaca credentials ────────────────────────────────────────
ALPACA_KEY     = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET  = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_BASE    = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ALPACA_HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type":        "application/json",
}

# ── Config ────────────────────────────────────────────────────
PAIRS = [
    "EUR/USD",
    "GBP/USD",
    "USD/JPY",
    "USD/ZAR",
    "USD/ZAR",   # double allocation — strongest trend
    "AUD/USD",
    "USD/CHF",
]
CHECK_INTERVAL  = 300    # 5 minutes
MAX_POSITIONS   = 3
RISK_PER_TRADE  = 0.02   # 2% of equity
STOP_PCT        = 0.0015  # 0.15% stop distance
TARGET_MULT     = 2.0    # 2:1 R/R
MAX_SPREAD_PIPS = 4.0
RSI_BUY         = 35
RSI_SELL        = 65
RSI_EXIT_LONG   = 55
RSI_EXIT_SHORT  = 45

IS_PAPER = "paper" in ALPACA_BASE.lower()


# ── Alpaca helpers ────────────────────────────────────────────

def _get(path):
    try:
        r = requests.get(f"{ALPACA_BASE}{path}", headers=ALPACA_HEADERS, timeout=5)
        return r.json() if r.status_code == 200 else {}
    except Exception as e:
        log.debug(f"Alpaca GET {path}: {e}")
        return {}


def _post(path, body):
    try:
        r = requests.post(f"{ALPACA_BASE}{path}", headers=ALPACA_HEADERS,
                          json=body, timeout=5)
        return r.json()
    except Exception as e:
        log.debug(f"Alpaca POST {path}: {e}")
        return {}


def get_account():
    return _get("/v2/account")


def get_positions():
    return _get("/v2/positions") or []


def place_order(symbol, qty, side, stop_loss, take_profit):
    alpaca_sym = symbol.replace("/", "")
    body = {
        "symbol":        alpaca_sym,
        "qty":           str(round(qty, 2)),
        "side":          side,
        "type":          "market",
        "time_in_force": "gtc",
    }
    result = _post("/v2/orders", body)
    log.info(f"💱 [Forex] ORDER {side.upper()} {symbol} qty={qty:.2f} "
             f"stop={stop_loss} tp={take_profit} → {result.get('id', result)}")
    return result


def close_position(symbol):
    alpaca_sym = symbol.replace("/", "")
    try:
        r = requests.delete(f"{ALPACA_BASE}/v2/positions/{alpaca_sym}",
                            headers=ALPACA_HEADERS, timeout=5)
        log.info(f"💱 [Forex] CLOSED {symbol} → {r.status_code}")
    except Exception as e:
        log.error(f"💱 [Forex] close error {symbol}: {e}")


# ── Market data ───────────────────────────────────────────────

def get_indicators(pair):
    """
    Fetch 30 days of daily data from yfinance and compute:
    RSI(14), 4-bar momentum, spread estimate, current mid price.
    Returns dict or None on failure.
    """
    try:
        base, quote = pair.split("/")
        sym    = f"{base}{quote}=X"
        start  = (date.today() - timedelta(days=45)).isoformat()
        ticker = yf.Ticker(sym)
        hist   = ticker.history(start=start, interval="1d", auto_adjust=True)

        if hist.empty or len(hist) < 16:
            return None

        closes = hist["Close"].tolist()

        # RSI(14)
        deltas  = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        gains   = [d for d in deltas[-14:] if d > 0]
        losses  = [-d for d in deltas[-14:] if d < 0]
        avg_g   = sum(gains) / 14 if gains else 1e-9
        avg_l   = sum(losses) / 14 if losses else 1e-9
        rsi     = round(100 - (100 / (1 + avg_g / avg_l)), 1)

        # 4-bar momentum %
        momentum = round((closes[-1] - closes[-5]) / closes[-5] * 100, 4) if len(closes) >= 5 else 0

        # Spread estimate (typical for major pairs)
        spread_map = {
            "EUR/USD": 0.6, "GBP/USD": 1.0, "USD/JPY": 0.7,
            "AUD/USD": 0.8, "USD/CHF": 0.9, "USD/ZAR": 2.5,
        }
        spread = spread_map.get(pair, 2.0)

        mid = closes[-1]

        return {
            "pair":         pair,
            "mid":          round(mid, 5),
            "rsi":          rsi,
            "momentum_pct": momentum,
            "spread_pips":  spread,
        }
    except Exception as e:
        log.debug(f"[Forex] indicators error {pair}: {e}")
        return None


# ── Signal engine ─────────────────────────────────────────────

def generate_signal(indicators, open_symbols, equity):
    """
    Given indicators for one pair, decide: buy / sell / close / hold.
    Returns dict with action, symbol, qty, stop_loss, take_profit.
    """
    pair     = indicators["pair"]
    mid      = indicators["mid"]
    rsi      = indicators["rsi"]
    momentum = indicators["momentum_pct"]
    spread   = indicators["spread_pips"]

    # ── Exit check for existing positions ────────────────────
    open_pos = trade_log.get_open("forex", pair)
    if open_pos:
        pos  = open_pos[-1]
        side = pos.get("side", "").lower()
        if side == "buy" and rsi >= RSI_EXIT_LONG:
            return {"action": "close", "symbol": pair,
                    "reasoning": f"RSI={rsi} — exit long"}
        if side == "sell" and rsi <= RSI_EXIT_SHORT:
            return {"action": "close", "symbol": pair,
                    "reasoning": f"RSI={rsi} — exit short"}

    # ── Skip if spread too wide or already in this pair ───────
    if spread > MAX_SPREAD_PIPS:
        return None
    if pair in open_symbols:
        return None

    notional = equity * RISK_PER_TRADE
    # USD/ZAR gets 2× (appears twice in PAIRS list)
    if pair == "USD/ZAR":
        notional = min(notional * 2, equity * 0.04)
    notional = round(min(notional, 50.0), 2)  # cap at $50 notional

    stop_dist = mid * STOP_PCT

    # ── BUY signal ─────────────────────────────────────────────
    if rsi < RSI_BUY and momentum > 0:
        return {
            "action":      "buy",
            "symbol":      pair,
            "qty":         notional,
            "stop_loss":   round(mid - stop_dist, 5),
            "take_profit": round(mid + stop_dist * TARGET_MULT, 5),
            "reasoning":   f"RSI={rsi} oversold, mom={momentum:+.3f}%",
            "score":       (RSI_BUY - rsi) * (1 + abs(momentum)),
        }

    # ── SELL signal ────────────────────────────────────────────
    if rsi > RSI_SELL and momentum < 0:
        return {
            "action":      "sell",
            "symbol":      pair,
            "qty":         notional,
            "stop_loss":   round(mid + stop_dist, 5),
            "take_profit": round(mid - stop_dist * TARGET_MULT, 5),
            "reasoning":   f"RSI={rsi} overbought, mom={momentum:+.3f}%",
            "score":       (rsi - RSI_SELL) * (1 + abs(momentum)),
        }

    return None


# ── Main loop ─────────────────────────────────────────────────

def run():
    log.info("💱 [Forex] Stream 1 starting...")
    log.info(f"   Mode: {'PAPER' if IS_PAPER else 'LIVE'}")
    log.info(f"   Pairs: {', '.join(set(PAIRS))}")
    log.info(f"   Rules: RSI<{RSI_BUY}=buy, RSI>{RSI_SELL}=sell, "
             f"stop={STOP_PCT*100:.2f}%, target=2:1 R/R")

    while True:
        try:
            account  = get_account()
            equity   = float(account.get("equity", 1000))
            cash     = float(account.get("cash", 1000))
            positions = get_positions()
            open_syms = {p.get("symbol", "").replace("/", "") for p in positions}

            log.info(f"💱 [Forex] equity=${equity:,.2f} cash=${cash:,.2f} "
                     f"positions={len(positions)}/{MAX_POSITIONS}")

            if len(positions) >= MAX_POSITIONS:
                log.info("💱 [Forex] max positions — skipping entry scan")
                time.sleep(CHECK_INTERVAL)
                continue

            # ── Scan all pairs, collect signals ──────────────
            signals = []
            seen_pairs = set()
            for pair in PAIRS:
                if pair in seen_pairs:
                    continue  # skip duplicate USD/ZAR second scan
                seen_pairs.add(pair)

                ind = get_indicators(pair)
                if not ind:
                    continue

                log.info(f"   {pair}: RSI={ind['rsi']:.0f} "
                         f"mom={ind['momentum_pct']:+.3f}% "
                         f"mid={ind['mid']:.5f}")

                sig = generate_signal(ind, open_syms, equity)
                if not sig:
                    continue

                if sig["action"] == "close":
                    # Execute closes immediately
                    exit_px = ind["mid"]
                    pnl     = trade_log.close_by_symbol("forex", pair, exit_px)
                    log.info(f"💱 [Forex] CLOSE {pair} @ {exit_px} | P&L: {pnl:+.4f}")
                    if not IS_PAPER:
                        close_position(pair)
                else:
                    signals.append(sig)

            # ── Execute best signal only (highest score) ──────
            if signals:
                best = max(signals, key=lambda x: x.get("score", 0))
                log.info(f"💱 [Forex] BEST SIGNAL: {best['action'].upper()} "
                         f"{best['symbol']} | {best['reasoning']}")

                result = place_order(
                    best["symbol"], best["qty"],
                    best["action"],
                    best["stop_loss"], best["take_profit"]
                )

                if result.get("id"):
                    ind = get_indicators(best["symbol"])
                    entry_px = ind["mid"] if ind else best["qty"]
                    trade_log.open_trade(
                        "forex", best["symbol"], best["action"],
                        entry_px, best["qty"],
                        best["stop_loss"], best["take_profit"]
                    )
                elif IS_PAPER:
                    # Paper mode — log signal even if order not executed
                    log.info(f"💱 [Forex] PAPER SIGNAL logged (not executed): "
                             f"{best['action'].upper()} {best['symbol']}")
                    trade_log.open_trade(
                        "forex", best["symbol"], best["action"],
                        best.get("stop_loss", 0), best["qty"],
                        best["stop_loss"], best["take_profit"],
                        meta={"paper": True, "reasoning": best["reasoning"]}
                    )

        except Exception as e:
            log.error(f"💱 [Forex] loop error: {e}", exc_info=True)

        time.sleep(CHECK_INTERVAL)
