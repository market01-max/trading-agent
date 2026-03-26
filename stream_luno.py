"""
stream_luno.py — Stream 2: Luno (3 strategies on R18,000)

S2a — Momentum Rotation  R3,000
      Rank SOL/ETH/XRP/BTC/USDT by momentum every hour.
      Hold top 2. Park in ZAR when all momentum negative.

S2b — Grid Trading        R9,000
      XRP/ZAR ladder: 8 buy orders below, 8 sell orders above.
      Collect 2% spread on every oscillation.

S2c — ETH Staking         R6,000
      Buy ETH immediately for 11% APY staking yield.
      Buy more on RSI < 35. Take 30% profit on RSI > 70.

All three run as separate threads. A single LunoAPI instance is shared.
"""

import os
import time
import logging
import threading
import requests
from datetime import datetime, timezone

import trade_log

log = logging.getLogger(__name__)

# ── Credentials ───────────────────────────────────────────────
LUNO_KEY    = os.environ.get("LUNO_API_KEY", "")
LUNO_SECRET = os.environ.get("LUNO_API_SECRET", "")
LUNO_BASE   = "https://api.luno.com/api/1"
IS_LIVE     = bool(LUNO_KEY and LUNO_SECRET)

# ── Capital split ─────────────────────────────────────────────
CAP_ROTATION = 3000.0
CAP_GRID     = 9000.0
CAP_ETH      = 6000.0
DAILY_LOSS   = 0.05   # 5% daily loss → pause that strategy

# ── Strategy 2a config ────────────────────────────────────────
ROTATION_PAIRS    = ["SOL/ZAR", "ETH/ZAR", "XRP/ZAR", "BTC/ZAR", "USDT/ZAR"]
ROTATION_TOP_N    = 2
ROTATION_INTERVAL = 3600

# ── Strategy 2b config ────────────────────────────────────────
GRID_PAIR     = "XRPZAR"
GRID_LEVELS   = 8
GRID_SPACING  = 0.02     # 2% per level
GRID_ORDER    = 150.0    # R150 per order
GRID_INTERVAL = 300      # check every 5 min

# ── Strategy 2c config ────────────────────────────────────────
ETH_PAIR          = "ETHZAR"
ETH_RSI_BUY       = 35
ETH_RSI_SELL      = 70
ETH_MAX_TRADE     = 600.0
ETH_STAKING_APY   = 0.11
ETH_INTERVAL      = 3600


# ════════════════════════════════════════════════════════
#  LUNO API CLIENT
# ════════════════════════════════════════════════════════

class LunoAPI:
    def __init__(self):
        self.auth = (LUNO_KEY, LUNO_SECRET)

    def _get(self, path, params=None):
        if not IS_LIVE:
            return {}
        try:
            r = requests.get(f"{LUNO_BASE}{path}",
                             auth=self.auth, params=params or {}, timeout=8)
            return r.json() if r.status_code == 200 else {}
        except Exception as e:
            log.debug(f"Luno GET {path}: {e}")
            return {}

    def _post(self, path, data):
        if not IS_LIVE:
            return {}
        try:
            r = requests.post(f"{LUNO_BASE}{path}",
                              auth=self.auth, data=data, timeout=8)
            return r.json() if r.status_code == 200 else {}
        except Exception as e:
            log.debug(f"Luno POST {path}: {e}")
            return {}

    def balances(self):
        result = {}
        for b in self._get("/balance").get("balance", []):
            result[b["asset"]] = {
                "balance":   float(b.get("balance", 0)),
                "available": float(b.get("balance", 0)) - float(b.get("reserved", 0)),
            }
        return result

    def ticker(self, pair):
        return self._get("/ticker", {"pair": pair})

    def candles(self, pair, duration=3600, limit=30):
        data = self._get("/candles", {"pair": pair, "duration": duration})
        return (data.get("candles") or [])[-limit:]

    def open_orders(self, pair=""):
        params = {"state": "PENDING"}
        if pair:
            params["pair"] = pair
        return self._get("/listorders", params).get("orders") or []

    def market_buy(self, pair, zar_amount):
        return self._post("/marketorder", {
            "pair": pair, "type": "BUY",
            "counter_volume": str(round(zar_amount, 2))
        })

    def market_sell(self, pair, base_qty):
        return self._post("/marketorder", {
            "pair": pair, "type": "SELL",
            "base_volume": str(round(base_qty, 8))
        })

    def limit_buy(self, pair, qty, price):
        return self._post("/postorder", {
            "pair": pair, "type": "BID",
            "volume": str(round(qty, 8)),
            "price":  str(round(price, 4))
        })

    def limit_sell(self, pair, qty, price):
        return self._post("/postorder", {
            "pair": pair, "type": "ASK",
            "volume": str(round(qty, 8)),
            "price":  str(round(price, 4))
        })

    def cancel_order(self, order_id):
        return self._post("/stoporder", {"order_id": order_id})

    def cancel_all(self, pair=""):
        for o in self.open_orders(pair):
            self.cancel_order(o.get("order_id", ""))

    def last_price(self, pair):
        try:
            return float(self.ticker(pair).get("last_trade", 0))
        except Exception:
            return 0.0


# ── Indicator helpers ─────────────────────────────────────────

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [d for d in deltas[-period:] if d > 0]
    losses = [-d for d in deltas[-period:] if d < 0]
    avg_g  = sum(gains)  / period if gains  else 1e-9
    avg_l  = sum(losses) / period if losses else 1e-9
    return round(100 - 100 / (1 + avg_g / avg_l), 1)

def calc_momentum(closes, lookback=10):
    if len(closes) < lookback + 1:
        return 0.0
    return round((closes[-1] - closes[-(lookback+1)]) / closes[-(lookback+1)] * 100, 4)


# ════════════════════════════════════════════════════════
#  STRATEGY 2a — MOMENTUM ROTATION
# ════════════════════════════════════════════════════════

def run_rotation(api: LunoAPI):
    log.info("🔄 [Rotation] S2a starting — R3,000 on SOL/ETH/XRP/BTC/USDT")
    start_zar  = CAP_ROTATION
    daily_loss = 0.0
    last_reset = datetime.now(timezone.utc).date()

    while True:
        try:
            today = datetime.now(timezone.utc).date()
            if today != last_reset:
                daily_loss = 0.0
                last_reset = today

            if daily_loss >= start_zar * DAILY_LOSS:
                log.warning("🛑 [Rotation] Daily loss limit — pausing")
                time.sleep(ROTATION_INTERVAL)
                continue

            # Score all pairs by momentum
            scores = []
            for pair in ROTATION_PAIRS:
                lp   = pair.replace("/", "")
                cans = api.candles(lp, duration=3600, limit=25)
                if not cans:
                    continue
                closes = [float(c["close"]) for c in cans if c.get("close")]
                if len(closes) < 12:
                    continue
                mom   = calc_momentum(closes, 10)
                price = api.last_price(lp)
                scores.append({"pair": pair, "lp": lp, "mom": mom, "price": price})

            scores.sort(key=lambda x: x["mom"], reverse=True)
            log.info("🔄 [Rotation] " + " | ".join(
                f"{s['pair']} {s['mom']:+.2f}%" for s in scores))

            positive = [s for s in scores if s["mom"] > 0]
            targets  = [s["pair"] for s in positive[:ROTATION_TOP_N]]

            if not targets:
                log.info("🔄 [Rotation] All momentum negative — parking in ZAR")
                bals = api.balances()
                for s in scores:
                    asset = s["pair"].split("/")[0]
                    qty   = bals.get(asset, {}).get("available", 0)
                    if qty > 0.0001:
                        api.market_sell(s["lp"], qty)
            else:
                # Sell anything not in top-N
                bals = api.balances()
                for s in scores:
                    asset = s["pair"].split("/")[0]
                    if s["pair"] not in targets and asset not in ("ZAR", "USDT"):
                        qty = bals.get(asset, {}).get("available", 0)
                        if qty > 0.0001:
                            log.info(f"🔄 [Rotation] Selling {asset}")
                            api.market_sell(s["lp"], qty)
                            trade_log.close_by_symbol("luno", s["pair"], s["price"])

                # Buy top-N if not already held
                alloc = CAP_ROTATION / ROTATION_TOP_N
                bals  = api.balances()
                for s in positive[:ROTATION_TOP_N]:
                    asset = s["pair"].split("/")[0]
                    held  = bals.get(asset, {}).get("available", 0)
                    if held * s["price"] < alloc * 0.8:  # less than 80% allocated
                        log.info(f"🔄 [Rotation] Buying {s['pair']} R{alloc:.0f}")
                        result = api.market_buy(s["lp"], alloc)
                        if result.get("order_id"):
                            trade_log.open_trade("luno", s["pair"], "buy",
                                                 s["price"], alloc / max(s["price"], 0.001),
                                                 meta={"strategy": "rotation"})

        except Exception as e:
            log.error(f"🔄 [Rotation] error: {e}", exc_info=True)

        time.sleep(ROTATION_INTERVAL)


# ════════════════════════════════════════════════════════
#  STRATEGY 2b — GRID TRADING
# ════════════════════════════════════════════════════════

def run_grid(api: LunoAPI):
    log.info(f"📊 [Grid] S2b starting — R9,000 on XRP/ZAR "
             f"({GRID_LEVELS} levels × {GRID_SPACING*100:.0f}%)")
    base_price = None
    daily_loss = 0.0
    last_reset = datetime.now(timezone.utc).date()

    def place_grid(centre):
        nonlocal base_price
        api.cancel_all(GRID_PAIR)
        time.sleep(1)
        qty = GRID_ORDER / centre
        placed = 0
        for i in range(1, GRID_LEVELS + 1):
            buy_px  = round(centre * (1 - GRID_SPACING * i), 4)
            sell_px = round(centre * (1 + GRID_SPACING * i), 4)
            if api.limit_buy(GRID_PAIR, qty, buy_px).get("order_id"):
                placed += 1
            bals = api.balances()
            xrp  = bals.get("XRP", {}).get("available", 0)
            if xrp >= qty:
                if api.limit_sell(GRID_PAIR, qty, sell_px).get("order_id"):
                    placed += 1
        base_price = centre
        log.info(f"📊 [Grid] Grid placed around R{centre:.4f} | {placed} orders")

    while True:
        try:
            today = datetime.now(timezone.utc).date()
            if today != last_reset:
                daily_loss = 0.0
                last_reset = today

            if daily_loss >= CAP_GRID * DAILY_LOSS:
                log.warning("🛑 [Grid] Daily loss limit — pausing")
                time.sleep(GRID_INTERVAL)
                continue

            price = api.last_price(GRID_PAIR)
            if price <= 0:
                time.sleep(GRID_INTERVAL)
                continue

            # Initial grid or re-centre if price moved >10%
            if base_price is None or abs(price - base_price) / base_price > 0.10:
                log.info(f"📊 [Grid] Placing grid around R{price:.4f}")
                place_grid(price)
            else:
                open_n = len(api.open_orders(GRID_PAIR))
                log.info(f"📊 [Grid] XRP @ R{price:.4f} | open orders: {open_n}/{GRID_LEVELS*2}")
                if open_n < GRID_LEVELS:
                    log.info("📊 [Grid] Grid thinning — refreshing")
                    place_grid(price)

        except Exception as e:
            log.error(f"📊 [Grid] error: {e}", exc_info=True)

        time.sleep(GRID_INTERVAL)


# ════════════════════════════════════════════════════════
#  STRATEGY 2c — ETH STAKING + TRADING
# ════════════════════════════════════════════════════════

def run_eth(api: LunoAPI):
    log.info(f"💎 [ETH] S2c starting — R6,000 ETH staking + active trading "
             f"({ETH_STAKING_APY*100:.0f}% APY)")
    staking_start  = datetime.now(timezone.utc)
    last_yield_day = ""
    daily_loss     = 0.0
    last_reset     = datetime.now(timezone.utc).date()

    while True:
        try:
            today = datetime.now(timezone.utc).date()
            if today != last_reset:
                daily_loss = 0.0
                last_reset = today

            if daily_loss >= CAP_ETH * DAILY_LOSS:
                log.warning("🛑 [ETH] Daily loss limit — pausing")
                time.sleep(ETH_INTERVAL)
                continue

            bals  = api.balances()
            price = api.last_price(ETH_PAIR)
            if price <= 0:
                time.sleep(ETH_INTERVAL)
                continue

            eth_held = bals.get("ETH", {}).get("available", 0)
            zar_avail = bals.get("ZAR", {}).get("available", 0)
            equity   = eth_held * price + zar_avail

            # RSI from hourly candles
            cans   = api.candles(ETH_PAIR, duration=3600, limit=30)
            closes = [float(c["close"]) for c in cans if c.get("close")]
            rsi    = calc_rsi(closes) if closes else 50.0

            # Staking yield estimate
            days_held = (datetime.now(timezone.utc) - staking_start).days
            est_yield = round(eth_held * price * ETH_STAKING_APY * days_held / 365, 2)

            log.info(f"💎 [ETH] price=R{price:,.0f} | held={eth_held:.4f} ETH "
                     f"(R{eth_held*price:,.0f}) | RSI={rsi:.0f} | "
                     f"est_yield=R{est_yield:.2f}")

            # Initial buy — deploy 80% of allocation into ETH
            if eth_held < 0.001 and zar_avail >= 50:
                buy_zar = min(CAP_ETH * 0.80, zar_avail)
                log.info(f"💎 [ETH] Initial buy R{buy_zar:.0f} for staking")
                result = api.market_buy(ETH_PAIR, buy_zar)
                if result.get("order_id"):
                    trade_log.open_trade("luno", "ETH/ZAR", "buy", price,
                                         buy_zar / price,
                                         meta={"strategy": "eth_staking"})

            # Dip buy — RSI oversold
            elif rsi < ETH_RSI_BUY and zar_avail >= 50:
                trade_zar = min(ETH_MAX_TRADE, zar_avail * 0.5)
                log.info(f"💎 [ETH] Dip buy R{trade_zar:.0f} (RSI={rsi:.0f})")
                result = api.market_buy(ETH_PAIR, trade_zar)
                if result.get("order_id"):
                    trade_log.open_trade("luno", "ETH/ZAR", "buy", price,
                                         trade_zar / price,
                                         meta={"strategy": "eth_dip"})

            # Take partial profit — RSI overbought
            elif rsi > ETH_RSI_SELL and eth_held > 0.001:
                sell_qty = round(eth_held * 0.30, 6)
                log.info(f"💎 [ETH] Taking 30% profit {sell_qty:.4f} ETH (RSI={rsi:.0f})")
                result = api.market_sell(ETH_PAIR, sell_qty)
                if result.get("order_id"):
                    trade_log.close_by_symbol("luno", "ETH/ZAR", price)

            else:
                log.info(f"💎 [ETH] HOLD | RSI={rsi:.0f} neutral")

            # Log daily staking yield once per day
            today_str = today.isoformat()
            if today_str != last_yield_day and eth_held > 0.001:
                daily_yield = round(eth_held * price * ETH_STAKING_APY / 365, 2)
                if daily_yield > 0:
                    tid = trade_log.open_trade("luno", "ETH/ZAR-Staking", "buy",
                                               price, daily_yield / price,
                                               meta={"type": "staking_yield"})
                    trade_log.close_trade(tid, price)
                    log.info(f"💎 [ETH] Staking yield logged: R{daily_yield:.2f}")
                last_yield_day = today_str

        except Exception as e:
            log.error(f"💎 [ETH] error: {e}", exc_info=True)

        time.sleep(ETH_INTERVAL)


# ════════════════════════════════════════════════════════
#  ENTRY POINT
# ════════════════════════════════════════════════════════

def run():
    if not IS_LIVE:
        log.warning("⚠️  [Luno] No API credentials — dry-run mode")
    else:
        log.info("🇿🇦 [Luno] Stream 2 starting — R18,000 across 3 strategies")

    api = LunoAPI()

    threads = [
        threading.Thread(target=run_rotation, args=(api,), daemon=True, name="S2a-Rotation"),
        threading.Thread(target=run_grid,     args=(api,), daemon=True, name="S2b-Grid"),
        threading.Thread(target=run_eth,      args=(api,), daemon=True, name="S2c-ETH"),
    ]

    for t in threads:
        t.start()
        log.info(f"   ✅ {t.name} started")
