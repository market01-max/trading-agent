"""
dashboard.py — Flask web dashboard

Routes:
  GET  /                  Live dashboard
  GET  /backtest          Backtest results page
  GET  /api/trades        JSON trade log
  GET  /api/stats         JSON computed stats
  GET  /api/luno          JSON live Luno balances (cached)
  GET  /backtest/run      Trigger backtest in background
  GET  /health            Railway health check
"""

import json
import os
import threading
import time as _time
from datetime import datetime, timezone
from flask import Flask, jsonify, Response

import trade_log

app  = Flask(__name__)
PORT = int(os.environ.get("PORT", 8080))

BACKTEST_FILE = "backtest_results.json"

# ─────────────────────────────────────────────────────────────
#  LUNO LIVE BALANCE CACHE
#  Refreshes every 60s in background. Never blocks HTTP thread.
# ─────────────────────────────────────────────────────────────

_luno_cache = {"positions": [], "total_zar": 0, "at": "not yet"}
_luno_lock  = threading.Lock()

def _luno_loop():
    import requests as req
    key    = os.environ.get("LUNO_API_KEY", "")
    secret = os.environ.get("LUNO_API_SECRET", "")
    pairs  = {"ETH": "ETHZAR", "XBT": "XBTZAR", "SOL": "SOLZAR",
               "XRP": "XRPZAR", "DOGE": "DOGEZAR"}
    while True:
        if key and secret:
            try:
                r = req.get("https://api.luno.com/api/1/balance",
                            auth=(key, secret), timeout=8)
                if r.status_code == 200:
                    positions = []
                    for b in r.json().get("balance", []):
                        asset = b["asset"]
                        qty   = float(b.get("balance", 0))
                        if qty <= 0:
                            continue
                        if asset == "ZAR":
                            positions.append({"asset": "ZAR", "qty": round(qty, 2),
                                              "zar": round(qty, 2)})
                            continue
                        pair = pairs.get(asset)
                        if not pair:
                            continue
                        try:
                            t2    = req.get("https://api.luno.com/api/1/ticker",
                                            params={"pair": pair}, timeout=5)
                            price = float(t2.json().get("last_trade", 0))
                            if price > 0:
                                positions.append({
                                    "asset": asset,
                                    "qty":   round(qty, 6),
                                    "price": round(price, 2),
                                    "zar":   round(qty * price, 2),
                                })
                        except Exception:
                            pass
                    total = sum(p["zar"] for p in positions)
                    with _luno_lock:
                        _luno_cache.update({
                            "positions": positions,
                            "total_zar": round(total, 2),
                            "at": datetime.now(timezone.utc).strftime("%H:%M UTC"),
                        })
            except Exception:
                pass
        _time.sleep(60)


# ─────────────────────────────────────────────────────────────
#  STATS
# ─────────────────────────────────────────────────────────────

def compute_stats(trades):
    closed = [t for t in trades if t.get("status") == "closed"]
    open_p = [t for t in trades if t.get("status") == "open"]

    if not closed:
        return {
            "total": 0, "wins": 0, "losses": 0, "win_rate": 0,
            "pnl": 0, "best": 0, "worst": 0,
            "streams": {}, "pairs": {}, "daily": {},
            "open": open_p, "recent": [],
        }

    pnls     = [t.get("pnl", 0) for t in closed]
    wins     = [p for p in pnls if p > 0]
    win_rate = round(len(wins) / len(closed) * 100, 1)

    streams = {}
    for t in closed:
        s = t.get("stream", "?")
        if s not in streams:
            streams[s] = {"n": 0, "pnl": 0, "wins": 0}
        streams[s]["n"]    += 1
        streams[s]["pnl"]  += t.get("pnl", 0)
        if t.get("pnl", 0) > 0:
            streams[s]["wins"] += 1
    for s in streams:
        n = streams[s]["n"]
        streams[s]["pnl"]  = round(streams[s]["pnl"], 2)
        streams[s]["wr"]   = round(streams[s]["wins"] / n * 100, 1) if n else 0

    pairs = {}
    for t in closed:
        p = t.get("symbol", "?")
        if p not in pairs:
            pairs[p] = {"n": 0, "pnl": 0, "wins": 0}
        pairs[p]["n"]   += 1
        pairs[p]["pnl"] += t.get("pnl", 0)
        if t.get("pnl", 0) > 0:
            pairs[p]["wins"] += 1
    for p in pairs:
        n = pairs[p]["n"]
        pairs[p]["pnl"] = round(pairs[p]["pnl"], 2)
        pairs[p]["wr"]  = round(pairs[p]["wins"] / n * 100, 1) if n else 0
    pairs = dict(sorted(pairs.items(), key=lambda x: x[1]["pnl"], reverse=True))

    daily = {}
    for t in closed:
        day = t.get("exit_time", "")[:10]
        if day:
            daily[day] = round(daily.get(day, 0) + t.get("pnl", 0), 2)
    daily = dict(sorted(daily.items()))

    return {
        "total":    len(closed),
        "wins":     len(wins),
        "losses":   len(closed) - len(wins),
        "win_rate": win_rate,
        "pnl":      round(sum(pnls), 2),
        "best":     round(max(pnls), 2),
        "worst":    round(min(pnls), 2),
        "streams":  streams,
        "pairs":    pairs,
        "daily":    daily,
        "open":     open_p,
        "recent":   list(reversed(closed))[:50],
    }


# ─────────────────────────────────────────────────────────────
#  HTML TEMPLATES
#  JS lives here as raw Python strings — no f-string escaping
# ─────────────────────────────────────────────────────────────

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #020817; color: #e2e8f0; font-family: 'IBM Plex Sans', sans-serif; padding: 24px; }
h2 { font-size: 12px; font-weight: 600; color: #475569; text-transform: uppercase; letter-spacing: .08em; margin-bottom: 14px; }
.card { background: #0b1120; border: 1px solid #1e293b; border-radius: 14px; padding: 20px 24px; margin-bottom: 20px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 20px; }
.metric { background: #0f172a; border: 1px solid #1e293b; border-radius: 10px; padding: 14px 18px; }
.metric-label { font-size: 11px; color: #475569; text-transform: uppercase; letter-spacing: .07em; margin-bottom: 6px; }
.metric-value { font-size: 26px; font-weight: 700; font-family: 'IBM Plex Mono', monospace; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; color: #475569; font-size: 11px; text-transform: uppercase; padding: 8px 12px; border-bottom: 1px solid #1e293b; }
td { padding: 10px 12px; border-bottom: 1px solid #0f172a; vertical-align: middle; }
tr:last-child td { border-bottom: none; }
.sgrid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 20px; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: #22c55e; animation: pulse 2s infinite; display: inline-block; margin-right: 6px; }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .4; } }
a.btn { font-size: 12px; color: #94a3b8; text-decoration: none; padding: 6px 14px; border: .5px solid #1e293b; border-radius: 8px; background: #0f172a; }
a.btn-primary { color: #3b82f6; }
"""

# Dashboard page JS — plain string, no Python interpolation needed here
# Chart data is inserted via a separate <script> block with JSON
DASHBOARD_JS = """
(function() {
  // Sentiment panel
  fetch('/api/sentiment').then(function(r) { return r.json(); }).then(function(data) {
    var p = document.getElementById('sent');
    if (!data || !data.asset_scores || !Object.keys(data.asset_scores).length) {
      p.innerHTML = 'No sentiment data yet';
      return;
    }
    var h = '<div style="display:flex;flex-wrap:wrap;gap:10px">';
    Object.keys(data.asset_scores).forEach(function(a) {
      var i = data.asset_scores[a], s = i.score || 0;
      var c = s > 0.15 ? '#22c55e' : s < -0.15 ? '#ef4444' : '#94a3b8';
      var l = s > 0.15 ? 'bullish' : s < -0.15 ? 'bearish' : 'neutral';
      h += '<div style="min-width:110px;background:#0f172a;border:1px solid #1e293b;border-radius:8px;padding:10px">';
      h += '<div style="font-size:11px;color:#475569">' + a + '</div>';
      h += '<div style="font-size:18px;font-weight:700;color:' + c + '">' + (s >= 0 ? '+' : '') + s.toFixed(2) + '</div>';
      h += '<div style="font-size:11px;color:' + c + '">' + l + '</div>';
      h += '</div>';
    });
    h += '</div>';
    p.innerHTML = h;
  }).catch(function() {
    document.getElementById('sent').innerHTML = 'Sentiment unavailable';
  });

  // Luno balances
  fetch('/api/luno').then(function(r) { return r.json(); }).then(function(d) {
    var el = document.getElementById('luno');
    if (!d || !d.positions || !d.positions.length) {
      el.innerHTML = 'No Luno balances';
      return;
    }
    var h = '<div style="display:flex;flex-wrap:wrap;gap:10px;margin-bottom:8px">';
    d.positions.forEach(function(p) {
      h += '<div style="min-width:110px;background:#0f172a;border:1px solid #1e293b;border-radius:8px;padding:10px">';
      h += '<div style="font-size:11px;color:#475569">' + p.asset + '</div>';
      h += '<div style="font-size:18px;font-weight:500;color:#f1f5f9">' + p.qty.toLocaleString() + '</div>';
      if (p.asset !== 'ZAR') {
        h += '<div style="font-size:11px;color:#94a3b8">R' + p.zar.toLocaleString() + '</div>';
      }
      h += '</div>';
    });
    h += '</div>';
    h += '<div style="font-size:11px;color:#475569">Total: R' + d.total_zar.toLocaleString() + ' &middot; ' + d.at + '</div>';
    el.innerHTML = h;
  }).catch(function() {
    document.getElementById('luno').innerHTML = 'Balances unavailable';
  });
})();
"""


def base_html(title, body, extra_head=""):
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>{CSS}</style>
{extra_head}
</head>
<body>
{body}
</body>
</html>"""


def render_dashboard(stats):
    pc   = "#22c55e" if stats["pnl"] >= 0 else "#ef4444"
    ps   = "+" if stats["pnl"] >= 0 else ""
    now  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Metric cards
    metrics = f"""
    <div class="grid">
      <div class="metric"><div class="metric-label">Total P&amp;L</div>
        <div class="metric-value" style="color:{pc}">{ps}{stats['pnl']:.2f}</div></div>
      <div class="metric"><div class="metric-label">Win Rate</div>
        <div class="metric-value" style="color:#f8fafc">{stats['win_rate']}%</div></div>
      <div class="metric"><div class="metric-label">Trades</div>
        <div class="metric-value" style="color:#f8fafc">{stats['total']}</div></div>
      <div class="metric"><div class="metric-label">Best</div>
        <div class="metric-value" style="color:#22c55e">+{stats['best']:.2f}</div></div>
      <div class="metric"><div class="metric-label">Worst</div>
        <div class="metric-value" style="color:#ef4444">{stats['worst']:.2f}</div></div>
      <div class="metric"><div class="metric-label">Open</div>
        <div class="metric-value" style="color:#3b82f6">{len(stats['open'])}</div></div>
    </div>"""

    # Stream cards
    stream_colors = {"forex": "#3b82f6", "luno": "#f59e0b"}
    scards = ""
    for name, d in stats["streams"].items():
        col = stream_colors.get(name.lower(), "#64748b")
        c   = "#22c55e" if d["pnl"] >= 0 else "#ef4444"
        s   = "+" if d["pnl"] >= 0 else ""
        scards += (f'<div style="background:#0f172a;border:1px solid #1e293b;'
                   f'border-radius:12px;padding:16px 20px">'
                   f'<div style="font-size:11px;font-weight:600;color:{col};'
                   f'text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px">{name}</div>'
                   f'<div style="font-size:24px;font-weight:700;color:{c}">{s}{d["pnl"]:.2f}</div>'
                   f'<div style="margin-top:6px;font-size:12px;color:#475569">'
                   f'{d["n"]} trades &middot; {d["wr"]}% WR</div></div>')

    # Daily chart data
    dl = json.dumps(list(stats["daily"].keys())[-30:])
    dv = json.dumps(list(stats["daily"].values())[-30:])
    dc = json.dumps(["#22c55e" if v >= 0 else "#ef4444"
                     for v in list(stats["daily"].values())[-30:]])

    # Open positions
    open_html = ""
    for p in stats["open"]:
        ent = p.get("entry_time", "")[:16].replace("T", " ")
        c   = "#3b82f6" if p.get("stream") == "forex" else "#f59e0b"
        open_html += (f'<div style="display:flex;justify-content:space-between;'
                      f'padding:10px 14px;background:#0f172a;border-radius:8px;margin-bottom:6px">'
                      f'<span style="font-weight:600;color:#f1f5f9">{p.get("symbol","")}</span>'
                      f'<span style="font-size:11px;background:{c}22;color:{c};'
                      f'padding:2px 6px;border-radius:4px">{p.get("stream","")}</span>'
                      f'<span style="font-size:12px;color:#94a3b8">'
                      f'{p.get("side","").upper()} @ {p.get("entry_price","")} &middot; {ent}</span>'
                      f'</div>')
    if not open_html:
        open_html = '<p style="color:#475569;font-size:13px">No open positions</p>'

    # Pair table
    pair_rows = ""
    for sym, d in list(stats["pairs"].items())[:12]:
        c = "#22c55e" if d["pnl"] >= 0 else "#ef4444"
        s = "+" if d["pnl"] >= 0 else ""
        pair_rows += (f'<tr><td style="color:#f1f5f9;font-weight:500">{sym}</td>'
                      f'<td style="color:#94a3b8">{d["n"]}</td>'
                      f'<td style="color:{c};font-weight:500">{s}{d["pnl"]:.2f}</td>'
                      f'<td style="color:#94a3b8">{d["wr"]}%</td></tr>')
    if not pair_rows:
        pair_rows = '<tr><td colspan="4" style="color:#475569;padding:16px">No closed trades yet</td></tr>'

    # Trade history
    trade_rows = ""
    for t in stats["recent"][:25]:
        pnl  = t.get("pnl", 0)
        c    = "#22c55e" if pnl >= 0 else "#ef4444"
        s    = "+" if pnl >= 0 else ""
        side = t.get("side", "").upper()
        sc   = "#22c55e" if side == "BUY" else "#f59e0b"
        ent  = t.get("entry_time", "")[:16].replace("T", " ")
        ext  = t.get("exit_time",  "")[:16].replace("T", " ")
        trade_rows += (
            f'<tr><td style="color:#94a3b8;font-size:12px">{ent}</td>'
            f'<td style="color:#f1f5f9;font-weight:500">{t.get("symbol","")}</td>'
            f'<td><span style="background:{sc}22;color:{sc};padding:2px 7px;'
            f'border-radius:4px;font-size:11px;font-weight:600">{side}</span></td>'
            f'<td style="color:#94a3b8">{t.get("stream","")}</td>'
            f'<td style="color:#94a3b8">{t.get("entry_price","")}</td>'
            f'<td style="color:#94a3b8">{t.get("exit_price","")}</td>'
            f'<td style="color:{c};font-weight:600">{s}{pnl:.2f}</td>'
            f'<td style="color:#94a3b8;font-size:12px">{ext}</td></tr>'
        )
    if not trade_rows:
        trade_rows = '<tr><td colspan="8" style="color:#475569;padding:16px">No closed trades yet</td></tr>'

    # Chart script — inserted as a separate block so JSON data is clean
    chart_script = f"""<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script>
new Chart(document.getElementById('chart'), {{
  type: 'bar',
  data: {{ labels: {dl}, datasets: [{{ data: {dv}, backgroundColor: {dc}, borderRadius: 4 }}] }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{ ticks: {{ color: '#475569', font: {{ size: 10 }} }}, grid: {{ color: '#1e293b' }} }},
      y: {{ ticks: {{ color: '#475569', font: {{ size: 10 }} }}, grid: {{ color: '#1e293b' }} }}
    }}
  }}
}});
</script>"""

    body = f"""
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:28px">
  <div>
    <div style="font-size:22px;font-weight:700;color:#f8fafc;letter-spacing:-.02em">Trading Dashboard</div>
    <div style="font-size:12px;color:#475569;margin-top:4px">
      <span class="dot"></span>Live &middot; {now}
    </div>
  </div>
  <div style="display:flex;gap:10px">
    <a class="btn" href="/backtest">Backtest</a>
    <a class="btn btn-primary" href="/backtest/run">Run Backtest</a>
  </div>
</div>

{metrics}

<h2>P&amp;L by Stream</h2>
<div class="sgrid">{scards or '<p style="color:#475569;font-size:13px">No trades yet</p>'}</div>

<div class="card">
  <h2>Daily P&amp;L (last 30 days)</h2>
  <div style="position:relative;height:200px"><canvas id="chart"></canvas></div>
</div>

<div class="card">
  <h2>WSB Sentiment</h2>
  <div id="sent" style="color:#475569;font-size:13px">Loading...</div>
</div>

<div class="card">
  <h2>Luno Live Balances</h2>
  <div id="luno" style="color:#475569;font-size:13px">Loading...</div>
</div>

<div class="card">
  <h2>Open Positions</h2>
  {open_html}
</div>

<div class="card">
  <h2>Performance by Pair</h2>
  <table>
    <thead><tr><th>Pair</th><th>Trades</th><th>P&amp;L</th><th>Win%</th></tr></thead>
    <tbody>{pair_rows}</tbody>
  </table>
</div>

<div class="card">
  <h2>Recent Trades</h2>
  <div style="overflow-x:auto">
  <table>
    <thead><tr><th>Entry</th><th>Symbol</th><th>Side</th><th>Stream</th>
    <th>Entry Px</th><th>Exit Px</th><th>P&amp;L</th><th>Exit</th></tr></thead>
    <tbody>{trade_rows}</tbody>
  </table>
  </div>
</div>

{chart_script}
<script>{DASHBOARD_JS}</script>"""

    return base_html("Trading Dashboard", body,
                     extra_head='<meta http-equiv="refresh" content="30">')


def render_backtest(bt):
    nav = ('<div style="margin-bottom:24px;display:flex;gap:10px">'
           '<a class="btn" href="/">Dashboard</a>'
           '<a class="btn btn-primary" href="/backtest/run">Re-run</a>'
           '</div>')

    if not bt:
        body = nav + '<p style="color:#475569">No backtest results yet. Click Re-run to start. Takes 2-3 min.</p>'
        return base_html("Backtest", body, '<meta http-equiv="refresh" content="15">')

    combined = bt.get("combined", {})
    luno     = bt.get("luno", {})
    forex    = bt.get("forex", {})
    gen      = bt.get("generated_at", "")[:16].replace("T", " ")
    days     = bt.get("period_days", 1095)

    def pc(v): return "#22c55e" if v >= 0 else "#ef4444"
    def sg(v): return "+" if v >= 0 else ""

    c_ret = combined.get("total_return_pct", 0)
    c_pnl = combined.get("net_profit", 0)
    c_s   = combined.get("starting_capital", 0)
    c_e   = combined.get("final_equity", 0)
    l_ret = luno.get("total_return_pct", 0)
    f_ret = forex.get("total_return_pct", 0)

    # Equity curve
    lc     = luno.get("equity_curve", [])
    fc     = forex.get("equity_curve", [])
    dates  = sorted(set([e["date"] for e in lc] + [e["date"] for e in fc]))[-365:]
    lm     = {e["date"]: e["equity"] for e in lc}
    fm     = {e["date"]: e["equity"] for e in fc}
    lv, fv, cv = [], [], []
    ll = luno.get("starting_capital", 18000)
    lf = forex.get("starting_capital", 1000)
    for d in dates:
        ll = lm.get(d, ll); lf = fm.get(d, lf)
        lv.append(round(ll, 2)); fv.append(round(lf, 2)); cv.append(round(ll+lf, 2))

    dl = json.dumps(dates); lj = json.dumps(lv); fj = json.dumps(fv); cj = json.dumps(cv)

    chart_script = f"""<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script>
new Chart(document.getElementById('eq'), {{
  type: 'line',
  data: {{ labels: {dl}, datasets: [
    {{ label: 'Combined', data: {cj}, borderColor: '#f8fafc', borderWidth: 2, pointRadius: 0, tension: .3 }},
    {{ label: 'Luno', data: {lj}, borderColor: '#f59e0b', borderWidth: 1.5, pointRadius: 0, tension: .3, borderDash: [4,3] }},
    {{ label: 'Forex', data: {fj}, borderColor: '#3b82f6', borderWidth: 1.5, pointRadius: 0, tension: .3, borderDash: [4,3] }}
  ]}},
  options: {{
    responsive: true, maintainAspectRatio: false,
    interaction: {{ mode: 'index', intersect: false }},
    plugins: {{ legend: {{ display: true, labels: {{ color: '#94a3b8', font: {{ size: 11 }}, boxWidth: 12 }} }} }},
    scales: {{
      x: {{ ticks: {{ color: '#475569', font: {{ size: 10 }}, maxTicksLimit: 12 }}, grid: {{ color: '#1e293b' }} }},
      y: {{ ticks: {{ color: '#475569', font: {{ size: 10 }} }}, grid: {{ color: '#1e293b' }} }}
    }}
  }}
}});
</script>"""

    # Monthly returns table
    lmon   = {m["month"]: m["return_pct"] for m in luno.get("monthly_returns", [])}
    fmon   = {m["month"]: m["return_pct"] for m in forex.get("monthly_returns", [])}
    months = sorted(set(list(lmon.keys()) + list(fmon.keys())))
    mrows  = ""
    for mo in months:
        lr = lmon.get(mo, 0); fr = fmon.get(mo, 0); co = round(lr+fr, 2)
        mrows += (f'<tr><td style="color:#94a3b8">{mo}</td>'
                  f'<td style="color:{pc(lr)}">{sg(lr)}{lr:.1f}%</td>'
                  f'<td style="color:{pc(fr)}">{sg(fr)}{fr:.1f}%</td>'
                  f'<td style="color:{pc(co)};font-weight:600">{sg(co)}{co:.1f}%</td></tr>')

    body = f"""{nav}
<div style="margin-bottom:24px">
  <div style="font-size:22px;font-weight:700;color:#f8fafc;letter-spacing:-.02em">Backtest Results</div>
  <div style="font-size:12px;color:#475569;margin-top:4px">{days} days &middot; {gen}</div>
</div>
<div class="grid">
  <div class="metric"><div class="metric-label">Return</div>
    <div class="metric-value" style="color:{pc(c_ret)}">{sg(c_ret)}{c_ret:.1f}%</div></div>
  <div class="metric"><div class="metric-label">Net Profit</div>
    <div class="metric-value" style="color:{pc(c_pnl)}">{sg(c_pnl)}${c_pnl:,.0f}</div></div>
  <div class="metric"><div class="metric-label">Capital</div>
    <div class="metric-value" style="font-size:16px;color:#f8fafc">${c_s:,.0f}&rarr;${c_e:,.0f}</div></div>
  <div class="metric"><div class="metric-label">Luno Return</div>
    <div class="metric-value" style="color:{pc(l_ret)}">{sg(l_ret)}{l_ret:.1f}%</div></div>
  <div class="metric"><div class="metric-label">Forex Return</div>
    <div class="metric-value" style="color:{pc(f_ret)}">{sg(f_ret)}{f_ret:.1f}%</div></div>
</div>
<div class="card">
  <h2>Equity Curve</h2>
  <div style="position:relative;height:260px"><canvas id="eq"></canvas></div>
</div>
<div class="card">
  <h2>Monthly Returns</h2>
  <div style="overflow-x:auto">
  <table>
    <thead><tr><th>Month</th><th>Luno</th><th>Forex</th><th>Combined</th></tr></thead>
    <tbody>{mrows or '<tr><td colspan="4" style="color:#475569;padding:16px">No data</td></tr>'}</tbody>
  </table>
  </div>
</div>
{chart_script}"""

    return base_html("Backtest", body)


# ─────────────────────────────────────────────────────────────
#  BACKTEST TRIGGER
# ─────────────────────────────────────────────────────────────

def _run_backtest():
    try:
        import backtest
        backtest.run_backtest(1095)
    except Exception as e:
        print(f"[backtest] error: {e}")


# ─────────────────────────────────────────────────────────────
#  SENTIMENT LOADER
# ─────────────────────────────────────────────────────────────

def load_sentiment():
    try:
        with open("sentiment_cache.json") as f:
            return json.load(f)
    except Exception:
        return {}


# ─────────────────────────────────────────────────────────────
#  FLASK ROUTES
# ─────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

@app.route("/")
def dashboard():
    try:
        trades = trade_log.get_all()
        stats  = compute_stats(trades)
        return Response(render_dashboard(stats), mimetype="text/html")
    except Exception as e:
        return Response(f"<h2>Error</h2><pre>{e}</pre><a href='/'>Retry</a>",
                        mimetype="text/html", status=500)

@app.route("/backtest")
def backtest_page():
    try:
        bt = {}
        if os.path.exists(BACKTEST_FILE):
            with open(BACKTEST_FILE) as f:
                bt = json.load(f)
        return Response(render_backtest(bt), mimetype="text/html")
    except Exception as e:
        return Response(f"<pre>Error: {e}</pre>", status=500)

@app.route("/api/trades")
def api_trades():
    return jsonify(trade_log.get_all())

@app.route("/api/stats")
def api_stats():
    return jsonify(compute_stats(trade_log.get_all()))

@app.route("/api/luno")
def api_luno():
    with _luno_lock:
        return jsonify(dict(_luno_cache))

@app.route("/api/sentiment")
def api_sentiment():
    return jsonify(load_sentiment())

@app.route("/backtest/run")
def backtest_run():
    threading.Thread(target=_run_backtest, daemon=True).start()
    return jsonify({"status": "running", "message": "Check /backtest in 2-3 min"})


# ─────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Start Luno background cache
    threading.Thread(target=_luno_loop, daemon=True, name="LunoCache").start()
    print(f"Dashboard on http://0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)
