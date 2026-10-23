"""
DISABLED (ENABLED = False): backtests lost money in every version (12 months: -33% with no
stop, worse with stops), so crypto_trader.py does all crypto buying again. Kept for backtest.py.

Top-performer crypto rotator for Alpaca (paper).
Each run: rank liquid USD crypto pairs (stablecoins excluded) by today's % change (since UTC midnight)
and hold only the #1 performer, in one position of up to BUDGET_USD.

Rules
  Buy:     the best coin up at least MIN_GAIN_PCT today whose 30-minute chart confirms the move
           (EMA 9 > EMA 21, close > today's VWAP, RSI 14 < RSI_MAX), not a coin crypto_trader.py holds.
  Profit:  standing GTC limit sell at entry +TAKE_PROFIT_PCT (Alpaca fills it 24/7).
  Rotate:  that pick beats the held coin by SWITCH_MARGIN today, and the held coin is up > MIN_ROTATE_PROFIT_PCT.
  Loss:    never sells below entry (owner's crypto rule); a loser is held until positive.
  Daily:   account down MAX_DAILY_LOSS today -> no new buys (nothing is sold).

Runs once per call: runner.py (Mac, every 15 min) and GitHub Actions (backup).
Its orders are tagged rot- (buys) and rtp- (take-profit), so crypto_trader.py leaves its coin alone.
"""
import sys
import time
from datetime import datetime, timedelta, timezone

from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, GetAssetsRequest
from alpaca.trading.enums import AssetClass, AssetStatus, OrderSide, TimeInForce
from alpaca.data.requests import CryptoSnapshotRequest, CryptoBarsRequest

from crypto_trader import (trading_client as trading, data_client as data, ROT_PREFIX, CANDLE,
                           RULES as CHART_RULES,
                           to_pair, to_step, crypto_positions, rotator_holding, open_orders,
                           cancel_take_profits)
from strategy import daily_loss_hit, closed_candles, ema, rsi, vwap

# ---------- config ----------
ENABLED          = False     # ✅ off: lost money in backtests. Also set crypto_trader.ROTATOR_ENABLED
BUDGET_USD       = 2000      # ✅ one position of up to this much (extra on top of the $10k cap)
ALLOC_PCT        = 0.20      # ✅ and never more than this % of cash
MIN_DOLLAR_VOL   = 10_000    # ✅ skip illiquid coins (yesterday's $ volume on Alpaca, which is small)
STABLECOINS      = {"USDC", "USDT", "USDG", "PAXG", "DAI", "PYUSD", "TUSD"}  # ✅ pegged, never "lead"
MIN_GAIN_PCT     = 2.0       # ✅ only buy if leader is up at least this much today
SWITCH_MARGIN    = 1.5       # ✅ new leader must beat current by this % (avoids churn)
TAKE_PROFIT_PCT  = 8.0
MIN_ROTATE_PROFIT_PCT = 0.75 # ✅ rotate only above this: covers ~0.25% fee each way + spread
RSI_MAX          = 70        # ✅ chart check: skip a leader that is already overbought
CHART_DAYS       = 3         # ✅ 30-minute candles to load for the chart check (~144)
MAX_DAILY_LOSS   = 0.03      # ✅ no new buys if equity is down 3% today
STALE_BUY_MINUTES = 30       # ✅ cancel a rotator buy still unfilled after this long
RTP_PREFIX       = "rtp-"    # ✅ client_order_id prefix of the rotator's take-profit sells


def usd_pairs():
    assets = trading.get_all_assets(
        GetAssetsRequest(asset_class=AssetClass.CRYPTO, status=AssetStatus.ACTIVE))
    return [a.symbol for a in assets if a.tradable and a.symbol.endswith("/USD")  # ✅ USD only
            and a.symbol.split("/")[0] not in STABLECOINS]


def rank_today(symbols):
    """Return [(symbol, pct_change, price)] sorted best-first."""
    snaps = data.get_crypto_snapshot(CryptoSnapshotRequest(symbol_or_symbols=symbols))
    rows = []
    for sym, s in snaps.items():
        if not (s and s.daily_bar and s.latest_trade):
            continue  # ✅ missing data -> skip, don't guess
        price = s.latest_trade.price
        open_ = s.daily_bar.open                          # day = UTC midnight
        prev = s.previous_daily_bar                       # ✅ full day, so early-UTC hours aren't "illiquid"
        dollar_vol = prev.volume * prev.close if prev else 0
        if open_ <= 0 or dollar_vol < MIN_DOLLAR_VOL:
            continue
        rows.append((sym, (price / open_ - 1) * 100, price))
    return sorted(rows, key=lambda r: r[1], reverse=True)


def chart_checks(symbols):
    """{symbol: (ok, reason)}: is the move still going on the 30-minute chart?"""
    if not symbols:
        return {}
    bars = data.get_crypto_bars(CryptoBarsRequest(
        symbol_or_symbols=symbols, timeframe=CANDLE,
        start=datetime.now(timezone.utc) - timedelta(days=CHART_DAYS))).data
    out = {}
    for sym in symbols:
        candles = closed_candles(bars.get(sym, []), CHART_RULES.candle_minutes)
        closes = [b.close for b in candles]
        if len(closes) < CHART_RULES.ema_slow + 1:
            out[sym] = (False, f"only {len(closes)} candles")
            continue
        fast = ema(closes, CHART_RULES.ema_fast)[-1]
        slow = ema(closes, CHART_RULES.ema_slow)[-1]
        rsi_now = rsi(closes, CHART_RULES.rsi_period)
        vw = vwap(candles, CHART_RULES.vwap_tz)[-1]
        checks = [(fast > slow, f"ema{CHART_RULES.ema_fast}{'>' if fast > slow else '<'}ema{CHART_RULES.ema_slow}"),
                  (vw is not None and closes[-1] > vw, f"{'above' if vw and closes[-1] > vw else 'below'} vwap"),
                  (rsi_now is not None and rsi_now < RSI_MAX, f"rsi {rsi_now:.0f}" if rsi_now else "rsi n/a")]
        out[sym] = (all(ok for ok, _ in checks),
                    ", ".join(("✓ " if ok else "✗ ") + text for ok, text in checks))
    return out


def rotator_orders():
    """Open rotator orders: (pending rot- buys, rtp- take-profit sells by pair)."""
    buys, _ = open_orders()
    pending = [o for orders in buys.values() for o in orders
               if (o.client_order_id or "").startswith(ROT_PREFIX)]
    tps = {}
    for o in trading.get_orders():                        # ✅ default = open orders
        if o.side == OrderSide.SELL and (o.client_order_id or "").startswith(RTP_PREFIX):
            tps.setdefault(to_pair(o.symbol), []).append(o)
    return pending, tps


def buy(sym, price):
    cash = float(trading.get_account().cash)
    notional = round(min(BUDGET_USD, cash * ALLOC_PCT), 2)
    if notional < 10:
        print("Not enough cash"); return False
    cid = f"{ROT_PREFIX}{sym.replace('/', '')}-{int(time.time())}"  # ✅ idempotent retry key
    trading.submit_order(MarketOrderRequest(
        symbol=sym, notional=notional, side=OrderSide.BUY,
        time_in_force=TimeInForce.GTC, client_order_id=cid))
    print(f"BUY  {sym} ${notional} @ ~{price:.6g}")
    return True


def sell_all(sym, pos, reason, tps):
    cancel_take_profits(tps.get(sym, []))                 # ✅ free the qty the take-profit holds
    time.sleep(1)
    trading.close_position(pos.symbol)                    # ✅ position symbol has no slash, e.g. BTCUSD
    print(f"SELL {sym} ({reason}) P/L {float(pos.unrealized_plpc)*100:+.2f}%")


def sync_take_profit(sym, tps):
    """The held coin gets exactly one limit sell at entry +TAKE_PROFIT_PCT; leftovers are cancelled."""
    for other, orders in tps.items():
        if other != sym:
            cancel_take_profits(orders)
    if sym is None:
        return
    pos = crypto_positions().get(sym)
    if pos is None:
        return
    target = float(pos.avg_entry_price) * (1 + TAKE_PROFIT_PCT / 100)
    orders = tps.get(sym, [])
    if (len(orders) == 1 and abs(float(orders[0].qty) / float(pos.qty) - 1) < 1e-6
            and abs(float(orders[0].limit_price) / target - 1) < 0.001):
        return
    if orders:
        cancel_take_profits(orders)
        time.sleep(1)
    asset = trading.get_asset(sym)
    qty = to_step(pos.qty, asset.min_trade_increment, up=False)
    price = to_step(target, asset.price_increment, up=True)
    try:
        trading.submit_order(LimitOrderRequest(
            symbol=sym, qty=qty, limit_price=price, side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            client_order_id=f"{RTP_PREFIX}{sym.replace('/', '')}-{int(time.time())}"))
        print(f"TP   {sym} sell {qty:g} @ {price:.10g} (+{TAKE_PROFIT_PCT:g}%), open until filled")
    except Exception as e:
        print(f"Take-profit order failed for {sym}: {e}")


def tick():
    held_sym = rotator_holding()
    pending, tps = rotator_orders()
    now = datetime.now(timezone.utc)
    for o in pending:
        if now - o.created_at > timedelta(minutes=STALE_BUY_MINUTES):  # ✅ stuck order: drop it, try again
            trading.cancel_order_by_id(o.id)
            print(f"Cancelled unfilled buy {to_pair(o.symbol)} from {o.created_at:%H:%M} UTC")
    pending = [o for o in pending if now - o.created_at <= timedelta(minutes=STALE_BUY_MINUTES)]
    if pending:
        print(f"Waiting for buy order(s) to fill: {[to_pair(o.symbol) for o in pending]}")
        return

    ranked = rank_today(usd_pairs())
    if not ranked:
        print("No data"); sync_take_profit(held_sym, tps); return
    print("Top 3: " + ", ".join(f"{s} {p:+.2f}%" for s, p, _ in ranked[:3]))

    others = set(crypto_positions(held_sym))              # ✅ crypto_trader.py's coins: never buy these
    candidates = [r for r in ranked if r[0] not in others]
    if not candidates:
        print("Every leader is already held by crypto_trader.py"); sync_take_profit(held_sym, tps); return
    # ✅ pick: best coin up >= MIN_GAIN_PCT today whose chart confirms the move
    risers = [r for r in candidates if r[1] >= MIN_GAIN_PCT and r[0] != held_sym]
    charts = chart_checks([s for s, _, _ in risers])
    pick = None
    for sym, pct, price in risers:
        ok, why = charts[sym]
        print(f"{'CHART ok' if ok else 'CHART no'} {sym} {pct:+.2f}%: {why}")
        if ok:
            pick = (sym, pct, price)
            break
    if not risers:
        print(f"No coin up +{MIN_GAIN_PCT}% today besides the one held")

    # ✅ manage existing position first
    if held_sym:
        pos = crypto_positions()[held_sym]
        pl = float(pos.unrealized_plpc) * 100
        held = next((r for r in ranked if r[0] == held_sym), None)
        held_pct = held[1] if held else -999
        if pick and pick[1] - held_pct >= SWITCH_MARGIN:
            if pl > MIN_ROTATE_PROFIT_PCT:
                sell_all(held_sym, pos, f"rotate to {pick[0]}", tps)
                time.sleep(3)                             # ✅ let sell settle before buying
                tps.pop(held_sym, None)
                held_sym = None
            else:
                print(f"HOLD {held_sym} ({pl:+.2f}%): {pick[0]} leads, but rotating needs > +{MIN_ROTATE_PROFIT_PCT}% (no selling at a loss)")
        else:
            print(f"HOLD {held_sym} ({pl:+.2f}%), today {held_pct:+.2f}%")

    if held_sym is None:
        if daily_loss_hit(trading.get_account(), MAX_DAILY_LOSS):
            print(f"Account down {MAX_DAILY_LOSS:.0%}+ today, no new buys.")
        elif pick is None:
            print("Nothing to buy: no riser with a confirming chart")
        elif buy(pick[0], pick[2]):
            time.sleep(3)                                 # ✅ let the market buy fill
            held_sym = rotator_holding()

    sync_take_profit(held_sym, tps)


if __name__ == "__main__":
    if not ENABLED:
        print("Rotator is disabled (ENABLED = False); no orders placed.")
        sys.exit(0)
    print("Rotator (PAPER)")
    tick()
    sys.exit(0)
