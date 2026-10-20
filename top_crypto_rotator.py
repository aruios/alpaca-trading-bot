"""
Top-performer crypto rotator for Alpaca (paper). Does all new crypto buying.
Each run: rank liquid USD crypto pairs (stablecoins excluded) by today's % change (since UTC midnight)
and hold only the #1 performer, in one position of up to BUDGET_USD.

Rules
  Buy:     leader is up at least MIN_GAIN_PCT today, not a coin crypto_trader.py holds.
  Profit:  standing GTC limit sell at entry +TAKE_PROFIT_PCT (Alpaca fills it 24/7).
  Rotate:  a new leader beats the held coin by SWITCH_MARGIN, and the held coin is in profit.
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
from alpaca.data.requests import CryptoSnapshotRequest

from crypto_trader import (trading_client as trading, data_client as data, ROT_PREFIX,
                           to_pair, to_step, crypto_positions, rotator_holding, open_orders,
                           cancel_take_profits)
from strategy import daily_loss_hit

# ---------- config ----------
BUDGET_USD       = 2000      # ✅ one position of up to this much (extra on top of the $10k cap)
ALLOC_PCT        = 0.20      # ✅ and never more than this % of cash
MIN_DOLLAR_VOL   = 10_000    # ✅ skip illiquid coins (yesterday's $ volume on Alpaca, which is small)
STABLECOINS      = {"USDC", "USDT", "USDG", "PAXG", "DAI", "PYUSD", "TUSD"}  # ✅ pegged, never "lead"
MIN_GAIN_PCT     = 2.0       # ✅ only buy if leader is up at least this much today
SWITCH_MARGIN    = 1.5       # ✅ new leader must beat current by this % (avoids churn)
TAKE_PROFIT_PCT  = 8.0
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
    leader, leader_pct, leader_price = candidates[0]

    # ✅ manage existing position first
    if held_sym:
        pos = crypto_positions()[held_sym]
        pl = float(pos.unrealized_plpc) * 100
        held = next((r for r in ranked if r[0] == held_sym), None)
        held_pct = held[1] if held else -999
        if leader != held_sym and leader_pct - held_pct >= SWITCH_MARGIN:
            if pl > 0:
                sell_all(held_sym, pos, f"rotate to {leader}", tps)
                time.sleep(3)                             # ✅ let sell settle before buying
                tps.pop(held_sym, None)
                held_sym = None
            else:
                print(f"HOLD {held_sym} ({pl:+.2f}%): {leader} leads but no selling at a loss")
        else:
            print(f"HOLD {held_sym} ({pl:+.2f}%), today {held_pct:+.2f}%")

    if held_sym is None:
        if daily_loss_hit(trading.get_account(), MAX_DAILY_LOSS):
            print(f"Account down {MAX_DAILY_LOSS:.0%}+ today, no new buys.")
        elif leader_pct < MIN_GAIN_PCT:
            print(f"Leader {leader} {leader_pct:+.2f}% is under +{MIN_GAIN_PCT}%, not buying")
        elif buy(leader, leader_price):
            time.sleep(3)                                 # ✅ let the market buy fill
            held_sym = rotator_holding()

    sync_take_profit(held_sym, tps)


if __name__ == "__main__":
    print("Rotator (PAPER)")
    tick()
    sys.exit(0)
