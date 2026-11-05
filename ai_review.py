"""Claude second opinion on every crypto run - SHADOW MODE: it never places or cancels orders.

Once per run, after the rules have bought/sold, Claude gets every coin's last 24 closed
candles, the 5 buy checks, the position P/L and what the rules just did, and answers
buy / sell / hold / skip per coin. The answer goes in the analysis report ("Claude" column)
and is appended to logs/analysis/ai-shadow.jsonl so it can be scored against the rules later:

    python ai_review.py --score        # how Claude's calls and the rules' calls did 24h later

Off unless ANTHROPIC_API_KEY is set. Any error just prints and the bot carries on.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(ROOT, "logs", "analysis", "ai-shadow.jsonl")
MODEL = os.getenv("AI_REVIEW_MODEL", "claude-opus-5-5")    # ✅ e.g. claude-sonnet-5-5 to halve the cost
EFFORT = os.getenv("AI_REVIEW_EFFORT", "low")              # ✅ low | medium | high: more = deeper and pricier
RECENT_CANDLES = 24                                        # ✅ 24 x 30 min = last 12 hours of closes
SCORE_HOURS = 24                                           # ✅ --score: price change this long after each call

SYSTEM = """You review a rule-based crypto trading bot on an Alpaca paper account, once per 30-minute candle.
The bot's rules (they stay in charge; you are a second opinion being measured against them):
- Buy when all 5 checks pass on the last closed 30-min candle: close > SMA 50, EMA 9 crossed above
  EMA 21 in the last 3 candles, RSI 14 < 70, ADX 14 >= 25, close > today's VWAP (resets 00:00 UTC).
- Sell at -15% stop-loss or +15% take-profit, or on a signal exit (EMA 9 < EMA 21, or close < SMA 50)
  once the position is above +0.75%. Fees are about 0.25% each way.
For every coin listed, give the action you would take right now:
- "buy": not held, and you would buy it now.
- "skip": not held, and you would not buy it now.
- "hold": held, and you would keep it.
- "sell": held, and you would sell it now.
Judge from the numbers given (price action, the checks, P/L). Be decisive and keep each reason under
20 words. Also give a one-sentence view of the overall crypto market from these charts."""

SCHEMA = {
    "type": "object",
    "properties": {
        "market_view": {"type": "string"},
        "coins": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "action": {"type": "string", "enum": ["buy", "skip", "hold", "sell"]},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "reason": {"type": "string"},
                },
                "required": ["symbol", "action", "confidence", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["market_view", "coins"],
    "additionalProperties": False,
}


def coin_text(sym, sig, candles, position, action):
    checks = ", ".join(f"{name} {'PASS' if ok else 'fail'} ({val})" for name, ok, val in sig.checks)
    closes = " ".join(f"{c.close:.6g}" for c in candles[-RECENT_CANDLES:])
    held = f"HELD, P/L {float(position.unrealized_plpc):+.1%}" if position else "not held"
    return (f"{sym}: close {sig.close:.6g}, {held}. Rules did: {action}.\n"
            f"  checks: {checks or sig.reason}\n  last {RECENT_CANDLES} closes (oldest first): {closes}")


def review(signals, candles, positions, actions):
    """Returns {sym: {"action", "confidence", "reason"}} (empty when off or on any error)."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("AI review off (no ANTHROPIC_API_KEY).")
        return {}
    try:
        import anthropic
    except ImportError:
        print("AI review off (pip install -r requirements.txt to get the anthropic package).")
        return {}

    syms = sorted(signals)
    prompt = "\n\n".join(coin_text(s, signals[s], candles.get(s, []), positions.get(s), actions.get(s, "wait"))
                         for s in syms)
    try:
        client = anthropic.Anthropic(timeout=120.0)
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",                                  # ✅ a declined request retries on another model
        )
    except anthropic.APIError as e:
        print(f"AI review failed: {e}")
        return {}
    if response.stop_reason != "end_turn":                        # ✅ refusal / max_tokens: no usable JSON
        print(f"AI review skipped: stop_reason={response.stop_reason}")
        return {}

    u = response.usage
    print(f"AI review by {response.model}: {u.input_tokens} in / {u.output_tokens} out tokens")
    data = json.loads(next(b.text for b in response.content if b.type == "text"))
    print(f"AI market view: {data['market_view']}")
    out = {c["symbol"]: c for c in data["coins"] if c["symbol"] in signals}

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a") as f:
        for sym, c in out.items():
            f.write(json.dumps({"time": now, "symbol": sym, "close": signals[sym].close,
                                "held": sym in positions, "rules": actions.get(sym, "wait"),
                                "ai": c["action"], "confidence": c["confidence"], "reason": c["reason"],
                                "model": response.model}) + "\n")
    return out


# ---------- python ai_review.py --score ----------

def score():
    """Price move SCORE_HOURS after each call, using later log rows of the same coin as the price feed."""
    rows = [json.loads(line) for line in open(LOG)] if os.path.exists(LOG) else []
    by_sym = {}
    for r in rows:
        r["t"] = datetime.fromisoformat(r["time"])
        by_sym.setdefault(r["symbol"], []).append(r)

    stats = {}                                                    # ✅ (who, call) -> [moves]
    for series in by_sym.values():
        series.sort(key=lambda x: x["t"])
        for r in series:
            target = r["t"] + timedelta(hours=SCORE_HOURS)
            later = next((x for x in series if x["t"] >= target), None)
            if later is None or later["t"] > target + timedelta(hours=6):  # ✅ bot was off: no fair price
                continue
            move = later["close"] / r["close"] - 1
            rules = ("buy" if "BOUGHT" in r["rules"] else "sell" if "SOLD" in r["rules"]
                     else "hold" if r["held"] else "skip")
            stats.setdefault(("rules", rules), []).append(move)
            stats.setdefault(("claude", r["ai"]), []).append(move)

    print(f"{len(rows)} logged calls; avg price move {SCORE_HOURS}h after each call "
          "(good buy/hold = up, good sell/skip = down):")
    for (who, call), moves in sorted(stats.items()):
        print(f"  {who:7} {call:5} n={len(moves):4}  avg {sum(moves) / len(moves):+.2%}")


if __name__ == "__main__":
    if "--score" in sys.argv:
        score()
    else:
        print(__doc__)
