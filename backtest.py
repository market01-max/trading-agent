"""
backtest.py — 3-year backtest for both streams
Triggered manually via GET /backtest/run on the dashboard.
Results saved to backtest_results.json.

Stream 1 (Forex): yfinance daily data, RSI+momentum rules
Stream 2 (Luno):  ccxt BTC/USDT data, regime-aware SMA strategy
"""

import json
import warnings
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta, date

warnings.filterwarnings("ignore")

BACKTEST_FILE = "backtest_results.json"

# ── Forex config ──────────────────────────────────────────────
FOREX_PAIRS     = ["EUR/USD", "GBP/USD", "USD/JPY", "USD/ZAR", "AUD/USD", "USD/CHF"]
FOREX_CAPITAL   = 1000.0
FOREX_RISK_PCT  = 0.02
FOREX_STOP_PCT  = 0.0015
FOREX_TARGET    = 2.0
FOREX_RSI_BUY   = 35
FOREX_RSI_SELL  = 65
FOREX_RSI_EX_L  = 55
FOREX_RSI_EX_S  = 45

# ── Luno config ───────────────────────────────────────────────
LUNO_CAPITAL    = 18000.0
LUNO_RISK_PCT   = 0.10
LUNO_MAX_EXP    = 0.70
LUNO_FEE        = 0.001
LUNO_SLIP       = 0.0005
LUNO_SMA_FAST   = 20
LUNO_SMA_SLOW   = 100
LUNO_ATR_MULT   = 1.5
LUNO_RSI_MAX    = 80
LUNO_DCA        = 500.0   # monthly DCA contribution


# ════════════════════════════════════════════════════════
#  FOREX BACKTEST
# ════════════════════════════════════════════════════════

def fetch_forex(pair, days):
    try:
        import yfinance as yf
        base, quote = pair.split("/")
        sym  = f"{base}{quote}=X"
        start = (date.today() - timedelta(days=days + 60)).isoformat()
        hist  = yf.Ticker(sym).history(start=start, interval="1d", auto_adjust=True)
        if hist.empty:
            return pd.DataFrame()
        df = pd.DataFrame({
            "date":  pd.to_datetime(hist.index).tz_localize(None),
            "open":  hist["Open"].values,
            "high":  hist["High"].values,
            "low":   hist["Low"].values,
            "close": hist["Close"].values,
        })
        return df.dropna().reset_index(drop=True)
    except Exception as e:
        print(f"  yfinance error {pair}: {e}")
        return pd.DataFrame()


def backtest_forex(days):
    print("  Backtesting Stream 1 — Forex...")
    cash      = FOREX_CAPITAL
    equity_curve = []
    all_trades   = []
    pair_stats   = {}
    monthly_pnl  = {}

    for pair in FOREX_PAIRS:
        print(f"    {pair}...", end=" ", flush=True)
        df = fetch_forex(pair, days)
        if df.empty or len(df) < 20:
            print("no data")
            continue
        print(f"{len(df)} days")

        closes = df["close"].values
        pnl_total = 0

        # Compute RSI and momentum
        in_trade = None

        for i in range(15, len(df)):
            row  = df.iloc[i]
            price = float(row["close"])

            # RSI(14)
            d     = [closes[j] - closes[j-1] for j in range(i-13, i+1)]
            gains = [x for x in d if x > 0]
            losses= [-x for x in d if x < 0]
            ag    = sum(gains) / 14 if gains else 1e-9
            al    = sum(losses)/ 14 if losses else 1e-9
            rsi   = 100 - 100 / (1 + ag / al)

            # 4-bar momentum
            mom = (closes[i] - closes[i-4]) / closes[i-4] * 100 if i >= 4 else 0

            stop_dist  = price * FOREX_STOP_PCT
            notional   = cash * FOREX_RISK_PCT
            entry_date = str(row["date"].date())

            # Exit check
            if in_trade:
                side = in_trade["side"]
                if (side == "buy" and rsi >= FOREX_RSI_EX_L) or \
                   (side == "sell" and rsi <= FOREX_RSI_EX_S):
                    direction = 1 if side == "buy" else -1
                    pnl = direction * (price - in_trade["entry"]) * in_trade["qty"]
                    pnl_total += pnl
                    mo = entry_date[:7]
                    monthly_pnl[mo] = round(monthly_pnl.get(mo, 0) + pnl, 4)

                    if pair not in pair_stats:
                        pair_stats[pair] = {"trades": 0, "pnl": 0, "wins": 0}
                    pair_stats[pair]["trades"] += 1
                    pair_stats[pair]["pnl"]    += pnl
                    if pnl > 0:
                        pair_stats[pair]["wins"] += 1
                    all_trades.append({"pair": pair, "side": side,
                                       "entry": in_trade["entry"], "exit": price,
                                       "pnl": round(pnl, 6)})
                    in_trade = None
                continue

            # Entry check
            if rsi < FOREX_RSI_BUY and mom > 0 and not in_trade:
                qty = notional / price
                in_trade = {"side": "buy", "entry": price, "qty": qty,
                            "date": entry_date}

            elif rsi > FOREX_RSI_SELL and mom < 0 and not in_trade:
                qty = notional / price
                in_trade = {"side": "sell", "entry": price, "qty": qty,
                            "date": entry_date}

        cash += pnl_total

    # Compute pair win rates
    for p in pair_stats:
        n = pair_stats[p]["trades"]
        pair_stats[p]["pnl"]      = round(pair_stats[p]["pnl"], 4)
        pair_stats[p]["win_rate"] = round(pair_stats[p]["wins"] / n * 100, 1) if n else 0

    total_return = round((cash - FOREX_CAPITAL) / FOREX_CAPITAL * 100, 2)
    monthly_returns = [{"month": k, "return_pct": round(v / FOREX_CAPITAL * 100, 2)}
                       for k, v in sorted(monthly_pnl.items())]

    print(f"  Forex: {len(all_trades)} trades | return={total_return:+.1f}%")
    return {
        "starting_capital":  FOREX_CAPITAL,
        "final_equity":      round(cash, 2),
        "total_return_pct":  total_return,
        "net_profit":        round(cash - FOREX_CAPITAL, 2),
        "total_trades":      len(all_trades),
        "wins":              sum(1 for t in all_trades if t["pnl"] > 0),
        "win_rate":          round(sum(1 for t in all_trades if t["pnl"] > 0) / max(len(all_trades), 1) * 100, 1),
        "max_drawdown_pct":  0.0,
        "pair_stats":        pair_stats,
        "monthly_returns":   monthly_returns,
        "equity_curve":      [],   # simplified for now
    }


# ════════════════════════════════════════════════════════
#  LUNO BACKTEST
# ════════════════════════════════════════════════════════

def fetch_luno(days):
    try:
        import ccxt
        exchange = ccxt.luno()
        since    = exchange.parse8601(
            (datetime.now(timezone.utc) - timedelta(days=days+60)).strftime("%Y-%m-%dT00:00:00Z")
        )
        ohlcv = []
        while True:
            batch = exchange.fetch_ohlcv("BTC/ZAR", "1d", since=since, limit=500)
            if not batch:
                break
            ohlcv += batch
            since  = batch[-1][0] + 86400000
            if len(batch) < 500:
                break

        if not ohlcv:
            raise ValueError("no data from Luno")

        df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"]    = pd.to_datetime(df["ts"], unit="ms")
        df["date"]  = df["ts"].dt.date
        df = df.drop_duplicates("date").sort_values("date").reset_index(drop=True)
        print(f"  Luno: {len(df)} candles loaded")
        return df
    except Exception as e:
        print(f"  Luno data error: {e} — trying Binance BTC/USDT")
        try:
            import ccxt
            exchange = ccxt.binance()
            since    = exchange.parse8601(
                (datetime.now(timezone.utc) - timedelta(days=days+60)).strftime("%Y-%m-%dT00:00:00Z")
            )
            ohlcv = []
            while True:
                batch = exchange.fetch_ohlcv("BTC/USDT", "1d", since=since, limit=500)
                if not batch:
                    break
                ohlcv += batch
                since  = batch[-1][0] + 86400000
                if len(batch) < 500:
                    break
            df = pd.DataFrame(ohlcv, columns=["ts","open","high","low","close","volume"])
            df["ts"]   = pd.to_datetime(df["ts"], unit="ms")
            df["date"] = df["ts"].dt.date
            df = df.drop_duplicates("date").sort_values("date").reset_index(drop=True)
            print(f"  Binance fallback: {len(df)} candles")
            return df
        except Exception as e2:
            print(f"  Both sources failed: {e2}")
            return pd.DataFrame()


def backtest_luno(days):
    print("  Backtesting Stream 2 — Luno (BTC regime-aware)...")
    df = fetch_luno(days)
    if df.empty or len(df) < LUNO_SMA_SLOW + 30:
        print("  Not enough data")
        return {"starting_capital": LUNO_CAPITAL, "final_equity": LUNO_CAPITAL,
                "total_return_pct": 0, "net_profit": 0, "total_trades": 0,
                "wins": 0, "win_rate": 0, "max_drawdown_pct": 0,
                "equity_curve": [], "monthly_returns": []}

    # Indicators
    df["sma_fast"] = df["close"].rolling(LUNO_SMA_FAST).mean()
    df["sma_slow"] = df["close"].rolling(LUNO_SMA_SLOW).mean()
    tr = np.maximum(df["high"]-df["low"],
                    np.maximum(abs(df["high"]-df["close"].shift(1)),
                               abs(df["low"]-df["close"].shift(1))))
    df["atr"] = tr.rolling(14).mean()

    # ADX
    pdm   = df["high"].diff().clip(lower=0)
    ndm   = (-df["low"].diff()).clip(lower=0)
    atr14 = tr.rolling(14).mean()
    pdi   = (pdm.rolling(14).mean() / atr14.replace(0, 1e-9)) * 100
    ndi   = (ndm.rolling(14).mean() / atr14.replace(0, 1e-9)) * 100
    dx    = (abs(pdi - ndi) / (pdi + ndi + 1e-9)) * 100
    df["adx"] = dx.rolling(14).mean()

    # RSI
    delta = df["close"].diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    df["rsi"] = 100 - 100 / (1 + gain / loss.replace(0, 1e-9))

    df.dropna(inplace=True)
    df.reset_index(drop=True, inplace=True)

    cash = LUNO_CAPITAL; btc = 0.0; entry_price = None
    trades = []; equity_curve = []
    peak_eq = cash; max_dd = 0.0
    monthly_pnl = {}; last_month = None
    regime = "sideways"

    for i in range(len(df)):
        row   = df.iloc[i]
        price = float(row["close"])
        atr   = float(row["atr"])
        rsi   = float(row["rsi"])
        adx   = float(row["adx"])
        d     = row["date"]
        mo    = str(d)[:7]

        # Monthly DCA
        m = d.month
        if last_month is None:
            last_month = m
        elif m != last_month:
            cash += LUNO_DCA
            last_month = m

        equity = cash + btc * price
        equity_curve.append({"date": str(d), "equity": round(equity, 2)})
        peak_eq = max(peak_eq, equity)
        dd = (equity - peak_eq) / peak_eq * 100
        max_dd = min(max_dd, dd)

        if i == 0:
            continue

        prev = df.iloc[i-1]

        # Regime
        if adx >= 22 and price > row["sma_slow"] and row["sma_fast"] > row["sma_slow"]:
            regime = "bull"
        elif adx >= 22 and price < row["sma_slow"]:
            regime = "bear"
        else:
            regime = "sideways"

        if regime == "bull":
            # Entry
            if btc == 0 and price > row["sma_fast"] and \
               row["sma_fast"] > float(prev["sma_fast"]) and rsi < LUNO_RSI_MAX:
                stop_d  = LUNO_ATR_MULT * atr
                atr_qty = (cash * LUNO_RISK_PCT) / max(stop_d, 1)
                cap_qty = (equity * LUNO_MAX_EXP) / price
                qty     = min(atr_qty, cap_qty)
                cost    = qty * price * (1 + LUNO_SLIP + LUNO_FEE)
                if cost > cash:
                    qty = cash / (price * (1+LUNO_SLIP) * (1+LUNO_FEE))
                if qty > 0:
                    cash -= qty * price * (1+LUNO_SLIP) * (1+LUNO_FEE)
                    btc   = qty
                    entry_price = price

            # Exit
            elif btc > 0 and entry_price:
                stop = entry_price - LUNO_ATR_MULT * atr
                if price < row["sma_fast"] or price < stop:
                    proceeds = btc * price * (1-LUNO_SLIP) * (1-LUNO_FEE)
                    pnl      = proceeds - btc * entry_price
                    cash    += proceeds
                    monthly_pnl[mo] = round(monthly_pnl.get(mo, 0) + pnl, 2)
                    trades.append({"pnl": round(pnl, 2), "entry": entry_price, "exit": price})
                    btc = 0; entry_price = None

        elif regime == "sideways":
            # Mean reversion
            if btc == 0 and rsi < 32:
                qty  = (cash * 0.25) / (price * (1+LUNO_SLIP) * (1+LUNO_FEE))
                if qty > 0:
                    cash -= qty * price * (1+LUNO_SLIP) * (1+LUNO_FEE)
                    btc   = qty; entry_price = price

            elif btc > 0 and entry_price:
                if rsi > 55 or price < entry_price * 0.97:
                    proceeds = btc * price * (1-LUNO_SLIP) * (1-LUNO_FEE)
                    pnl      = proceeds - btc * entry_price
                    cash    += proceeds
                    trades.append({"pnl": round(pnl, 2)})
                    btc = 0; entry_price = None

        else:  # bear
            if btc > 0 and entry_price:
                proceeds = btc * price * (1-LUNO_SLIP) * (1-LUNO_FEE)
                pnl      = proceeds - btc * entry_price
                cash    += proceeds
                trades.append({"pnl": round(pnl, 2)})
                btc = 0; entry_price = None

    final = cash + btc * float(df.iloc[-1]["close"])
    total_return = round((final - LUNO_CAPITAL) / LUNO_CAPITAL * 100, 2)
    wins = [t for t in trades if t["pnl"] > 0]
    monthly_returns = [{"month": k, "return_pct": round(v / LUNO_CAPITAL * 100, 2)}
                       for k, v in sorted(monthly_pnl.items())]
    print(f"  Luno: {len(trades)} trades | return={total_return:+.1f}%")
    return {
        "starting_capital": LUNO_CAPITAL,
        "final_equity":     round(final, 2),
        "total_return_pct": total_return,
        "net_profit":       round(final - LUNO_CAPITAL, 2),
        "total_trades":     len(trades),
        "wins":             len(wins),
        "win_rate":         round(len(wins) / max(len(trades), 1) * 100, 1),
        "max_drawdown_pct": round(max_dd, 2),
        "equity_curve":     equity_curve,
        "monthly_returns":  monthly_returns,
    }


# ════════════════════════════════════════════════════════
#  COMBINED ENTRY POINT
# ════════════════════════════════════════════════════════

def run_backtest(days=1095):
    print(f"\n{'='*55}")
    print(f"  BACKTEST — {days} days ({days//365} years)")
    print(f"{'='*55}")

    forex  = backtest_forex(days)
    luno   = backtest_luno(days)

    total_start = FOREX_CAPITAL + LUNO_CAPITAL
    total_end   = forex["final_equity"] + luno["final_equity"]
    total_ret   = round((total_end - total_start) / total_start * 100, 2)

    combined = {
        "starting_capital": total_start,
        "final_equity":     round(total_end, 2),
        "total_return_pct": total_ret,
        "net_profit":       round(total_end - total_start, 2),
        "total_trades":     forex["total_trades"] + luno["total_trades"],
    }

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period_days":  days,
        "combined":     combined,
        "forex":        forex,
        "luno":         luno,
    }

    with open(BACKTEST_FILE, "w") as f:
        json.dump(result, f, indent=2)

    print(f"\n{'='*55}")
    print(f"  COMBINED: {total_ret:+.1f}% | ${total_start:,.0f} → ${total_end:,.0f}")
    print(f"  Forex:    {forex['total_return_pct']:+.1f}%")
    print(f"  Luno:     {luno['total_return_pct']:+.1f}%")
    print(f"  Saved → {BACKTEST_FILE}")
    print(f"{'='*55}\n")
    return result


if __name__ == "__main__":
    run_backtest(1095)
