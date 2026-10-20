"""Live dashboard for the trading bot. Read-only: it never places orders.

Local:  pip install -r dashboard/requirements.txt && streamlit run dashboard/app.py
Cloud:  main file dashboard/app.py, secrets ALPACA_API_KEY + ALPACA_SECRET_KEY,
        optional GITHUB_TOKEN (fine-grained, Actions: read) for the "Bot runs" tab.
"""
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import requests
import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)                                      # ✅ reuse the bot's own code

st.set_page_config(page_title="Trading Bot", page_icon="📈", layout="wide")


def secret(name):
    try:
        return st.secrets[name]                               # ✅ Streamlit Cloud secrets
    except Exception:
        return os.getenv(name)                                # ✅ local .env / shell


for key in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
    if secret(key):
        os.environ[key] = secret(key)
if not os.getenv("ALPACA_API_KEY"):
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
if not os.getenv("ALPACA_API_KEY"):
    st.error("Add ALPACA_API_KEY and ALPACA_SECRET_KEY to the app's secrets (or .env locally).")
    st.stop()

import crypto_trader as ct                                    # noqa: E402  (needs keys set first)
import stock_trader as stk                                    # noqa: E402
import top_crypto_rotator as rot                              # noqa: E402
from strategy import ema, vwap, adx, exit_reason, closed_candles  # noqa: E402
from alpaca.trading.requests import GetOrdersRequest          # noqa: E402
from alpaca.trading.enums import QueryOrderStatus, AssetClass  # noqa: E402
from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest  # noqa: E402
from alpaca.data.timeframe import TimeFrame                   # noqa: E402

REPO = secret("GITHUB_REPO") or "aruios/alpaca-trading-bot"
GITHUB_TOKEN = secret("GITHUB_TOKEN")
TIMEZONES = ["America/New_York", "UTC", "Europe/London", "Asia/Kolkata", "America/Los_Angeles"]


# ---------- data (cached so a refresh every minute stays light) ----------

def is_crypto(symbol):
    return "/" in symbol


def rules_for(symbol):
    return ct.RULES if is_crypto(symbol) else stk.RULES


@st.cache_data(ttl=60)
def load_account():
    a = ct.trading_client.get_account()
    return SimpleNamespace(equity=float(a.equity), last_equity=float(a.last_equity),
                           cash=float(a.cash), buying_power=float(a.buying_power))


@st.cache_data(ttl=60)
def load_positions():
    out = []
    for p in ct.trading_client.get_all_positions():
        crypto = p.asset_class == AssetClass.CRYPTO
        out.append(SimpleNamespace(
            symbol=ct.to_pair(p.symbol) if crypto else p.symbol, crypto=crypto,
            qty=float(p.qty), entry=float(p.avg_entry_price), price=float(p.current_price),
            market_value=float(p.market_value), unrealized_pl=float(p.unrealized_pl),
            unrealized_plpc=float(p.unrealized_plpc),
        ))
    return out


@st.cache_data(ttl=600)
def crypto_universe():
    main = ct.get_tradable(ct.WATCHLIST)
    small = [c for c in ct.get_tradable(ct.SMALL_COIN_WATCHLIST) if c not in main]
    return main, small


@st.cache_data(ttl=300)
def stock_universe():
    return stk.get_tradable(list(dict.fromkeys(stk.WATCHLIST + stk.get_trending(stk.TRENDING_TOP))))


@st.cache_data(ttl=60)
def load_signals(crypto_syms, stock_syms):
    sigs = dict(ct.get_signals(list(crypto_syms)))
    sigs.update(stk.get_signals(list(stock_syms)))
    return sigs


@st.cache_data(ttl=60)
def load_rotator_holding():
    return ct.rotator_holding()


@st.cache_data(ttl=60)
def load_orders(days=7):
    orders = ct.trading_client.get_orders(GetOrdersRequest(
        status=QueryOrderStatus.CLOSED, limit=500,
        after=datetime.now(timezone.utc) - timedelta(days=days)))
    out = []
    for o in orders:
        crypto = o.asset_class == AssetClass.CRYPTO
        out.append(SimpleNamespace(
            time=o.filled_at or o.submitted_at,
            symbol=ct.to_pair(o.symbol) if crypto else o.symbol,
            side=o.side.value, status=o.status.value,
            qty=float(o.filled_qty or 0),
            price=float(o.filled_avg_price) if o.filled_avg_price else None,
        ))
    return out


@st.cache_data(ttl=60)
def load_candles(symbol, days):
    rules = rules_for(symbol)
    start = datetime.now(timezone.utc) - timedelta(days=days + (3 if is_crypto(symbol) else 10))
    if is_crypto(symbol):                                     # ✅ extra history warms up EMA/ADX
        bars = ct.data_client.get_crypto_bars(CryptoBarsRequest(
            symbol_or_symbols=symbol, timeframe=ct.CANDLE, start=start)).data
    else:
        bars = stk.data_client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Hour, start=start, feed=stk.FEED)).data
    return [SimpleNamespace(timestamp=b.timestamp, open=b.open, high=b.high, low=b.low,
                            close=b.close, volume=b.volume)
            for b in closed_candles(bars.get(symbol, []), rules.candle_minutes)]


def github(path, **params):
    r = requests.get(f"https://api.github.com/repos/{REPO}/{path}", params=params, timeout=20,
                     headers={"Authorization": f"Bearer {GITHUB_TOKEN}",
                              "Accept": "application/vnd.github+json"})
    r.raise_for_status()
    return r


@st.cache_data(ttl=60)
def load_runs():
    jobs = []
    for run in github("actions/workflows/trading_bot.yml/runs", per_page=12).json()["workflow_runs"]:
        for j in github(f"actions/runs/{run['id']}/jobs").json()["jobs"]:
            if j["conclusion"] != "skipped":                  # ✅ each run skips the other job
                jobs.append(SimpleNamespace(
                    id=j["id"], name=j["name"], status=j["status"], conclusion=j["conclusion"],
                    started=datetime.fromisoformat(j["started_at"].replace("Z", "+00:00")),
                    url=j["html_url"]))
    return jobs


BOT_LINE = re.compile(r"^(BUY|SELL|HOLD|wait|Budget|Small-coin|Account down|Market closed|"
                      r"Not tradable|No candles|Crypto exposure|Stock exposure|\s+\S+ at \$)"
                      r"|failed")


@st.cache_data(ttl=3600)
def load_job_log(job_id):
    try:
        text = github(f"actions/jobs/{job_id}/logs").text
    except requests.HTTPError:
        return ["(log not available yet)"]
    lines = [re.sub(r"^\S+Z ", "", ln) for ln in text.splitlines()]     # ✅ drop GitHub timestamps
    return [ln for ln in lines if BOT_LINE.search(ln)] or ["(no bot output)"]


# ---------- helpers ----------

def next_runs(now):
    """Next scheduled times from trading_bot.yml (GitHub may start them a few minutes late)."""
    crypto = now.replace(second=0, microsecond=0)
    while not (crypto > now and crypto.minute in (2, 32)):
        crypto += timedelta(minutes=1)
    stock = now.replace(minute=5, second=0, microsecond=0)
    while not (stock > now and stock.weekday() < 5 and 14 <= stock.hour <= 20):
        stock += timedelta(hours=1)
    return crypto, stock


def money(v):
    return f"${v:,.2f}"


def fmt_time(t, tz):
    return t.astimezone(tz).strftime("%b %d %H:%M") if t else ""


def position_status(p, sig):
    why = exit_reason(p, sig, rules_for(p.symbol))
    if why:
        return f"SELL next run: {why}"
    if p.crypto and p.unrealized_plpc <= 0:
        return "🔒 holding until in profit"
    return "hold"


def check_cell(passed, value, name):
    if "cross" in name or value is None:
        return "✓" if passed else "✗"
    return f"{'✓' if passed else '✗'} {value:,.6g}" if value >= 1000 else \
        f"{'✓' if passed else '✗'} {value:.4g}"


# ---------- sections ----------

def overview(account, positions, small_set, tz):
    day = account.equity - account.last_equity
    day_pct = day / account.last_equity if account.last_equity else 0
    c = st.columns(4)
    c[0].metric("Equity (paper)", money(account.equity), f"{day:+,.2f} ({day_pct:+.2%}) today")
    c[1].metric("Cash", money(account.cash))
    c[2].metric("Buying power", money(account.buying_power))
    limit = ct.DAILY_LOSS_LIMIT
    c[3].metric("Daily-loss stop", f"{day_pct:+.2%} / -{limit:.0%}",
                "buying paused" if day_pct <= -limit else "buying allowed",
                delta_color="inverse" if day_pct <= -limit else "normal")

    crypto_exp = sum(p.market_value for p in positions if p.crypto)
    small_exp = sum(p.market_value for p in positions if p.symbol in small_set)
    stock_exp = sum(p.market_value for p in positions
                    if not p.crypto and p.symbol not in stk.DONT_MANAGE)
    c = st.columns(3)
    for col, label, used, cap in [
        (c[0], "Crypto budget", crypto_exp, ct.BUDGET_USD),
        (c[1], "Small-coin budget", small_exp, ct.SMALL_COIN_BUDGET_USD),
        (c[2], "Stock budget", stock_exp, stk.BUDGET_USD),
    ]:
        col.progress(min(used / cap, 1.0), text=f"{label}: {money(used)} of {money(cap)}")

    now = datetime.now(timezone.utc)
    nc, ns = next_runs(now)
    st.caption(f"Next scheduled runs: crypto {fmt_time(nc, tz)} · stocks {fmt_time(ns, tz)} "
               f"({tz.key}). Page refreshes every minute.")


def positions_table(positions, signals, rotator_sym):
    if not positions:
        st.info("No open positions.")
        return
    rows = []
    for p in sorted(positions, key=lambda p: p.unrealized_plpc, reverse=True):
        tp = rules_for(p.symbol).take_profit
        rows.append({
            "Symbol": p.symbol,
            "Type": "crypto (rotator)" if p.symbol == rotator_sym else "crypto" if p.crypto else "stock",
            "Qty": p.qty, "Entry": p.entry, "Price": p.price, "Value": p.market_value,
            "P/L $": p.unrealized_pl, "P/L %": p.unrealized_plpc * 100,
            "To take-profit": f"{((rot.TAKE_PROFIT_PCT / 100 if p.symbol == rotator_sym else tp) - p.unrealized_plpc) * 100:+.1f}%",
            "Bot will": "not managed" if p.symbol in stk.DONT_MANAGE
            else f"rotator: sells at +{rot.TAKE_PROFIT_PCT:g}% or rotates while in profit" if p.symbol == rotator_sym
            else position_status(p, signals.get(p.symbol)),
        })
    st.dataframe(rows, hide_index=True, width="stretch", column_config={
        "Qty": st.column_config.NumberColumn(format="%.6g"),
        "Entry": st.column_config.NumberColumn(format="$%.6g"),
        "Price": st.column_config.NumberColumn(format="$%.6g"),
        "Value": st.column_config.NumberColumn(format="$%.2f"),
        "P/L $": st.column_config.NumberColumn(format="$%+.2f"),
        "P/L %": st.column_config.NumberColumn(format="%+.2f%%"),
    })


def signals_table(symbols, signals, held, small_set):
    rows = []
    for sym in symbols:
        sig = signals.get(sym)
        if sig is None:
            continue
        if sym in held:
            decision = "held"
        elif not sig.checks:
            decision = sig.reason                             # ✅ e.g. not enough candles
        elif sig.buy and sym in small_set and sig.close > ct.SMALL_COIN_MAX_PRICE:
            decision = f"skip: above ${ct.SMALL_COIN_MAX_PRICE:g}"
        elif sig.buy:
            decision = "🟢 BUY next run"
        else:
            decision = "wait"
        row = {"Symbol": sym, "Price": sig.close}
        for name, (passed, value) in sig.checks.items():
            row[name] = check_cell(passed, value, name)
        row["Passed"] = sum(ok for ok, _ in sig.checks.values())
        row["Bot will"] = decision
        rows.append(row)
    rows.sort(key=lambda r: (r["Bot will"].startswith("🟢"), r["Passed"]), reverse=True)
    if rows:
        st.dataframe(rows, hide_index=True, width="stretch", column_config={
            "Price": st.column_config.NumberColumn(format="$%.6g"),
            "Passed": st.column_config.ProgressColumn(min_value=0, max_value=5, format="%d/5"),
        })
    else:
        st.info("No symbols to show.")


def chart(symbol, days, orders, tz):
    rules = rules_for(symbol)
    candles = load_candles(symbol, days)
    if len(candles) < rules.ema_slow:
        st.warning(f"Only {len(candles)} candles for {symbol}.")
        return
    closes = [b.close for b in candles]
    fast, slow = ema(closes, rules.ema_fast), ema(closes, rules.ema_slow)
    vw, adx_line = vwap(candles, rules.vwap_tz), adx(candles, rules.adx_period)

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    first = next((i for i, b in enumerate(candles) if b.timestamp >= cutoff), 0)
    candles, fast, slow, vw, adx_line = (s[first:] for s in (candles, fast, slow, vw, adx_line))
    x = [b.timestamp.astimezone(tz).strftime("%m-%d %H:%M") for b in candles]  # ✅ no night/weekend gaps

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.75, 0.25],
                        vertical_spacing=0.03)
    fig.add_trace(go.Candlestick(x=x, open=[b.open for b in candles], high=[b.high for b in candles],
                                 low=[b.low for b in candles], close=[b.close for b in candles],
                                 name=symbol, increasing_line_color="#26a69a",
                                 decreasing_line_color="#ef5350"), row=1, col=1)
    fig.add_trace(go.Scatter(x=x, y=fast, name=f"EMA {rules.ema_fast}",
                             line=dict(color="blue", width=1.5)), row=1, col=1)
    fig.add_trace(go.Scatter(x=x, y=slow, name=f"EMA {rules.ema_slow}",
                             line=dict(color="black", width=1.5)), row=1, col=1)
    day_tz = ZoneInfo(rules.vwap_tz)
    days_of = [b.timestamp.astimezone(day_tz).date() for b in candles]
    starts = [i for i in range(len(x)) if i == 0 or days_of[i] != days_of[i - 1]] + [len(x)]
    for n, (a, b) in enumerate(zip(starts, starts[1:])):      # ✅ one VWAP segment per day
        fig.add_trace(go.Scatter(x=x[a:b], y=vw[a:b], name="VWAP", legendgroup="vwap",
                                 showlegend=n == 0, line=dict(color="purple", width=1.5)),
                      row=1, col=1)

    step = timedelta(minutes=rules.candle_minutes)
    for side, symbol_mark, color in [("buy", "triangle-up", "#1b8a3a"), ("sell", "triangle-down", "#c62828")]:
        mx, my, text = [], [], []
        for o in orders:
            if o.symbol != symbol or o.side != side or o.price is None or o.time is None:
                continue
            i = next((i for i, b in enumerate(candles) if b.timestamp <= o.time < b.timestamp + step), None)
            if i is None and candles and o.time >= candles[-1].timestamp:
                i = len(candles) - 1                          # ✅ filled in the still-open candle
            if i is not None:
                mx.append(x[i])
                my.append(o.price)
                text.append(f"{side.upper()} {o.qty:g} @ {o.price:,.6g}<br>{fmt_time(o.time, tz)}")
        if mx:
            fig.add_trace(go.Scatter(x=mx, y=my, mode="markers", name=side.upper(), text=text,
                                     hoverinfo="text", marker=dict(symbol=symbol_mark, size=13,
                                     color=color, line=dict(color="white", width=1))), row=1, col=1)

    fig.add_trace(go.Scatter(x=x, y=adx_line, name=f"ADX {rules.adx_period}",
                             line=dict(color="red", width=1.5)), row=2, col=1)
    fig.add_hline(y=rules.adx_min, line_dash="dash", line_color="gray", row=2, col=1)
    fig.update_yaxes(range=[0, 100], row=2, col=1)
    fig.update_xaxes(type="category", nticks=12, rangeslider_visible=False)
    fig.update_layout(template="plotly_white", height=640, margin=dict(l=10, r=10, t=30, b=10),
                      legend=dict(orientation="h", y=1.04), hovermode="x unified",
                      title=f"{symbol} · {rules.candle_minutes}-minute candles ({tz.key})")
    st.plotly_chart(fig, width="stretch", theme=None)  # ✅ white chart so black EMA shows


def activity(orders, tz):
    rows = [{"Time": fmt_time(o.time, tz), "Side": o.side.upper(), "Symbol": o.symbol,
             "Qty": o.qty, "Fill price": o.price, "Value": (o.qty * o.price) if o.price else None,
             "Status": o.status}
            for o in sorted(orders, key=lambda o: o.time or datetime.min.replace(tzinfo=timezone.utc),
                            reverse=True)]
    if not rows:
        st.info("No orders in the last 7 days.")
        return
    st.dataframe(rows, hide_index=True, width="stretch", column_config={
        "Qty": st.column_config.NumberColumn(format="%.6g"),
        "Fill price": st.column_config.NumberColumn(format="$%.6g"),
        "Value": st.column_config.NumberColumn(format="$%.2f"),
    })


def bot_runs(tz):
    if not GITHUB_TOKEN:
        st.info("Add a GITHUB_TOKEN secret (fine-grained, this repo, Actions: read) "
                "to see the bot's runs and what it printed.")
        return
    try:
        jobs = load_runs()
    except Exception as e:
        st.error(f"Could not read GitHub Actions: {e}")
        return
    icon = {"success": "✅", "failure": "❌", "cancelled": "⚪"}
    for j in jobs:
        state = icon.get(j.conclusion, "⏳") + " " + (j.conclusion or j.status)
        with st.expander(f"{fmt_time(j.started, tz)} · {j.name} · {state}"):
            if j.status == "completed":
                st.code("\n".join(load_job_log(j.id)), language=None)
            st.markdown(f"[Open on GitHub]({j.url})")


# ---------- page ----------

st.sidebar.title("📈 Trading Bot")
tz = ZoneInfo(st.sidebar.selectbox("Time zone", TIMEZONES))
auto = st.sidebar.toggle("Auto-refresh (1 min)", value=True)
if st.sidebar.button("Refresh now"):
    st.cache_data.clear()
st.sidebar.caption("Paper account · read-only view, this page never places orders.")


@st.fragment(run_every="60s" if auto else None)
def page():
    try:
        account, positions = load_account(), load_positions()
        main, small = crypto_universe()
        stocks = stock_universe()
        held = {p.symbol for p in positions}
        held_crypto = {p.symbol for p in positions if p.crypto}
        held_stock = held - held_crypto
        signals = load_signals(tuple(sorted(set(main) | set(small) | held_crypto)),
                               tuple(sorted(set(stocks) | held_stock)))
        orders = load_orders()
    except Exception as e:
        st.error(f"Could not load data from Alpaca: {e}")
        return
    small_set = set(small)

    tabs = st.tabs(["Overview", "Signals", "Chart", "Activity", "Bot runs"])
    with tabs[0]:
        overview(account, positions, small_set, tz)
        st.subheader("Positions")
        positions_table(positions, signals, load_rotator_holding())
    with tabs[1]:
        st.caption("Each buy check on the last closed candle. A buy needs all 5. "
                   "This is what the bot would do if it ran now.")
        st.subheader("Crypto")
        st.caption("New crypto buys are made by top_crypto_rotator.py (today's top performer); "
                   "these checks now drive sells of the coins crypto_trader.py already holds.")
        signals_table(main + small, signals, held, small_set)
        st.subheader("Stocks")
        st.caption("Your WATCHLIST plus today's top gainers; the bot only trades in market hours.")
        signals_table(stocks, signals, held, small_set)
    with tabs[2]:
        options = list(dict.fromkeys(sorted(held) + main + small + stocks))
        c = st.columns([3, 1])
        symbol = c[0].selectbox("Symbol", options, key="chart_symbol")
        days = c[1].slider("Days", 1, 10, 3, key="chart_days")
        if symbol:
            chart(symbol, days, orders, tz)
    with tabs[3]:
        st.caption("Filled and cancelled orders, last 7 days.")
        activity(orders, tz)
    with tabs[4]:
        bot_runs(tz)


page()
