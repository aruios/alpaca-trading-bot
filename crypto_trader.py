from dotenv import load_dotenv
import os
import math
import sys
import time
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from datetime import datetime, timedelta, timezone
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (MarketOrderRequest, LimitOrderRequest, GetAssetsRequest,
                                     GetOrdersRequest)
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass, AssetStatus, QueryOrderStatus
from alpaca.data.historical.crypto import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from strategy import Rules, closed_candles, evaluate, exit_reason, daily_loss_hit
from analysis import write_report

# 1. Load credentials
load_dotenv()
API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

# 2. Clients (paper=True for simulation)
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client = CryptoHistoricalDataClient(API_KEY, SECRET_KEY)

# 3. Config
BUY_ENABLED = True                   # ✅ this bot does all crypto buying (the rotator is off: it lost in backtests)
BUDGET_USD = 6000                    # ✅ max total crypto exposure (paper account is $10,000)
MAIN_TRADE_USD = 750                 # ✅ $ per buy for main coins
SMALL_TRADE_USD = 250                # ✅ $ per buy for coins under $1
SMALL_COIN_BUDGET_USD = 1500         # ✅ max total exposure to small coins
SMALL_COIN_MAX_PRICE = 1.00          # ✅ "small coin" = last close at or below this
DAILY_LOSS_LIMIT = 0.03              # ✅ no new buys if account is down 3% today
LOOKBACK_DAYS = 5                    # ✅ 5 days of 30-minute candles = ~240 candles
CANDLE = TimeFrame(30, TimeFrameUnit.Minute)                          # ✅ 30-minute candles
RULES = Rules(stop_loss=0.15, take_profit=0.15, candle_minutes=30,
              min_profit=0.0075)    # ✅ -15% stop (backtest winner); signal exits only above +0.75% (fees + spread)

WATCHLIST = [
    "BTC/USD", "ETH/USD", "SOL/USD",   # ✅ core holdings
    "ZEC/USD", "XMR/USD",              # ✅ your picks (ZEC: NU7 upgrade testing)
    "AAVE/USD",                        # ✅ news 2026-10: early-October rally, DeFi leader
    "SUI/USD",                         # ✅ news 2026-10: Hashi lets BTC be used as collateral
    "QNT/USD",                         # ✅ news 2026-09: picked by The Clearing House for tokenized deposits
    "INJ/USD",                         # ✅ news 2026-10: up ~25% this week
]                                      # ✅ get_tradable() drops any Alpaca doesn't list

# ✅ Small coins: candidates usually under $1. Price is checked every run,
#    so any that climb above SMALL_COIN_MAX_PRICE are not bought.
SMALL_COIN_WATCHLIST = [
    "DOGE/USD", "SHIB/USD", "PEPE/USD", "BONK/USD", "WIF/USD",   # ✅ meme coins (very volatile)
    "ADA/USD", "ARB/USD", "POL/USD", "ONDO/USD",                 # ✅ added by Alpaca May 2026
    "GRT/USD", "BAT/USD", "CRV/USD", "XTZ/USD", "SUSHI/USD",     # ✅ older Alpaca listings (BAT up ~46-49% this week)
    "TIA/USD", "RSR/USD",                                        # ✅ news 2026-10: TIA +57% to ~$0.75, RSR +20-30%
    "BTW/USD",                                                   # ✅ news 2026-10: ~+50%, but thin volume
]


def get_tradable(watchlist):
    # ✅ Alpaca doesn't list every coin (XMR/ZEC likely missing), so check first
    assets = trading_client.get_all_assets(
        GetAssetsRequest(asset_class=AssetClass.CRYPTO, status=AssetStatus.ACTIVE)
    )
    available = {a.symbol for a in assets if a.tradable}
    skipped = [s for s in watchlist if s not in available]
    if skipped:
        print("Not tradable on Alpaca, skipped:", skipped)
    return [s for s in watchlist if s in available]


def to_pair(symbol):
    # ✅ Positions report crypto as "BTCUSD"; data API wants "BTC/USD"
    return symbol if "/" in symbol else f"{symbol[:-3]}/{symbol[-3:]}"


ROT_PREFIX = "rot-"                  # ✅ client_order_id prefix of top_crypto_rotator.py buys
ROTATOR_ENABLED = False              # ✅ keep in sync with top_crypto_rotator.ENABLED


def rotator_holding():
    """Pair top_crypto_rotator.py owns: its latest filled buy, while that position is open."""
    for o in trading_client.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.CLOSED, side=OrderSide.BUY, limit=200)):  # ✅ newest first
        if (o.client_order_id or "").startswith(ROT_PREFIX) and float(o.filled_qty or 0) > 0:
            sym = to_pair(o.symbol)
            return sym if sym in crypto_positions() else None
    return None


def crypto_positions(exclude=None):
    return {
        to_pair(p.symbol): p for p in trading_client.get_all_positions()
        if p.asset_class == AssetClass.CRYPTO and to_pair(p.symbol) != exclude
    }


def get_signals(symbols):
    if not symbols:
        return {}
    start = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    bars = data_client.get_crypto_bars(
        CryptoBarsRequest(symbol_or_symbols=symbols, timeframe=CANDLE, start=start)
    ).data
    signals = {}
    for s in symbols:
        candles = closed_candles(bars.get(s, []), RULES.candle_minutes)
        if candles:
            signals[s] = evaluate(candles, RULES)
        else:
            print(f"No candles for {s}, skipped")
    return signals


# ---------- standing take-profit orders (Alpaca watches the price 24/7) ----------

TP_PREFIX = "tp-"                    # ✅ client_order_id prefix that marks our take-profit sells


def open_orders():
    """Open crypto orders by pair: (pending buys, our take-profit sells)."""
    buys, tps = {}, {}
    for o in trading_client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)):
        if o.asset_class != AssetClass.CRYPTO:
            continue
        sym = to_pair(o.symbol)
        if o.side == OrderSide.BUY:
            buys.setdefault(sym, []).append(o)
        elif (o.client_order_id or "").startswith(TP_PREFIX) or (
                not ROTATOR_ENABLED and (o.client_order_id or "").startswith("rtp-")):  # ✅ adopt rotator leftovers
            tps.setdefault(sym, []).append(o)
    return buys, tps


def to_step(value, step, up):
    """Round value to a multiple of step (up for the sell price, down for qty)."""
    step = Decimal(str(step or "0.000000001"))
    exact = (Decimal(str(value)) / step).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * step
    out = float(exact)
    if not up and Decimal(out) > exact:                               # ✅ float rounded up: never sell more than held
        out = math.nextafter(out, 0)
    return out


def cancel_take_profits(orders):
    for o in orders:
        try:
            trading_client.cancel_order_by_id(o.id)
        except Exception as e:
            print(f"Cancel failed for {to_pair(o.symbol)} take-profit: {e}")


def place_take_profit(sym, position):
    asset = trading_client.get_asset(sym)
    qty = to_step(position.qty, asset.min_trade_increment, up=False)
    price = to_step(float(position.avg_entry_price) * (1 + RULES.take_profit), asset.price_increment, up=True)
    try:
        trading_client.submit_order(order_data=LimitOrderRequest(
            symbol=sym, qty=qty, limit_price=price, side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,                            # ✅ stays open until it fills
            client_order_id=f"{TP_PREFIX}{sym.replace('/', '')}-{int(time.time())}",
        ))
        print(f"TP   {sym} sell {qty:g} @ {price:.10g} (+{RULES.take_profit:.0%}), open until filled")
    except Exception as e:
        print(f"Take-profit order failed for {sym}: {e}")


def sync_take_profits(exclude=None):
    """Every crypto position (except the rotator's) gets one +TAKE_PROFIT limit sell for its full qty."""
    positions = crypto_positions(exclude)
    _, tps = open_orders()
    for sym, p in positions.items():
        target = float(p.avg_entry_price) * (1 + RULES.take_profit)
        orders = tps.pop(sym, [])
        ok = (len(orders) == 1
              and abs(float(orders[0].qty) / float(p.qty) - 1) < 1e-6
              and abs(float(orders[0].limit_price) / target - 1) < 0.001)
        if ok:
            continue
        if orders:                                                    # ✅ qty or entry changed: replace it
            cancel_take_profits(orders)
            time.sleep(1)
        place_take_profit(sym, p)
    for orders in tps.values():                                       # ✅ position gone: clean up leftovers
        cancel_take_profits(orders)


def buy(symbol, usd):
    order_data = MarketOrderRequest(
        symbol=symbol,
        notional=round(usd, 2),                                       # ✅ $ amount, not qty
        side=OrderSide.BUY,
        time_in_force=TimeInForce.GTC,                                # ✅ crypto supports GTC
    )
    try:
        order = trading_client.submit_order(order_data=order_data)
        print(f"BUY  {symbol} ${usd:.2f} -> {order.id}")
        return True
    except Exception as e:
        print(f"Buy failed for {symbol}: {e}")
        return False


def sell(position, why, tp_orders=()):
    if tp_orders:                                                     # ✅ free the qty the take-profit holds
        cancel_take_profits(tp_orders)
        time.sleep(1)
    try:
        trading_client.close_position(position.symbol)                # ✅ sells the full position
        print(f"SELL {to_pair(position.symbol)} ${float(position.market_value):.2f}: {why}")
        return True
    except Exception as e:
        print(f"Sell failed for {position.symbol}: {e}")
        return False


if __name__ == "__main__":
    main = get_tradable(WATCHLIST)
    small = [c for c in get_tradable(SMALL_COIN_WATCHLIST) if c not in main]
    rotator_sym = rotator_holding() if ROTATOR_ENABLED else None      # ✅ never touch the rotator's coin
    positions = crypto_positions(rotator_sym)
    pending_buys, tp_orders = open_orders()

    # ✅ Get candles for everything we might buy AND everything we hold
    signals = get_signals(sorted(set(main) | set(small) | set(positions)))
    actions = {}                                                      # ✅ what we did, for the analysis report
    held_before = dict(positions)

    def finish():
        write_report("crypto", "Crypto chart analysis", signals, actions, held_before)

    # ---- 1. SELL: check every crypto position (also ones bought by hand) ----
    sold = set()
    for sym, p in positions.items():
        why = exit_reason(p, signals.get(sym), RULES)
        if why and sell(p, why, tp_orders.get(sym, ())):
            sold.add(sym)
            actions[sym] = f"🔴 SOLD: {why.split(' (')[0]}"
        elif not why:
            pl = float(p.unrealized_plpc)
            print(f"HOLD {sym} ({pl:+.1%})")
            actions[sym] = (f"HOLD (take-profit at +{RULES.take_profit:.0%}, stop at -{RULES.stop_loss:.0%})"
                            if RULES.stop_loss else f"HOLD (take-profit at +{RULES.take_profit:.0%})")
        else:
            actions[sym] = f"SELL failed: {why.split(' (')[0]}"
    for sym in sold:
        positions.pop(sym)

    if not BUY_ENABLED:
        print("New crypto buys are done by top_crypto_rotator.py; managing existing positions only.")
        sync_take_profits(rotator_sym)
        finish()
        sys.exit(0)

    # ---- 2. BUY: only on a fresh signal, within budget ----
    if daily_loss_hit(trading_client.get_account(), DAILY_LOSS_LIMIT):
        print(f"Account down {DAILY_LOSS_LIMIT:.0%}+ today, no new buys.")
        sync_take_profits(rotator_sym)
        for sym, sig in signals.items():
            actions.setdefault(sym, "no buys today (account down 3%)" if sig.buy else "wait")
        finish()
        sys.exit(0)                                                   # ✅ normal stop, sells above already ran

    small_set = set(small)
    pending = {s: sum(float(o.notional or 0) for o in orders) for s, orders in pending_buys.items()}
    exposure = sum(float(p.market_value) for p in positions.values()) + sum(pending.values())
    small_exposure = (sum(float(p.market_value) for s, p in positions.items() if s in small_set)
                      + sum(v for s, v in pending.items() if s in small_set))
    for sym in pending:
        print(f"PENDING {sym}: unfilled buy order(s) ${pending[sym]:.2f}, not buying again")
        actions[sym] = "buy order pending"

    candidates = []
    for sym in main + small:
        sig = signals.get(sym)
        if sym in positions or sym in sold or sym in pending or sig is None:  # ✅ no double-buys, no instant re-buy
            continue
        print(f"{'BUY? ' if sig.buy else 'wait '} {sym}: {sig.reason}")
        if not sig.buy:
            continue
        if sym in small_set and sig.close > SMALL_COIN_MAX_PRICE:
            print(f"  {sym} at ${sig.close:.4f} is above ${SMALL_COIN_MAX_PRICE:.2f}, skipped")
            actions[sym] = f"skip: small coin above ${SMALL_COIN_MAX_PRICE:g}"
            continue
        candidates.append((sig.strength, sym))

    for _, sym in sorted(candidates, reverse=True):                   # ✅ strongest trend first
        is_small = sym in small_set
        usd = SMALL_TRADE_USD if is_small else MAIN_TRADE_USD
        if exposure + usd > BUDGET_USD:
            print(f"Budget full (${exposure:.2f}), skipped {sym}")
            actions[sym] = "skip: budget full"
            continue
        if is_small and small_exposure + usd > SMALL_COIN_BUDGET_USD:
            print(f"Small-coin budget full (${small_exposure:.2f}), skipped {sym}")
            actions[sym] = "skip: small-coin budget full"
            continue
        if buy(sym, usd):
            exposure += usd
            small_exposure += usd if is_small else 0
            actions[sym] = f"🟢 BOUGHT ${usd}"
        else:
            actions[sym] = "BUY failed"

    print(f"Crypto exposure after run: ${exposure:.2f} of ${BUDGET_USD}")

    # ---- 3. TAKE-PROFIT: a standing +15% sell on every position ----
    time.sleep(3)                                                     # ✅ let new market buys fill
    sync_take_profits(rotator_sym)
    finish()
