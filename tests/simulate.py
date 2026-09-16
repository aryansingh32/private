"""Offline simulation: 5.5 days of 5-minute polls against fake devices.

Run with:  python3 tests/simulate.py
Sends nothing - DRY_RUN prints the Telegram messages it would deliver.
"""
import os, sys
from datetime import datetime, timedelta, timezone
S = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_sim")
os.makedirs(S, exist_ok=True)
os.environ.update(TAILSCALE_API_KEY="x", TELEGRAM_BOT_TOKEN="x", TELEGRAM_CHAT_ID="x",
                  STATE_FILE=f"{S}/state.json", LOG_DIR=f"{S}/logs",
                  TIMEZONE="Asia/Kolkata", DRY_RUN="1", FLAP_CONFIRMATIONS="2")
sys.path.insert(0, os.path.dirname(os.path.dirname(S)))
for f in (f"{S}/state.json",):
    if os.path.exists(f): os.remove(f)
import monitor as m

UTC = timezone.utc
start = datetime(2026, 9, 10, 0, 0, tzinfo=UTC)

def devices(when):
    ist = when + timedelta(hours=5, minutes=30)
    # laptop: online 09:00-18:00 IST on weekdays, with a 10-min lunch blip
    up = (ist.weekday() < 5 and 9 <= ist.hour < 18
          and not (ist.hour == 13 and ist.minute < 10))
    # server: always up except a 25-min outage on day 3
    srv = not (when.date() == datetime(2026, 9, 12).date() and when.hour == 4 and when.minute < 25)
    return [
        {"nodeId": "n1", "name": "laptop.tail.ts.net", "hostname": "laptop", "os": "linux",
         "connectedToControl": up, "lastSeen": when.isoformat(), "addresses": ["100.64.0.1"],
         "clientVersion": "1.102.4-tabc"},
        {"nodeId": "n2", "name": "server.tail.ts.net", "hostname": "server", "os": "linux",
         "connectedToControl": srv, "lastSeen": when.isoformat(), "addresses": ["100.64.0.2"],
         "clientVersion": "1.102.4-tabc"},
    ]

state = m.load_state()
sent = 0
for step in range(int(5.5 * 24 * 12)):      # 5.5 days of 5-minute polls
    when = start + timedelta(minutes=5 * step)
    msgs = m.process(state, devices(when), when)
    if m.digest_due(state, when) and state["devices"]:
        if not (msgs and state["last_run"] is None):
            msgs.append(m.render_digest(state, when))
        state["last_digest_date"] = m.to_local(when).strftime("%Y-%m-%d")
    for msg in msgs:
        sent += 1
        if sent <= 4 or step > 5.0 * 24 * 12:
            print(f"\n===== poll {step} | {m.stamp(when)} =====")
            print(msg)
    state["last_run"] = m.iso(when)
m.save_state(state)
print(f"\n### {sent} telegram messages over {step+1} polls "
      f"({(step+1)*5/60:.0f}h) -> {sent/((step+1)*5/1440):.1f} msgs/day")
print("### event log tail:")
import glob
for path in sorted(glob.glob(f"{S}/logs/*.log")):
    print(open(path).read().strip()[-900:])
