from dotenv import load_dotenv
import os
import sys
from datetime import datetime, timedelta, timezone
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetAssetsRequest
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass, AssetStatus
from alpaca.data.historical.crypto import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from strategy import Rules, closed_candles, evaluate, exit_reason, daily_loss_hit

# 1. Load credentials
load_dotenv()
API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

# 2. Clients (paper=True for simulation)
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client = CryptoHistoricalDataClient(API_KEY, SECRET_KEY)

# 3. Config
BUDGET_USD = 600                     # ✅ max total crypto exposure (paper account is $1,000)
MAIN_TRADE_USD = 150                 # ✅ $ per buy for main coins
SMALL_TRADE_USD = 50                 # ✅ $ per buy for coins under $1
SMALL_COIN_BUDGET_USD = 150          # ✅ max total exposure to small coins
SMALL_COIN_MAX_PRICE = 1.00          # ✅ "small coin" = last close at or below this
DAILY_LOSS_LIMIT = 0.03              # ✅ no new buys if account is down 3% today
LOOKBACK_DAYS = 5                    # ✅ 5 days of 30-minute candles = ~240 candles
CANDLE = TimeFrame(30, TimeFrameUnit.Minute)                          # ✅ 30-minute candles
RULES = Rules(take_profit=0.15, candle_minutes=30,
              sell_at_loss=False)                                      # ✅ crypto: only sell in profit

WATCHLIST = [
    "BTC/USD", "ETH/USD", "SOL/USD",   # ✅ core holdings
    "ZEC/USD", "XMR/USD",              # ✅ your picks (ZEC: NU7 upgrade testing)
    "AAVE/USD",                        # ✅ news 2026-10: early-October rally, DeFi leader
    "SUI/USD",                         # ✅ news 2026-10: Hashi lets BTC be used as collateral
    "QNT/USD",                         # ✅ news 2026-09: picked by The Clearing House for tokenized deposits
]                                      # ✅ get_tradable() drops any Alpaca doesn't list

# ✅ Small coins: candidates usually under $1. Price is checked every run,
#    so any that climb above SMALL_COIN_MAX_PRICE are not bought.
SMALL_COIN_WATCHLIST = [
    "DOGE/USD", "SHIB/USD", "PEPE/USD", "BONK/USD", "WIF/USD",   # ✅ meme coins (very volatile)
    "ADA/USD", "ARB/USD", "POL/USD", "ONDO/USD",                 # ✅ added by Alpaca May 2026
    "GRT/USD", "BAT/USD", "CRV/USD", "XTZ/USD", "SUSHI/USD",     # ✅ older Alpaca listings
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


def crypto_positions():
    return {
        to_pair(p.symbol): p for p in trading_client.get_all_positions()
        if p.asset_class == AssetClass.CRYPTO
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


def sell(position, why):
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
    positions = crypto_positions()

    # ✅ Get candles for everything we might buy AND everything we hold
    signals = get_signals(sorted(set(main) | set(small) | set(positions)))

    # ---- 1. SELL: check every crypto position (also ones bought by hand) ----
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

    small_set = set(small)
    exposure = sum(float(p.market_value) for p in positions.values())
    small_exposure = sum(float(p.market_value) for s, p in positions.items() if s in small_set)

    candidates = []
    for sym in main + small:
        sig = signals.get(sym)
        if sym in positions or sym in sold or sig is None:            # ✅ no double-buys, no instant re-buy
            continue
        print(f"{'BUY? ' if sig.buy else 'wait '} {sym}: {sig.reason}")
        if not sig.buy:
            continue
        if sym in small_set and sig.close > SMALL_COIN_MAX_PRICE:
            print(f"  {sym} at ${sig.close:.4f} is above ${SMALL_COIN_MAX_PRICE:.2f}, skipped")
            continue
        candidates.append((sig.strength, sym))

    for _, sym in sorted(candidates, reverse=True):                   # ✅ strongest trend first
        is_small = sym in small_set
        usd = SMALL_TRADE_USD if is_small else MAIN_TRADE_USD
        if exposure + usd > BUDGET_USD:
            print(f"Budget full (${exposure:.2f}), skipped {sym}")
            continue
        if is_small and small_exposure + usd > SMALL_COIN_BUDGET_USD:
            print(f"Small-coin budget full (${small_exposure:.2f}), skipped {sym}")
            continue
        if buy(sym, usd):
            exposure += usd
            small_exposure += usd if is_small else 0

    print(f"Crypto exposure after run: ${exposure:.2f} of ${BUDGET_USD}")
