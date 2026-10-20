# alpaca-trading-bot

A rule-based bot that trades **crypto** and **US stocks** on an Alpaca **paper** account
(simulated money, `paper=True` everywhere). It reads candles, calculates indicators, and
buys or sells on its own: on your Mac while it is on, and on GitHub Actions when it is not.
A Streamlit dashboard lets you watch everything live.

---

## How it trades

**Crypto buying is done by the top-performer rotator** (see below). `crypto_trader.py` no longer
buys (`BUY_ENABLED = False`); it keeps managing the coins it already holds with the sell rules here.
Stocks use the rules below for both buying and selling.

Every run, for each coin/stock, the bot downloads recent candles (only **closed** candles,
so a half-formed candle never triggers a trade) and checks these rules (`strategy.py`).

### Buy: all 5 must be true
| # | Check | Meaning |
|---|---|---|
| 1 | Close > SMA 50 | price is in an uptrend |
| 2 | EMA 9 crossed above EMA 21 in the last 3 candles | fresh momentum |
| 3 | RSI 14 < 70 | not already overbought |
| 4 | ADX 14 ≥ 25 | trend is strong, not choppy |
| 5 | Close > today's VWAP | buyers in control today (resets midnight UTC for crypto, midnight New York for stocks) |

### Sell: any one is enough
| | Crypto (30-min candles) | Stocks (1-hour candles) |
|---|---|---|
| Take-profit | +15%: a **standing limit sell at Alpaca**, filled the moment price touches it, 24/7, even with the Mac off | +10% (checked each run) |
| Signal exit (EMA 9 < EMA 21, or close < SMA 50) | **only above +0.75%** (covers Alpaca's ~0.25% fee each way + spread) | yes |
| Stop-loss | **none: never sells at a loss**, holds until positive | −7% |

### Limits
| | Crypto | Stocks |
|---|---|---|
| Total budget | $6,000 | $4,000 |
| Per buy | $750 main coins, $250 small coins (≤ $1) | $500 |
| Other | small coins capped at $1,500 in total | skips stocks under $5 |

- No new buys when the account is down **3%** on the day (sells still run).
- Never buys a symbol it already holds or has an unfilled buy order for, never re-buys one it just sold in the same run.
- Crypto take-profit orders are re-checked every run: one per position, replaced if the quantity or entry price changes, cancelled when the position is sold.
- Strongest trends are bought first while budget remains.

### What it watches
- **Crypto main:** BTC, ETH, SOL, AAVE, ZEC, XMR, SUI, QNT, INJ
- **Crypto small coins:** DOGE, SHIB, PEPE, BONK, WIF, ADA, ARB, POL, ONDO, GRT, BAT, CRV, XTZ, SUSHI, TIA, RSR, BTW
- **Stocks:** NVDA, MU, SPCX, COST, ORCL, MSTR, CRWV (`WATCHLIST` in `stock_trader.py`) **plus** today's top 15 % gainers

Coins Alpaca doesn't list are skipped automatically and named in the log
(currently ZEC, XMR, SUI, QNT, INJ, TIA, RSR, BTW).

---

## Crypto rotator: `top_crypto_rotator.py`

Every 15 minutes it ranks all liquid USD crypto pairs on Alpaca by today's % change
(since midnight UTC) and holds only the #1 performer, in one position.

| Setting | Value |
|---|---|
| Budget | **$2,000** in one position (on top of the $10,000 above), never more than 20% of cash |
| Buy | leader is up **≥ 2%** today, traded ≥ $10k on Alpaca yesterday, isn't a stablecoin, and isn't a coin `crypto_trader.py` holds |
| Take-profit | **+8%**, a standing limit sell at Alpaca (24/7) |
| Rotate | a new leader beats the held coin by **≥ 1.5%** today, **and** the held coin is up **> +0.75%** |
| Loss | **never sells below entry**; a losing coin is held until positive |
| Daily loss | account down 3% today: no new buys (nothing sold) |
| Stuck buy | a rotator buy unfilled for 30 min is cancelled |

Its orders are tagged `rot-` (buys) and `rtp-` (take-profit), so the two crypto bots never
touch each other's coins.

---

## Where it runs

```
Mac on  ──► runner.py (launch agent) ──► trades on time, sets MAC_HEARTBEAT on GitHub
Mac off ──► GitHub Actions schedule   ──► sees a stale heartbeat (>45 min), trades instead
```

### 1. On your Mac: `runner.py` (primary)
- Crypto bot at **:02 and :32**, rotator at **:02, :17, :32, :47**, stocks at **:05** (stocks exit by themselves when the market is closed).
- Pulls the latest `main` from GitHub before every run.
- After each **successful** run it sets the `MAC_HEARTBEAT` repo variable, so GitHub stands down.
- Installed as a launch agent: starts at login, restarts if it stops, keeps the Mac awake (`caffeinate`).
- Runs from its own clone in `~/.alpaca-trading-bot` (macOS blocks background services from `~/Documents`).

```bash
# one time: paper keys in .env (never committed)
printf 'ALPACA_API_KEY=PK...\nALPACA_SECRET_KEY=...\n' > .env

./mac/install.sh                                   # install + start
tail -f ~/.alpaca-trading-bot/logs/$(date +%F).log  # watch it
./mac/uninstall.sh                                 # stop; GitHub takes over immediately
```
Re-run `./mac/install.sh` after changing `.env`. Requires the GitHub CLI (`gh`) logged in.

### 2. On GitHub Actions: `.github/workflows/trading_bot.yml` (backup)
- Scheduled crypto (bot + rotator) every 30 min and stocks hourly in market hours; each scheduled run skips
  itself while the Mac heartbeat is under 45 minutes old.
- **Actions → Trading Bots → Run workflow** runs both jobs right away (always, even if the Mac is active).
- Needs repo secrets `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`.
- GitHub often starts scheduled runs late (sometimes ~2 h apart), which is why the Mac runner is primary.

> A change only affects trading once it is on `main`.

---

## Dashboard (Streamlit): watch it live

`dashboard/app.py` is **read-only**: it never places orders. Refreshes every minute.

| Tab | Shows |
|---|---|
| Overview | equity, today's P/L, daily-loss stop, budget bars, next scheduled runs, positions with what the bot will do next run (e.g. "🔒 holding until in profit", "SELL next run: take-profit") |
| Signals | every coin/stock with ✓/✗ and the value for each of the 5 buy checks |
| Chart | interactive candles, EMA 9 blue, EMA 21 black, VWAP purple, ADX red, ▲▼ the bot's real buys/sells |
| Activity | orders from the last 7 days |
| Bot runs | each GitHub Actions run and what the bot printed (needs `GITHUB_TOKEN`) |

**Deploy on Streamlit Community Cloud:** repo `aruios/alpaca-trading-bot`, branch `main`,
main file `dashboard/app.py`, Python 3.12, secrets:
```toml
ALPACA_API_KEY = "PK..."
ALPACA_SECRET_KEY = "..."
GITHUB_TOKEN = "..."   # optional: fine-grained, this repo, Actions: Read-only
```
**Run locally:** `pip install -r dashboard/requirements.txt && streamlit run dashboard/app.py`

---

## Chart image: `chart.py`

```bash
python chart.py BTC/USD      # crypto, 30-minute candles
python chart.py NVDA         # stock, hourly candles
python chart.py ETH/USD 5    # 5 days instead of 3
```
Saves `charts/<SYMBOL>.png` with EMA 9 (blue), EMA 21 (black), VWAP (purple), ADX (red panel).

---

## Files

| File | Purpose |
|---|---|
| `strategy.py` | indicators (EMA, SMA, RSI, ADX, VWAP), `Rules`, buy/sell logic shared by everything |
| `crypto_trader.py` | crypto bot: manages the coins it holds (buying is off), take-profit orders, shared crypto helpers |
| `top_crypto_rotator.py` | crypto rotator: holds today's top performer, does all new crypto buys |
| `stock_trader.py` | stock bot: budget, top-gainer scan, `RULES` |
| `runner.py`, `mac/` | Mac runner and its install/uninstall scripts |
| `.github/workflows/trading_bot.yml` | GitHub Actions schedule |
| `dashboard/` | Streamlit dashboard and its own `requirements.txt` |
| `chart.py` | saves a PNG chart |
| `CLAUDE.md` | project notes for Claude sessions (e.g. on claude.ai/code) |
| `.mcp.json` | Alpaca MCP server for Claude (paper; needs `uv` installed) |
| `bot.py` | old one-off SPY example, not used |

## Changing settings
- **Budgets / trade sizes / watchlists:** top of `crypto_trader.py` and `stock_trader.py`.
- **Rule thresholds** (take-profit, stop-loss, ADX/RSI limits, candle size): the `RULES = Rules(...)`
  line in each trader; defaults are in `strategy.py`.
- Push to `main`; the Mac runner picks it up on its next run, GitHub on its next scheduled run.

## Limitations
- Follows fixed rules: no chart-pattern recognition, no news.
- Crypto with no stop-loss can hold a falling coin indefinitely, tying up budget.
- Thinly traded coins with no volume since midnight UTC fail the VWAP check.
- Paper trading only; fills and slippage differ from a live account.
