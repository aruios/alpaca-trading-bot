"""Backtest the bot's current rules on Alpaca price history, with fees. Places no orders.

  python backtest.py              # last 6 months
  python backtest.py --months 12

Simulates three strategies separately, each with its own budget, using the settings in
crypto_trader.py, top_crypto_rotator.py, stock_trader.py and strategy.py:
  1. Crypto bot   30-min candles, 5 buy checks, +15% limit take-profit, -15% stop, signal exit > +0.75%
  2. Rotator      (disabled live) today's top riser (>= +2%) with a confirming chart, +8% limit
                  take-profit, rotates > +0.75% only, no stop
  3. Stocks       1-hour candles on WATCHLIST, 5 buy checks, +10% / -7%, signal exit

Fills: market orders at the candle close +/- slippage; take-profit limits fill when a later
candle's high reaches them. Crypto pays FEE on both sides (Alpaca ~0.25%). Prices are cached
in backtest_data/ so re-runs are fast.

Not modelled (results are an approximation): the stock top-gainer scan (needs past daily
gainer lists), the 3% daily-loss stop, the rotator running every 15 min (here every 30),
the two crypto bots sharing coins, and coins delisted before today (survivorship bias).
"""
import argparse
import os
import pickle
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame

import crypto_trader as ct
import stock_trader as stk
import top_crypto_rotator as rot
from strategy import ema, adx, vwap

CRYPTO_FEE = 0.0025          # ✅ Alpaca crypto fee per side
CRYPTO_SLIP = 0.0005         # ✅ market orders fill a bit worse than the candle close
STOCK_SLIP = 0.0002
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_data")
NY = ZoneInfo("America/New_York")


# ---------- data ----------

def cached(name, fetch):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, name + ".pkl")
    if os.path.exists(path):
        with open(path, "rb") as f:
            return pickle.load(f)
    data = fetch()
    with open(path, "wb") as f:
        pickle.dump(data, f)
    return data


def plain(bars):
    return [SimpleNamespace(timestamp=b.timestamp, open=b.open, high=b.high, low=b.low,
                            close=b.close, volume=b.volume) for b in bars]


def crypto_bars(symbols, start, end):
    def fetch():
        print(f"Downloading 30-min candles for {len(symbols)} coins ...", flush=True)
        data = ct.data_client.get_crypto_bars(CryptoBarsRequest(
            symbol_or_symbols=symbols, timeframe=ct.CANDLE, start=start, end=end)).data
        return {s: plain(data.get(s, [])) for s in symbols}
    return cached(f"crypto_{start:%Y%m%d}_{end:%Y%m%d}_{len(symbols)}", fetch)


def stock_bars(symbols, start, end):
    def fetch():
        print(f"Downloading 1-hour candles for {len(symbols)} stocks ...", flush=True)
        data = stk.data_client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbols, timeframe=TimeFrame.Hour, start=start, end=end,
            feed=stk.FEED)).data
        return {s: plain(data.get(s, [])) for s in symbols}
    return cached(f"stocks_{start:%Y%m%d}_{end:%Y%m%d}_{'-'.join(symbols)}", fetch)


# ---------- indicators as full series (same math as strategy.py) ----------

def sma_series(values, n):
    out, total = [None] * len(values), 0.0
    for i, v in enumerate(values):
        total += v
        if i >= n:
            total -= values[i - n]
        if i >= n - 1:
            out[i] = total / n
    return out


def rsi_series(values, n=14):
    out = [None] * len(values)
    if len(values) <= n:
        return out
    gains = losses = 0.0
    for a, b in zip(values[:n], values[1:n + 1]):
        gains += max(b - a, 0)
        losses += max(a - b, 0)
    avg_g, avg_l = gains / n, losses / n
    def value():
        return 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    out[n] = value()
    for i in range(n + 1, len(values)):
        d = values[i] - values[i - 1]
        avg_g = (avg_g * (n - 1) + max(d, 0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0)) / n
        out[i] = value()
    return out


class Series:
    """Every indicator the bot uses, precomputed once per symbol."""

    def __init__(self, candles, r):
        self.c = candles
        self.index = {b.timestamp: i for i, b in enumerate(candles)}
        closes = [b.close for b in candles]
        self.close = closes
        self.fast, self.slow = ema(closes, r.ema_fast), ema(closes, r.ema_slow)
        self.trend = sma_series(closes, r.trend_sma)
        self.rsi = rsi_series(closes, r.rsi_period)
        self.adx = adx(candles, r.adx_period)
        self.vwap = vwap(candles, r.vwap_tz)
        self.need = max(r.trend_sma, r.ema_slow + r.cross_lookback, r.rsi_period + 1, 2 * r.adx_period + 1)
        self.r = r

    def signal(self, i):
        """(buy, sell, strength) exactly like strategy.evaluate on candles[:i+1]."""
        r = self.r
        if i + 1 < self.need:
            return False, False, 0.0
        close, trend = self.close[i], self.trend[i]
        above_now = self.fast[i] > self.slow[i]
        fresh = above_now and any(self.fast[i - k] <= self.slow[i - k] for k in range(1, r.cross_lookback + 1))
        uptrend = close > trend
        strong = self.adx[i] is not None and self.adx[i] >= r.adx_min
        above_vwap = self.vwap[i] is not None and close > self.vwap[i]
        buy = uptrend and fresh and self.rsi[i] < r.rsi_max and strong and above_vwap
        return buy, (not above_now) or (not uptrend), close / trend - 1


# ---------- bookkeeping ----------

class Book:
    def __init__(self, name, budget, fee, slip):
        self.name, self.budget, self.fee, self.slip = name, budget, fee, slip
        self.pos = {}          # sym -> SimpleNamespace(qty, entry, cost)
        self.trades = []       # (sym, net_pnl, net_pct, reason)
        self.realized = 0.0
        self.curve = []

    def buy(self, sym, usd, price, t=None):
        fill = price * (1 + self.slip)
        self.pos[sym] = SimpleNamespace(qty=usd / fill * (1 - self.fee), entry=fill, cost=usd, t=t)

    def sell(self, sym, price, reason, market=True):
        p = self.pos.pop(sym)
        fill = price * (1 - self.slip) if market else price
        net = p.qty * fill * (1 - self.fee) - p.cost
        self.realized += net
        self.trades.append((sym, net, net / p.cost, reason))

    def pl(self, sym, price):
        return price / self.pos[sym].entry - 1          # ✅ what Alpaca reports (fees not included)

    def mark(self, prices):
        open_value = sum(p.qty * prices.get(s, p.entry) - p.cost for s, p in self.pos.items())
        self.curve.append(self.realized + open_value)

    def exposure(self, prices):
        return sum(p.qty * prices.get(s, p.entry) for s, p in self.pos.items())


def timeline(series):
    return sorted({t for s in series.values() for t in s.index})


def prices_at(series, t):
    out = {}
    for sym, s in series.items():
        i = s.index.get(t)
        if i is not None:
            out[sym] = s.close[i]
    return out


# ---------- 1. crypto bot ----------

LIVE = object()


def run_crypto_bot(series, main, small, stop_loss=LIVE, max_days=None, take_profit=None,
                   budget=None, name="Crypto bot"):
    """stop_loss: sell at -x (default: the live RULES.stop_loss; None = never). max_days: sell a
    position still below entry after N days. take_profit / budget: override the live settings."""
    r = ct.RULES
    stop_loss = r.stop_loss if stop_loss is LIVE else stop_loss
    tp = take_profit or r.take_profit
    budget = budget or ct.BUDGET_USD
    book = Book(name, budget, CRYPTO_FEE, CRYPTO_SLIP)
    small = set(small)
    for t in timeline(series):
        prices = prices_at(series, t)
        for sym in list(book.pos):                                    # ✅ standing +15% limit sell
            i = series[sym].index.get(t)
            target = book.pos[sym].entry * (1 + tp)
            if i is not None and series[sym].c[i].high >= target:
                book.sell(sym, target, "take-profit", market=False)
        sold = set()
        for sym in list(book.pos):                                    # ✅ variants: stop-loss / time limit
            if sym not in prices:
                continue
            pl = book.pl(sym, prices[sym])
            if stop_loss is not None and pl <= -stop_loss:
                book.sell(sym, prices[sym], "stop-loss"); sold.add(sym)
            elif max_days is not None and pl < 0 and t - book.pos[sym].t >= timedelta(days=max_days):
                book.sell(sym, prices[sym], "time limit"); sold.add(sym)
        for sym in list(book.pos):                                    # ✅ signal exit only above min_profit
            i = series[sym].index.get(t)
            if i is None:
                continue
            _, sell, _ = series[sym].signal(i)
            if sell and (r.min_profit is None or book.pl(sym, prices[sym]) > r.min_profit):
                book.sell(sym, prices[sym], "signal exit")
                sold.add(sym)
        exposure = book.exposure(prices)
        small_exp = sum(book.pos[s].qty * prices.get(s, book.pos[s].entry) for s in book.pos if s in small)
        candidates = []
        for sym in main + sorted(small):
            i = series[sym].index.get(t) if sym in series else None
            if i is None or sym in book.pos or sym in sold:
                continue
            buy, _, strength = series[sym].signal(i)
            if buy and not (sym in small and prices[sym] > ct.SMALL_COIN_MAX_PRICE):
                candidates.append((strength, sym))
        for _, sym in sorted(candidates, reverse=True):
            usd = ct.SMALL_TRADE_USD if sym in small else ct.MAIN_TRADE_USD
            if exposure + usd > budget or (sym in small and small_exp + usd > ct.SMALL_COIN_BUDGET_USD):
                continue
            book.buy(sym, usd, prices[sym], t)
            exposure += usd
            small_exp += usd if sym in small else 0
        book.mark(prices)
    return book, prices


# ---------- 2. rotator ----------

def day_stats(s):
    """Per candle: today's open (UTC day) and yesterday's $ volume."""
    day_open, prev_vol = [], []
    cur_day, cur_open, cur_vol, last_vol = None, None, 0.0, 0.0
    for b in s.c:
        d = b.timestamp.date()
        if d != cur_day:
            last_vol = cur_vol if cur_day is not None and (d - cur_day).days == 1 else 0.0
            cur_day, cur_open, cur_vol = d, b.open, 0.0
        cur_vol += b.volume * b.close
        day_open.append(cur_open)
        prev_vol.append(last_vol)
    return day_open, prev_vol


def run_rotator(series, stop_loss=None, name="Rotator"):
    """stop_loss: sell the held coin at -x and free the budget (live: None)."""
    book = Book(name, rot.BUDGET_USD, CRYPTO_FEE, CRYPTO_SLIP)
    stats = {sym: day_stats(s) for sym, s in series.items()}
    held = None
    for t in timeline(series):
        prices = prices_at(series, t)
        if held:                                                      # ✅ standing +8% limit sell
            i = series[held].index.get(t)
            target = book.pos[held].entry * (1 + rot.TAKE_PROFIT_PCT / 100)
            if i is not None and series[held].c[i].high >= target:
                book.sell(held, target, "take-profit", market=False)
                held = None
        if held and stop_loss is not None and held in prices and book.pl(held, prices[held]) <= -stop_loss:
            book.sell(held, prices[held], "stop-loss")
            held = None
        ranked = []
        for sym, s in series.items():
            i = s.index.get(t)
            if i is None:
                continue
            day_open, prev_vol = stats[sym]
            if day_open[i] > 0 and prev_vol[i] >= rot.MIN_DOLLAR_VOL:
                ranked.append((s.close[i] / day_open[i] * 100 - 100, sym, i))
        ranked.sort(reverse=True)
        pick = None
        for pct, sym, i in ranked:
            if pct < rot.MIN_GAIN_PCT:
                break
            if sym == held:
                continue
            s = series[sym]
            if (i >= s.r.ema_slow and s.fast[i] > s.slow[i] and s.vwap[i] is not None
                    and s.close[i] > s.vwap[i] and s.rsi[i] is not None and s.rsi[i] < rot.RSI_MAX):
                pick = (sym, pct)
                break
        if held and pick:
            held_pct = next((p for p, s, _ in ranked if s == held), -999)
            if (pick[1] - held_pct >= rot.SWITCH_MARGIN and held in prices
                    and book.pl(held, prices[held]) * 100 > rot.MIN_ROTATE_PROFIT_PCT):
                book.sell(held, prices[held], f"rotate to {pick[0]}")
                held = None
        if held is None and pick:
            book.buy(pick[0], rot.BUDGET_USD, prices[pick[0]], t)
            held = pick[0]
        book.mark(prices)
    return book, prices


# ---------- 3. stocks ----------

def run_stocks(series, trend_exit_only=False, name="Stocks (watchlist)"):
    """trend_exit_only: signal exit only when price falls below SMA 50 (live: also on EMA 9 < 21)."""
    r = stk.RULES
    book = Book(name, stk.BUDGET_USD, 0.0, STOCK_SLIP)
    for t in timeline(series):
        prices = prices_at(series, t)
        close_et = (t + timedelta(hours=1)).astimezone(NY)
        if close_et.weekday() >= 5 or not (10 <= close_et.hour <= 16):  # ✅ bot only trades in market hours
            book.mark(prices)
            continue
        for sym in list(book.pos):
            i = series[sym].index.get(t)
            if i is None:
                continue
            pl = book.pl(sym, prices[sym])
            _, sell, _ = series[sym].signal(i)
            if trend_exit_only:
                sell = prices[sym] < series[sym].trend[i]
            why = ("stop-loss" if pl <= -r.stop_loss else "take-profit" if pl >= r.take_profit
                   else "signal exit" if sell else None)
            if why:
                book.sell(sym, prices[sym], why)
        exposure = book.exposure(prices)
        candidates = []
        for sym, s in series.items():
            i = s.index.get(t)
            if i is None or sym in book.pos or prices[sym] < stk.MIN_STOCK_PRICE:
                continue
            buy, _, strength = s.signal(i)
            if buy:
                candidates.append((strength, sym))
        for _, sym in sorted(candidates, reverse=True):
            if exposure + stk.TRADE_USD > stk.BUDGET_USD:
                break
            book.buy(sym, stk.TRADE_USD, prices[sym], t)
            exposure += stk.TRADE_USD
        book.mark(prices)
    return book, prices


# ---------- report ----------

def max_drawdown(curve):
    peak, worst = 0.0, 0.0
    for v in curve:
        peak = max(peak, v)
        worst = min(worst, v - peak)
    return worst


def report(book, prices, benchmark_name, benchmark_pct):
    trades = book.trades
    wins = [t for t in trades if t[1] > 0]
    losses = [t for t in trades if t[1] <= 0]
    open_pl = sum(p.qty * prices.get(s, p.entry) * (1 - book.fee) - p.cost for s, p in book.pos.items())
    total = book.realized + open_pl
    print(f"\n=== {book.name}  (budget ${book.budget:,.0f})")
    print(f"  Closed trades     {len(trades)}   wins {len(wins)}  losses {len(losses)}"
          + (f"   win rate {len(wins) / len(trades):.0%}" if trades else ""))
    if wins:
        print(f"  Avg win           {sum(t[2] for t in wins) / len(wins):+.2%}  (${sum(t[1] for t in wins) / len(wins):+,.2f})")
    if losses:
        print(f"  Avg loss          {sum(t[2] for t in losses) / len(losses):+.2%}  (${sum(t[1] for t in losses) / len(losses):+,.2f})")
    reasons = {}
    for t in trades:
        key = t[3].split(" to ")[0]
        reasons[key] = reasons.get(key, 0) + 1
    if reasons:
        print("  Exits             " + ", ".join(f"{k} {v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1])))
    print(f"  Realized P/L      ${book.realized:+,.2f}  (after fees)")
    print(f"  Still open        {len(book.pos)} position(s), ${open_pl:+,.2f} if sold now"
          + (": " + ", ".join(f"{s} {prices.get(s, p.entry) / p.entry - 1:+.1%}" for s, p in book.pos.items()) if book.pos else ""))
    print(f"  Total             ${total:+,.2f}  = {total / book.budget:+.2%} of budget")
    print(f"  Worst drawdown    ${max_drawdown(book.curve):+,.2f}")
    print(f"  Benchmark         hold {benchmark_name}: {benchmark_pct:+.2%}")


def hold_return(candles):
    return candles[-1].close / candles[0].close - 1 if candles else 0.0


def load(months):
    """Download (or read cached) candles and build indicator series for a period."""
    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)  # ✅ stable cache key per day
    start = end - timedelta(days=30 * months)
    warmup = start - timedelta(days=5)                                # ✅ indicators need history first
    main = ct.get_tradable(ct.WATCHLIST)
    small = [c for c in ct.get_tradable(ct.SMALL_COIN_WATCHLIST) if c not in main]
    rot_universe = rot.usd_pairs()
    coins = sorted(set(main) | set(small) | set(rot_universe) | {"BTC/USD"})
    raw = crypto_bars(coins, warmup, end)

    def from_start(series):
        """Indicators use the warm-up candles; trading starts at `start`."""
        out = {}
        for sym, s in series.items():
            keep = [i for i, b in enumerate(s.c) if b.timestamp >= start]
            if keep:
                s.index = {s.c[i].timestamp: i for i in keep}
                out[sym] = s
        return out

    crypto = from_start({s: Series(c, ct.RULES) for s, c in raw.items() if len(c) > 60})
    watch = list(dict.fromkeys(stk.WATCHLIST + ["SPY"]))
    raw_st = stock_bars(watch, warmup - timedelta(days=10), end)
    stocks = from_start({s: Series(c, stk.RULES) for s, c in raw_st.items() if len(c) > 60 and s in stk.WATCHLIST})
    return SimpleNamespace(
        start=start, end=end, crypto=crypto, stocks=stocks,
        main=[s for s in main if s in crypto], small=[s for s in small if s in crypto],
        rot_universe=[s for s in rot_universe if s in crypto],
        btc=hold_return([b for b in raw.get("BTC/USD", []) if b.timestamp >= start]),
        spy=hold_return([b for b in raw_st.get("SPY", []) if b.timestamp >= start]))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=6)
    months = ap.parse_args().months
    d = load(months)
    print(f"Backtest {d.start:%Y-%m-%d} to {d.end:%Y-%m-%d} ({months} months). No orders are placed.")
    book, prices = run_crypto_bot({s: d.crypto[s] for s in d.main + d.small}, d.main, d.small)
    report(book, prices, "BTC", d.btc)
    book, prices = run_rotator({s: d.crypto[s] for s in d.rot_universe})
    report(book, prices, "BTC", d.btc)
    book, prices = run_stocks(d.stocks)
    report(book, prices, "SPY", d.spy)
    print("\nApproximation: see the notes at the top of backtest.py for what is not modelled.")
    sys.exit(0)
