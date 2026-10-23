"""Shared candle logic for crypto_trader.py and stock_trader.py.

Buy signal (all must be true on the last CLOSED candle):
  1. Trend:     close is above the slow trend SMA (default 50 candles)
  2. Momentum:  fast EMA (9) crossed above slow EMA (21) within the last few candles
  3. Not stretched: RSI(14) is below RSI_MAX (default 70)
  4. Strong trend:  ADX(14) is at or above ADX_MIN (default 25)
  5. Above VWAP:    close is above today's VWAP (buyers in control today)

Sell (any one is enough):
  1. Stop-loss:    position is down STOP_LOSS or more from your entry price
  2. Take-profit:  position is up TAKE_PROFIT or more
  3. Signal exit:  fast EMA drops below slow EMA, or close falls below the trend SMA

stop_loss=None turns 1 off. min_profit makes 3 fire only above that gain (crypto: +0.75%,
covering Alpaca's ~0.25% fee each way and the spread); None = signal exit at any P/L.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


@dataclass
class Rules:
    ema_fast: int = 9
    ema_slow: int = 21
    trend_sma: int = 50
    rsi_period: int = 14
    rsi_max: float = 70.0
    adx_period: int = 14
    adx_min: float = 25.0          # ✅ ADX below this = choppy market, no buy
    vwap_tz: str = "UTC"           # ✅ VWAP restarts at midnight in this timezone
    cross_lookback: int = 3        # ✅ a cross in the last N candles still counts (survives a missed run)
    stop_loss: float = 0.07        # ✅ 0.07 = sell at -7%; None = no stop-loss
    take_profit: float = 0.10      # ✅ 0.10 = sell at +10%
    candle_minutes: int = 60       # ✅ 60 = hourly candles
    min_profit: float = None       # ✅ signal exits only above this gain; None = at any P/L


# ---------- indicators (plain Python, no pandas needed) ----------

def ema(values, n):
    """EMA series aligned to values; None until enough data."""
    if len(values) < n:
        return [None] * len(values)
    k = 2 / (n + 1)
    e = sum(values[:n]) / n                                  # ✅ seed with SMA
    out = [None] * (n - 1) + [e]
    for v in values[n:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def sma(values, n):
    return sum(values[-n:]) / n if len(values) >= n else None


def rsi(values, n=14):
    """Wilder's RSI of the last value."""
    if len(values) <= n:
        return None
    gains = losses = 0.0
    for a, b in zip(values[:n], values[1:n + 1]):
        d = b - a
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / n, losses / n
    for a, b in zip(values[n:], values[n + 1:]):
        d = b - a
        avg_g = (avg_g * (n - 1) + max(d, 0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0)) / n
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def vwap(candles, tz):
    """VWAP that restarts at the start of each day in timezone tz (name)."""
    tz = ZoneInfo(tz)
    out, day, pv, vol = [], None, 0.0, 0.0
    for b in candles:
        d = b.timestamp.astimezone(tz).date()
        if d != day:
            day, pv, vol = d, 0.0, 0.0
        pv += (b.high + b.low + b.close) / 3 * b.volume
        vol += b.volume
        out.append(pv / vol if vol else None)
    return out


def adx(candles, n=14):
    """Wilder's ADX series aligned to candles; None until enough data."""
    out = [None] * len(candles)
    if len(candles) < 2 * n + 1:
        return out
    trs, pdms, ndms = [], [], []
    for prev, b in zip(candles, candles[1:]):
        up, down = b.high - prev.high, prev.low - b.low
        pdms.append(up if up > down and up > 0 else 0.0)
        ndms.append(down if down > up and down > 0 else 0.0)
        trs.append(max(b.high - b.low, abs(b.high - prev.close), abs(b.low - prev.close)))
    tr, pdm, ndm = sum(trs[:n]), sum(pdms[:n]), sum(ndms[:n])
    dxs = []
    for i in range(n, len(trs) + 1):
        if i > n:
            tr = tr - tr / n + trs[i - 1]
            pdm = pdm - pdm / n + pdms[i - 1]
            ndm = ndm - ndm / n + ndms[i - 1]
        pdi, ndi = (100 * pdm / tr, 100 * ndm / tr) if tr else (0.0, 0.0)
        dxs.append(100 * abs(pdi - ndi) / (pdi + ndi) if pdi + ndi else 0.0)
    a = sum(dxs[:n]) / n
    out[2 * n - 1] = a                                        # ✅ dxs[k] belongs to candle k + n
    for k in range(n, len(dxs)):
        a = (a * (n - 1) + dxs[k]) / n
        out[k + n] = a
    return out


# ---------- candles ----------

def closed_candles(bars, candle_minutes, now=None):
    """Drop the candle that is still forming, so signals never repaint."""
    now = now or datetime.now(timezone.utc)
    return [b for b in bars if b.timestamp + timedelta(minutes=candle_minutes) <= now]


# ---------- signals ----------

@dataclass
class Signal:
    buy: bool
    sell: bool
    close: float
    strength: float      # ✅ % above trend SMA, used to rank buy candidates
    reason: str


def evaluate(candles, r: Rules) -> Signal:
    closes = [b.close for b in candles]
    need = max(r.trend_sma, r.ema_slow + r.cross_lookback, r.rsi_period + 1, 2 * r.adx_period + 1)
    if len(closes) < need:
        return Signal(False, False, closes[-1] if closes else 0.0, 0.0,
                      f"only {len(closes)} candles, need {need}")

    close = closes[-1]
    trend = sma(closes, r.trend_sma)
    fast, slow = ema(closes, r.ema_fast), ema(closes, r.ema_slow)
    rsi_now = rsi(closes, r.rsi_period)
    adx_now = adx(candles, r.adx_period)[-1]
    vwap_now = vwap(candles, r.vwap_tz)[-1]

    above_now = fast[-1] > slow[-1]
    fresh_cross = above_now and any(
        fast[-i - 1] <= slow[-i - 1] for i in range(1, r.cross_lookback + 1)
    )
    uptrend = close > trend
    strength = close / trend - 1

    strong = adx_now is not None and adx_now >= r.adx_min
    above_vwap = vwap_now is not None and close > vwap_now

    buy = uptrend and fresh_cross and rsi_now < r.rsi_max and strong and above_vwap
    sell = (not above_now) or (not uptrend)

    reason = (f"close={close:.6g} sma{r.trend_sma}={trend:.6g} "
              f"ema{r.ema_fast}{'>' if above_now else '<'}ema{r.ema_slow} "
              f"cross={'yes' if fresh_cross else 'no'} rsi={rsi_now:.1f} "
              f"adx={adx_now or 0:.1f} vwap={vwap_now or 0:.6g}")
    return Signal(buy, sell, close, strength, reason)


def exit_reason(position, signal: Signal, r: Rules):
    """Return why to sell this position, or None to keep holding."""
    pl = float(position.unrealized_plpc)                     # ✅ e.g. -0.07 = -7%
    if r.stop_loss is not None and pl <= -r.stop_loss:
        return f"stop-loss ({pl:+.1%})"
    if pl >= r.take_profit:
        return f"take-profit ({pl:+.1%})"
    if r.min_profit is not None and pl <= r.min_profit:
        return None                                          # ✅ not clearly in profit: hold
    if signal is not None and signal.sell:
        return f"signal exit ({pl:+.1%}; {signal.reason})"
    return None


def daily_loss_hit(account, limit):
    """True when account equity is down `limit` (e.g. 0.03) since yesterday's close."""
    eq, last = float(account.equity), float(account.last_equity)
    return last > 0 and (eq - last) / last <= -limit
