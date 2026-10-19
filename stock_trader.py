from dotenv import load_dotenv
import os
import sys
from datetime import datetime, timedelta, timezone
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass
from alpaca.data.historical.screener import ScreenerClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import MarketMoversRequest, StockBarsRequest
from alpaca.data.enums import MarketType, DataFeed
from alpaca.data.timeframe import TimeFrame

from strategy import Rules, closed_candles, evaluate, exit_reason, daily_loss_hit

# 1. Load credentials
load_dotenv()
API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

# 2. Clients (paper=True for simulation)
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
screener = ScreenerClient(API_KEY, SECRET_KEY)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

# 3. Config
BUDGET_USD = 4000             # ✅ max total stock exposure (crypto 6,000 + stocks 4,000 = $10,000 account)
TRADE_USD = 500               # ✅ $ per buy
WATCHLIST = []                # ✅ e.g. ["NVDA", "AAPL"]; empty = scan today's top gainers
MIN_STOCK_PRICE = 5.0         # ✅ skip penny stocks
TRENDING_TOP = 15             # ✅ gainers to scan; candles decide which (if any) to buy
DAILY_LOSS_LIMIT = 0.03       # ✅ no new buys if account is down 3% today
LOOKBACK_DAYS = 30            # ✅ ~6.5 hourly candles per trading day, need 50+
FEED = DataFeed.IEX           # ✅ free data plan; use DataFeed.SIP if you pay for it
RULES = Rules(stop_loss=0.07, take_profit=0.10, candle_minutes=60,
              vwap_tz="America/New_York")   # ✅ VWAP restarts each NY trading day
DONT_MANAGE = set()           # ✅ stocks the bot must never sell, e.g. {"SPY"} if bot.py owns it


def get_trending(top):
    # ✅ Top % gainers, filtered by minimum price
    movers = screener.get_market_movers(
        MarketMoversRequest(top=50, market_type=MarketType.STOCKS)
    )
    return [m.symbol for m in movers.gainers if m.price >= MIN_STOCK_PRICE][:top]


def get_tradable(watchlist):
    tradable = []
    for s in watchlist:
        try:
            a = trading_client.get_asset(s)
            if a.tradable and a.fractionable:                         # ✅ notional needs fractionable
                tradable.append(s)
            else:
                print(f"{s} not tradable/fractionable, skipped")
        except Exception:
            print(f"{s} not found, skipped")
    return tradable


def stock_positions():
    return {
        p.symbol: p for p in trading_client.get_all_positions()
        if p.asset_class == AssetClass.US_EQUITY and p.symbol not in DONT_MANAGE
    }


def get_signals(symbols):
    if not symbols:
        return {}
    start = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    bars = data_client.get_stock_bars(
        StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame.Hour,
                         start=start, feed=FEED)
    ).data
    signals = {}
    for s in symbols:
        candles = closed_candles(bars.get(s, []), RULES.candle_minutes)
        if candles:
            signals[s] = evaluate(candles, RULES)
        else:
            print(f"No candles for {s}, skipped")
    return signals


def buy(symbol, usd):
    order_data = MarketOrderRequest(
        symbol=symbol,
        notional=round(usd, 2),                                       # ✅ $ amount, not qty
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,                                # ✅ notional stock orders require DAY
    )
    try:
        order = trading_client.submit_order(order_data=order_data)
        print(f"BUY  {symbol} ${usd:.2f} -> {order.id}")
        return True
    except Exception as e:
        print(f"Buy failed for {symbol}: {e}")
        return False


def sell(position, why):
    try:
        trading_client.close_position(position.symbol)                # ✅ sells the full position, fractions too
        print(f"SELL {position.symbol} ${float(position.market_value):.2f}: {why}")
        return True
    except Exception as e:
        print(f"Sell failed for {position.symbol}: {e}")
        return False


if __name__ == "__main__":
    clock = trading_client.get_clock()
    if not clock.is_open:                                             # ✅ avoid queued off-hours orders
        print(f"Market closed, nothing to do. Next open: {clock.next_open} "
              "(regular hours are 9:30 AM to 4:00 PM ET, weekdays).")
        sys.exit(0)                                                   # ✅ normal stop, not a failed run

    universe = get_tradable(WATCHLIST or get_trending(TRENDING_TOP))
    positions = stock_positions()
    signals = get_signals(sorted(set(universe) | set(positions)))

    # ---- 1. SELL: check every stock position the bot manages ----
    sold = set()
    for sym, p in positions.items():
        why = exit_reason(p, signals.get(sym), RULES)
        if why and sell(p, why):
            sold.add(sym)
        elif not why:
            print(f"HOLD {sym} ({float(p.unrealized_plpc):+.1%})")
    for sym in sold:
        positions.pop(sym)

    # ---- 2. BUY: only on a fresh signal, within budget ----
    if daily_loss_hit(trading_client.get_account(), DAILY_LOSS_LIMIT):
        print(f"Account down {DAILY_LOSS_LIMIT:.0%}+ today, no new buys.")
        sys.exit(0)                                                   # ✅ normal stop, sells above already ran

    exposure = sum(float(p.market_value) for p in positions.values())
    candidates = []
    for sym in universe:
        sig = signals.get(sym)
        if sym in positions or sym in sold or sym in DONT_MANAGE or sig is None:
            continue
        print(f"{'BUY? ' if sig.buy else 'wait '} {sym}: {sig.reason}")
        if sig.buy:
            candidates.append((sig.strength, sym))

    for _, sym in sorted(candidates, reverse=True):                   # ✅ strongest trend first
        if exposure + TRADE_USD > BUDGET_USD:
            print(f"Budget full (${exposure:.2f}), skipped {sym}")
            continue
        if buy(sym, TRADE_USD):
            exposure += TRADE_USD

    print(f"Stock exposure after run: ${exposure:.2f} of ${BUDGET_USD}")
