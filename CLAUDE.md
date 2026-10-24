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
- `crypto_trader.py` - crypto, 30-minute candles; does all crypto buying and selling. Also holds
  shared crypto helpers.
- `top_crypto_rotator.py` - owner's top-performer rotator, **disabled** (`ENABLED = False`, and
  `crypto_trader.ROTATOR_ENABLED = False`): lost money in every backtest. Kept for `backtest.py`.
  While disabled, crypto_trader adopts any leftover `rtp-` orders as its own take-profits.
- `stock_trader.py` - stocks, hourly candles, checks `WATCHLIST` plus today's top gainers.
- `chart.py` - saves `charts/<SYMBOL>.png`: candles, EMA 9 blue, EMA 21 black,
  VWAP purple, ADX red (own panel). Uses the same indicator code as the bot.
- `analysis.py` - per-run chart analysis report (ranked coins, ✅/❌ per check, action taken) to
  `logs/analysis/crypto-latest.md` and the GitHub Actions step summary. Uses `Signal.checks`.
- `backtest.py` - replays current rules on Alpaca history with fees (no orders); run it to
  judge any rule change before shipping. Signals are verified identical to `strategy.evaluate`.
- `bot.py` - old one-off SPY example, not run by the workflow.

## Trading rules (owner's choices - keep them unless asked to change)
Buy, on the last **closed** candle, all of:
close > SMA 50, EMA 9 crossed above EMA 21 in the last 3 candles, RSI 14 < 70,
ADX 14 >= 25, close > today's VWAP (resets midnight UTC for crypto, midnight New York for stocks).

Sell:
- **Crypto** (owner approved 2026-10-22, from backtest): **−15% stop-loss**, +15% take-profit,
  signal exit (EMA 9 below EMA 21, or close below SMA 50) only above +0.75% (`min_profit`) so
  fees (~0.25% each way) and spread can't make it a loss. Replaced the old "never sell at a loss"
  rule, which lost money in backtests (losers held forever).
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
