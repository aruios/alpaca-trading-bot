"""Shared candle logic for crypto_trader.py and stock_trader.py.

Buy signal (all must be true on the last CLOSED candle):
  1. Trend:     close is above the slow trend SMA (default 50 candles)
  2. Momentum:  fast EMA (9) crossed above slow EMA (21) within the last few candles
  3. Not stretched: RSI(14) is below RSI_MAX (default 70)

Sell (any one is enough):
  1. Stop-loss:    position is down STOP_LOSS or more from your entry price
  2. Take-profit:  position is up TAKE_PROFIT or more
  3. Signal exit:  fast EMA drops below slow EMA, or close falls below the trend SMA

With sell_at_loss=False (crypto), 1 is skipped and 3 only fires while the
position is in profit, so a losing position is held until it turns positive.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass
class Rules:
    ema_fast: int = 9
    ema_slow: int = 21
    trend_sma: int = 50
    rsi_period: int = 14
    rsi_max: float = 70.0
    cross_lookback: int = 3        # ✅ a cross in the last N candles still counts (survives a missed run)
    stop_loss: float = 0.07        # ✅ 0.07 = sell at -7%
    take_profit: float = 0.10      # ✅ 0.10 = sell at +10%
    candle_minutes: int = 60       # ✅ 60 = hourly candles
    sell_at_loss: bool = True      # ✅ False = never sell below entry; hold until in profit


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


def evaluate(closes, r: Rules) -> Signal:
    need = max(r.trend_sma, r.ema_slow + r.cross_lookback, r.rsi_period + 1)
    if len(closes) < need:
        return Signal(False, False, closes[-1] if closes else 0.0, 0.0,
                      f"only {len(closes)} candles, need {need}")

    close = closes[-1]
    trend = sma(closes, r.trend_sma)
    fast, slow = ema(closes, r.ema_fast), ema(closes, r.ema_slow)
    rsi_now = rsi(closes, r.rsi_period)

    above_now = fast[-1] > slow[-1]
    fresh_cross = above_now and any(
        fast[-i - 1] <= slow[-i - 1] for i in range(1, r.cross_lookback + 1)
    )
    uptrend = close > trend
    strength = close / trend - 1

    buy = uptrend and fresh_cross and rsi_now < r.rsi_max
    sell = (not above_now) or (not uptrend)

    reason = (f"close={close:.6g} sma{r.trend_sma}={trend:.6g} "
              f"ema{r.ema_fast}{'>' if above_now else '<'}ema{r.ema_slow} "
              f"cross={'yes' if fresh_cross else 'no'} rsi={rsi_now:.1f}")
    return Signal(buy, sell, close, strength, reason)


def exit_reason(position, signal: Signal, r: Rules):
    """Return why to sell this position, or None to keep holding."""
    pl = float(position.unrealized_plpc)                     # ✅ e.g. -0.07 = -7%
    if pl <= -r.stop_loss and r.sell_at_loss:
        return f"stop-loss ({pl:+.1%})"
    if pl >= r.take_profit:
        return f"take-profit ({pl:+.1%})"
    if pl <= 0 and not r.sell_at_loss:
        return None                                          # ✅ in loss: hold, wait for profit
    if signal is not None and signal.sell:
        return f"signal exit ({pl:+.1%}; {signal.reason})"
    return None


def daily_loss_hit(account, limit):
    """True when account equity is down `limit` (e.g. 0.03) since yesterday's close."""
    eq, last = float(account.equity), float(account.last_equity)
    return last > 0 and (eq - last) / last <= -limit
