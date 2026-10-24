"""Chart analysis report written on every bot run.

Ranks every symbol by how many of the 5 buy checks pass (then by trend strength) and records
what the bot did with it. Written to logs/analysis/<name>-latest.md, appended to the GitHub
Actions run summary when running there (open the run in the GitHub app to read it), and the
top of the ranking is printed to the run log.
"""
import os
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))


def write_report(name, title, signals, actions, positions):
    """signals: {sym: Signal}; actions: {sym: text}; positions: {sym: position} (P/L shown)."""
    rows = []
    for sym, sig in signals.items():
        score = sum(ok for _, ok, _ in sig.checks)
        rows.append((sym in positions, score, sig.strength, sym, sig))
    rows.sort(key=lambda r: (r[0], r[1], r[2]), reverse=True)        # ✅ held first, then best setups

    names = [n for n, _, _ in next((r[4].checks for r in rows if r[4].checks), [])]
    now = datetime.now(timezone.utc)
    lines = [f"## {title}", f"_{now:%Y-%m-%d %H:%M} UTC · best buy setups first; a buy needs all 5 checks_", "",
             "| Symbol | Price | Score | " + " | ".join(names) + " | Position P/L | Action |",
             "|---|---|---|" + "---|" * len(names) + "---|---|"]
    for held, score, _, sym, sig in rows:
        cells = [f"{'✅' if ok else '❌'} {val}" for _, ok, val in sig.checks] or ["n/a"] * len(names)
        pl = f"{float(positions[sym].unrealized_plpc):+.1%}" if held else ""
        lines.append(f"| {'**' + sym + '**' if held else sym} | {sig.close:.6g} | {score}/5 | "
                     + " | ".join(cells) + f" | {pl} | {actions.get(sym, 'wait')} |")
    text = "\n".join(lines) + "\n"

    out_dir = os.path.join(ROOT, "logs", "analysis")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, f"{name}-latest.md"), "w") as f:
        f.write(text)
    summary = os.getenv("GITHUB_STEP_SUMMARY")                       # ✅ shows on the Actions run page
    if summary:
        with open(summary, "a") as f:
            f.write(text + "\n")

    print("ANALYSIS (best setups first):")
    for held, score, strength, sym, sig in rows[:8]:
        print(f"  {score}/5 {sym:10} trend {strength:+.1%}  {'held ' if held else ''}{actions.get(sym, 'wait')}")
