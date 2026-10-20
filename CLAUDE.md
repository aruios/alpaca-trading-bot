# Alpaca trading bot

Rule-based bot that trades crypto and US stocks on an Alpaca **paper** account
(`paper=True` everywhere). Never switch to live trading unless the owner asks.

## How it runs
- `.github/workflows/trading_bot.yml` runs from **`main`** on GitHub Actions:
  `crypto_trader.py` every 30 min (`:02`, `:32`), `stock_trader.py` hourly in US market hours.
- `runner.py` (installed by `mac/install.sh` as a launch agent) runs the same scripts on the
  owner's Mac on time while it is on, pulling `main` first. After each successful run it sets the
  `MAC_HEARTBEAT` repo variable; scheduled Actions runs skip while it is < 45 min old, so the Mac
  and GitHub never both trade. Manual "Run workflow" always runs. It runs from its own clone in
  `~/.alpaca-trading-bot` (macOS blocks background services from ~/Documents); logs there.
- A change only affects trading once it is merged to `main`. Work on a branch / PR.
- Alpaca keys come from `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` (GitHub Secrets in CI,
  `.env` locally). Never commit keys.

## Files
- `strategy.py` - shared indicators (EMA, SMA, RSI, ADX, VWAP), `Rules`, buy/sell logic.
- `crypto_trader.py` - crypto, 30-minute candles. `BUY_ENABLED = False`: only manages coins it
  already holds (sells, `tp-` take-profit orders). Also holds shared crypto helpers.
- `top_crypto_rotator.py` - owner's top-performer rotator; does all new crypto buys. Buys the best
  riser (>= +2% today) whose 30-min chart confirms (EMA9>EMA21, > VWAP, RSI<70). One position
  (<= $2,000 extra budget), +8% `rtp-` take-profit, rotates only while in profit, never sells at a
  loss. Its coin = latest filled `rot-` buy still held (`rotator_holding()`); crypto_trader skips it.
- `stock_trader.py` - stocks, hourly candles, checks `WATCHLIST` plus today's top gainers.
- `chart.py` - saves `charts/<SYMBOL>.png`: candles, EMA 9 blue, EMA 21 black,
  VWAP purple, ADX red (own panel). Uses the same indicator code as the bot.
- `dashboard/app.py` - read-only Streamlit dashboard (Streamlit Community Cloud, own
  `dashboard/requirements.txt`). Reuses the traders' config and `strategy.py`; never places orders.
- `bot.py` - old one-off SPY example, not run by the workflow.

## Trading rules (owner's choices - keep them unless asked to change)
Buy, on the last **closed** candle, all of:
close > SMA 50, EMA 9 crossed above EMA 21 in the last 3 candles, RSI 14 < 70,
ADX 14 >= 25, close > today's VWAP (resets midnight UTC for crypto, midnight New York for stocks).

Sell:
- **Crypto never sells at a loss** (`sell_at_loss=False`, `min_profit=0.0075`): no stop-loss;
  signal exits only above +0.75% so fees (~0.25% each way) and spread can't turn it into a loss. Then it sells at +15% take-profit, or on the
  signal exit (EMA 9 below EMA 21, or close below SMA 50).
  The +15% is a standing GTC limit sell at Alpaca (client_order_id prefix `tp-`), kept in sync
  each run by `sync_take_profits()`; `sell()` cancels it before closing a position.
- Stocks: -7% stop-loss, +10% take-profit, or the signal exit.

No new buys when the account is down 3% on the day. No double-buys of a held symbol.

## Conventions
- Plain Python, no pandas. Keep the short `# ✅` inline comments style.
- Rule settings live in each trader's `RULES = Rules(...)`; change them there rather
  than editing defaults in `strategy.py`.
- Check changes with `python -m py_compile *.py`; test signal logic with fake candles
  (objects with `timestamp, open, high, low, close, volume`), since real runs place orders.
