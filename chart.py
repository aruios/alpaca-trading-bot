"""Candle chart with the bot's EMA lines plus VWAP and ADX.

  EMA 9  - blue      EMA 21 - black      VWAP - purple (resets each day)
  ADX 14 - red, in its own panel below (0-100 scale; above 25 = strong trend)

Usage:
  python chart.py BTC/USD        # crypto, 30-minute candles (same as crypto_trader.py)
  python chart.py NVDA           # stock, hourly candles (same as stock_trader.py)
  python chart.py ETH/USD 3      # optional: days of history (default 3)

Saves charts/<SYMBOL>.png
"""
from dotenv import load_dotenv
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from alpaca.data.historical.crypto import CryptoHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
from alpaca.data.enums import DataFeed
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
import matplotlib
matplotlib.use("Agg")                                         # ✅ no window needed, just save a PNG
import matplotlib.pyplot as plt

from strategy import Rules, closed_candles, ema, vwap, adx

load_dotenv()
API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

RULES = Rules()                                               # ✅ ema 9/21, adx 14 (same as the bot)
WARMUP = 2                                                    # ✅ extra days so the EMAs are settled on screen


def get_candles(symbol, days):
    start = datetime.now(timezone.utc) - timedelta(days=days + WARMUP)
    if "/" in symbol:                                         # ✅ crypto pairs look like BTC/USD
        minutes = 30
        bars = CryptoHistoricalDataClient(API_KEY, SECRET_KEY).get_crypto_bars(
            CryptoBarsRequest(symbol_or_symbols=symbol,
                              timeframe=TimeFrame(30, TimeFrameUnit.Minute), start=start)
        ).data
    else:
        minutes = 60
        start -= timedelta(days=WARMUP * 2)                   # ✅ stocks: fewer candles per day
        bars = StockHistoricalDataClient(API_KEY, SECRET_KEY).get_stock_bars(
            StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Hour,
                             start=start, feed=DataFeed.IEX)
        ).data
    return closed_candles(bars.get(symbol, []), minutes), minutes


def plot(symbol, candles, minutes, days):
    closes = [b.close for b in candles]
    fast, slow = ema(closes, RULES.ema_fast), ema(closes, RULES.ema_slow)
    tz = "UTC" if "/" in symbol else "America/New_York"       # ✅ stock day = NY session
    vw, ax_line = vwap(candles, tz), adx(candles, RULES.adx_period)
    tz = ZoneInfo(tz)

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    first = next((i for i, b in enumerate(candles) if b.timestamp >= cutoff), 0)
    candles, fast, slow = candles[first:], fast[first:], slow[first:]
    vw, ax_line = vw[first:], ax_line[first:]
    x = range(len(candles))                                   # ✅ index, so stock nights/weekends leave no gaps

    fig, (ax, ax2) = plt.subplots(2, 1, figsize=(14, 9), sharex=True,
                                  gridspec_kw={"height_ratios": [3, 1]})
    for i, b in zip(x, candles):
        color = "#26a69a" if b.close >= b.open else "#ef5350"
        ax.vlines(i, b.low, b.high, color=color, linewidth=1)
        ax.bar(i, abs(b.close - b.open) or (b.high - b.low) * 0.01,
               bottom=min(b.open, b.close), width=0.6, color=color)

    ax.plot(x, fast, color="blue", linewidth=1.5, label=f"EMA {RULES.ema_fast}")
    ax.plot(x, slow, color="black", linewidth=1.5, label=f"EMA {RULES.ema_slow}")
    days_of = [b.timestamp.astimezone(tz).date() for b in candles]
    starts = [i for i in x if i == 0 or days_of[i] != days_of[i - 1]] + [len(candles)]
    for n, (a, b) in enumerate(zip(starts, starts[1:])):        # ✅ one segment per day, no jump at reset
        ax.plot(x[a:b], vw[a:b], color="purple", linewidth=1.5, label="VWAP" if n == 0 else None)

    ax2.plot(x, ax_line, color="red", linewidth=1.5, label=f"ADX {RULES.adx_period}")
    ax2.axhline(RULES.adx_min, color="gray", linestyle="--", linewidth=1)   # ✅ trend-strength line
    ax2.set_ylim(0, 100)
    ax2.grid(alpha=0.3)
    ax2.legend(loc="upper left")

    step = max(len(candles) // 10, 1)
    ax2.set_xticks(list(x)[::step])
    ax2.set_xticklabels([b.timestamp.strftime("%m-%d %H:%M") for b in candles][::step],
                       rotation=30, ha="right")
    ax.set_title(f"{symbol}  {minutes}-minute candles (UTC)")
    ax.grid(alpha=0.3)
    ax.legend(loc="upper left")
    fig.tight_layout()

    os.makedirs("charts", exist_ok=True)
    path = os.path.join("charts", symbol.replace("/", "") + ".png")
    fig.savefig(path, dpi=110)
    print(f"Saved {path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    symbol = sys.argv[1].upper()
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    candles, minutes = get_candles(symbol, days)
    if len(candles) < RULES.ema_slow:
        sys.exit(f"Only {len(candles)} candles for {symbol}, need {RULES.ema_slow}+")
    plot(symbol, candles, minutes, days)
