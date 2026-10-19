"""Run the bot on this Mac right after each candle closes, for as long as the Mac is on.

  crypto_trader.py  at :02 and :32 (30-minute candles)
  stock_trader.py   at :05 every hour (it exits by itself when the market is closed)

Before each run it pulls the latest code from GitHub, so changes merged to main apply here too.
After each successful run it sets the MAC_HEARTBEAT repo variable. GitHub Actions skips
its scheduled runs while the heartbeat is under 45 min old, and takes over again when this
Mac is off, asleep, or its runs keep failing.

Start:  mac/install.sh  (launch agent, starts at login, keeps the Mac awake)
Once:   .venv/bin/python runner.py --once
Logs:   logs/YYYY-MM-DD.log
"""
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
REPO = "aruios/alpaca-trading-bot"
LOG_DIR = os.path.join(ROOT, "logs")
CRYPTO_MINUTES = (2, 32)       # ✅ same as trading_bot.yml
STOCK_MINUTE = 5
RUN_TIMEOUT = 10 * 60          # ✅ a stuck run never blocks the next one for long


def log(msg):
    now = datetime.now()
    line = f"{now:%H:%M:%S} {msg}"
    print(line, flush=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, f"{now:%Y-%m-%d}.log"), "a") as f:
        f.write(line + "\n")


def sh(*cmd, timeout=60):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)


def heartbeat():
    r = sh("gh", "variable", "set", "MAC_HEARTBEAT", "-R", REPO,
           "--body", str(int(time.time())))
    if r.returncode:
        log(f"WARNING heartbeat failed, GitHub may also run the bot: {r.stderr.strip()}")


def pull():
    r = sh("git", "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
           "pull", "--ff-only", "-q", f"https://github.com/{REPO}.git", "main")
    if r.returncode:
        log(f"WARNING git pull failed, running local code: {r.stderr.strip()}")


def run(script):
    log(f"--- {script} ---")
    try:
        r = sh(sys.executable, script, timeout=RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        log(f"  {script} timed out after {RUN_TIMEOUT // 60} min")
        return False
    for line in (r.stdout + r.stderr).strip().splitlines():
        log(f"  {line}")
    if r.returncode:
        log(f"  {script} exited with code {r.returncode}")
    return r.returncode == 0


def due_jobs(t):
    jobs = []
    if t.minute in CRYPTO_MINUTES:
        jobs.append("crypto_trader.py")
    if t.minute == STOCK_MINUTE and t.weekday() < 5:
        jobs.append("stock_trader.py")
    return jobs


def next_slot(after):
    t = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    while not due_jobs(t):
        t += timedelta(minutes=1)
    return t


def tick(jobs):
    pull()
    ok = [run(script) for script in jobs]
    if all(ok):
        heartbeat()                                           # ✅ only claim the bot when runs work


if __name__ == "__main__":
    if "--once" in sys.argv:
        tick(["crypto_trader.py", "stock_trader.py"])
        sys.exit(0)

    log(f"runner started (pid {os.getpid()})")
    slot = next_slot(datetime.now(timezone.utc))
    while True:
        now = datetime.now(timezone.utc)
        if now >= slot:
            late = now - slot
            if late > timedelta(minutes=20):                  # ✅ woke from sleep: one catch-up crypto run
                log(f"woke up {late} after the {slot:%H:%M} UTC slot, running crypto now")
                tick(["crypto_trader.py"])
            else:
                tick(due_jobs(slot))
            slot = next_slot(datetime.now(timezone.utc))
            log(f"next run {slot.astimezone():%H:%M}")
        time.sleep(20)                                        # ✅ short naps survive Mac sleep/wake
