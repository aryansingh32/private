#!/usr/bin/env python3
"""
Tailscale device monitor -> Telegram notifier.

Polls the Tailscale API, tracks every device's online/offline state across runs
in a JSON state file, appends a timestamped event log, and pushes a Telegram
message ONLY when something actually changes (plus one analytics digest a day).

Standard library only - no pip install, so CI runs stay fast.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - py<3.9
    ZoneInfo = None

UTC = timezone.utc
STATE_VERSION = 1


# --------------------------------------------------------------------------- #
# configuration (everything comes from the environment)
# --------------------------------------------------------------------------- #

def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_int(name: str, default: int) -> int:
    try:
        return int(env(name) or default)
    except ValueError:
        return default


def env_bool(name: str, default: bool = False) -> bool:
    raw = env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


TAILSCALE_API_KEY = env("TAILSCALE_API_KEY")
TAILNET = env("TAILNET", "-") or "-"
TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = env("TELEGRAM_CHAT_ID")

STATE_FILE = env("STATE_FILE", "state/state.json")
LOG_DIR = env("LOG_DIR", "logs")
TIMEZONE = env("TIMEZONE", "UTC") or "UTC"

# "auto" = trust the API's own online flag; "lastseen" = judge purely by lastSeen age.
ONLINE_SOURCE = (env("ONLINE_SOURCE", "auto") or "auto").lower()
# Used when the API exposes no online flag (or ONLINE_SOURCE=lastseen).
ONLINE_THRESHOLD_SECONDS = env_int("ONLINE_THRESHOLD_SECONDS", 900)
# How many consecutive polls must agree before a flip is announced (anti-flap).
FLAP_CONFIRMATIONS = max(1, env_int("FLAP_CONFIRMATIONS", 1))
# Ignore gaps longer than this when accumulating uptime stats (workflow outage).
MAX_GAP_SECONDS = env_int("MAX_GAP_SECONDS", 7200)

DIGEST_ENABLED = env_bool("DIGEST_ENABLED", True)
DIGEST_HOUR = env_int("DIGEST_HOUR", 9)          # local hour, 0-23
FORCE_DIGEST = env_bool("FORCE_DIGEST", False)
NOTIFY_NEW_DEVICES = env_bool("NOTIFY_NEW_DEVICES", True)
NOTIFY_REMOVED_DEVICES = env_bool("NOTIFY_REMOVED_DEVICES", True)
DRY_RUN = env_bool("DRY_RUN", False)

MAX_SESSIONS = env_int("MAX_SESSIONS", 200)      # kept per device
MAX_DAYS = env_int("MAX_DAYS", 90)               # daily buckets kept per device
MAX_EVENTS = env_int("MAX_EVENTS", 500)          # events kept in state.json

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
TAILSCALE_API = "https://api.tailscale.com/api/v2"


def local_tz():
    if ZoneInfo is not None:
        try:
            return ZoneInfo(TIMEZONE)
        except Exception:
            pass
    return UTC


TZ = local_tz()


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat() if dt else None


def parse_iso(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def to_local(dt: datetime) -> datetime:
    return dt.astimezone(TZ)


def stamp(dt: datetime | None) -> str:
    if not dt:
        return "never"
    return to_local(dt).strftime("%Y-%m-%d %H:%M:%S %Z")


def clock(dt: datetime | None) -> str:
    return to_local(dt).strftime("%H:%M") if dt else "--:--"


def fmt_duration(seconds: float | None) -> str:
    """3d 4h 12m / 48m / 30s"""
    if seconds is None:
        return "unknown"
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    return " ".join(parts) or "0m"


def esc(text) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def log(msg: str) -> None:
    print(f"[{now_utc().isoformat()}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def http_request(url: str, *, method: str = "GET", headers: dict | None = None,
                 data: bytes | None = None, timeout: int = 30,
                 attempts: int = 3) -> tuple[int, bytes]:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read()
            if exc.code == 429 or 500 <= exc.code < 600:
                last_error = exc
                retry_after = exc.headers.get("Retry-After")
                delay = int(retry_after) if (retry_after or "").isdigit() else attempt * 3
                log(f"HTTP {exc.code} from {url.split('?')[0]}; retrying in {delay}s")
                time.sleep(min(delay, 30))
                continue
            return exc.code, body
        except Exception as exc:  # network hiccup
            last_error = exc
            log(f"request error ({exc}); attempt {attempt}/{attempts}")
            time.sleep(attempt * 3)
    raise RuntimeError(f"request to {url.split('?')[0]} failed: {last_error}")


def fetch_devices() -> list[dict]:
    url = f"{TAILSCALE_API}/tailnet/{urllib.parse.quote(TAILNET)}/devices?fields=all"
    status, body = http_request(
        url, headers={"Authorization": f"Bearer {TAILSCALE_API_KEY}",
                      "Accept": "application/json"})
    if status != 200:
        raise RuntimeError(f"Tailscale API returned {status}: {body[:300].decode('utf-8', 'replace')}")
    payload = json.loads(body.decode("utf-8"))
    devices = payload.get("devices", [])
    log(f"fetched {len(devices)} device(s) from tailnet '{TAILNET}'")
    return devices


def telegram_send(text: str) -> None:
    """Send a message, splitting on Telegram's 4096-char limit."""
    if DRY_RUN:
        print("\n----- DRY RUN MESSAGE -----\n" + text + "\n---------------------------\n")
        return
    for chunk in chunk_message(text):
        url = TELEGRAM_API.format(token=TELEGRAM_BOT_TOKEN, method="sendMessage")
        payload = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode()
        status, body = http_request(
            url, method="POST", data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        if status != 200:
            log(f"telegram error {status}: {body[:300].decode('utf-8', 'replace')}")
        else:
            log(f"telegram message sent ({len(chunk)} chars)")
        time.sleep(0.4)  # stay well under Telegram's rate limits


def chunk_message(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit and current:
            chunks.append(current.rstrip())
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current.rstrip())
    return chunks


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            state = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state.setdefault("version", STATE_VERSION)
    state.setdefault("devices", {})
    state.setdefault("events", [])
    state.setdefault("last_run", None)
    state.setdefault("last_digest_date", None)
    return state


def save_state(state: dict) -> None:
    directory = os.path.dirname(os.path.abspath(STATE_FILE))
    os.makedirs(directory, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, STATE_FILE)


def append_log(line: str, when: datetime) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, to_local(when).strftime("%Y-%m.log"))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def new_device_state(device: dict, online: bool, when: datetime) -> dict:
    # A device first seen while offline gets `since` backdated to lastSeen so the
    # first "came online" message reports a truthful downtime.
    since = when
    if not online:
        last_seen = parse_iso(device.get("lastSeen"))
        if last_seen and last_seen < when:
            since = last_seen
    return {
        "name": short_name(device),
        "os": device.get("os", "unknown"),
        "status": "online" if online else "offline",
        "since": iso(since),
        "first_tracked": iso(when),
        "last_sampled": iso(when),
        "last_online": iso(when) if online else iso(parse_iso(device.get("lastSeen"))),
        "last_offline": None if online else iso(since),
        "pending_status": None,
        "pending_count": 0,
        "sessions": [],          # completed ONLINE sessions
        "outages": [],           # completed OFFLINE sessions
        "totals": {"online_seconds": 0, "offline_seconds": 0},
        "hour_online": [0] * 24,  # seconds observed online per local hour-of-day
        "hour_total": [0] * 24,
        "dow_online": [0] * 7,    # seconds observed online per local weekday
        "dow_total": [0] * 7,
        "daily": {},              # local date -> {"online": s, "total": s}
        "flips": 0,
        "removed": False,
    }


def short_name(device: dict) -> str:
    name = device.get("name") or device.get("hostname") or device.get("id", "?")
    return name.split(".")[0] if "." in name else name


def is_online(device: dict, when: datetime) -> bool:
    """Decide whether a device is up.

    The Tailscale API exposes this differently depending on the tailnet/plan:
    some return an explicit `online` boolean, others only `connectedToControl`
    (device currently attached to the control plane).  `lastSeen` is the
    universal fallback.  ONLINE_SOURCE=lastseen forces the fallback.
    """
    if ONLINE_SOURCE != "lastseen":
        for field in ("online", "connectedToControl"):
            flag = device.get(field)
            if isinstance(flag, bool):
                return flag
    last_seen = parse_iso(device.get("lastSeen"))
    if not last_seen:
        return False
    return (when - last_seen).total_seconds() <= ONLINE_THRESHOLD_SECONDS


# --------------------------------------------------------------------------- #
# time accounting: spread an interval over local hour / weekday / day buckets
# --------------------------------------------------------------------------- #

def attribute_interval(dev: dict, start: datetime, end: datetime, online: bool) -> None:
    total = (end - start).total_seconds()
    if total <= 0:
        return
    if total > MAX_GAP_SECONDS:
        # The monitor was down; we have no idea what happened in between.
        log(f"  gap of {fmt_duration(total)} for {dev['name']} exceeds MAX_GAP_SECONDS, not counted")
        return

    dev["totals"]["online_seconds" if online else "offline_seconds"] += int(total)

    cursor = to_local(start)
    local_end = to_local(end)
    guard = 0
    while cursor < local_end and guard < 1000:
        guard += 1
        next_hour = (cursor + timedelta(hours=1)).replace(
            minute=0, second=0, microsecond=0)
        slice_end = min(next_hour, local_end)
        seconds = int((slice_end - cursor).total_seconds())
        if seconds > 0:
            hour = cursor.hour
            dow = cursor.weekday()
            day = cursor.strftime("%Y-%m-%d")
            dev["hour_total"][hour] += seconds
            dev["dow_total"][dow] += seconds
            bucket = dev["daily"].setdefault(day, {"online": 0, "total": 0})
            bucket["total"] += seconds
            if online:
                dev["hour_online"][hour] += seconds
                dev["dow_online"][dow] += seconds
                bucket["online"] += seconds
        cursor = slice_end

    # keep the daily history bounded
    if len(dev["daily"]) > MAX_DAYS:
        for day in sorted(dev["daily"])[:-MAX_DAYS]:
            dev["daily"].pop(day, None)


# --------------------------------------------------------------------------- #
# analytics
# --------------------------------------------------------------------------- #

def analytics(dev: dict, when: datetime) -> dict:
    totals = dev["totals"]
    online_s = totals["online_seconds"]
    offline_s = totals["offline_seconds"]
    tracked = online_s + offline_s

    sessions = [s["seconds"] for s in dev["sessions"] if s.get("seconds")]
    outages = [s["seconds"] for s in dev["outages"] if s.get("seconds")]
    since = parse_iso(dev["since"])
    current = (when - since).total_seconds() if since else 0

    days = max(tracked / 86400.0, 1e-9)
    out = {
        "uptime_pct": (online_s / tracked * 100) if tracked else None,
        "tracked_seconds": tracked,
        "online_seconds": online_s,
        "current_seconds": current,
        "sessions": len(sessions),
        "avg_session": (sum(sessions) / len(sessions)) if sessions else None,
        "longest_session": max(sessions) if sessions else None,
        "avg_outage": (sum(outages) / len(outages)) if outages else None,
        "online_per_day": online_s / days if tracked else None,
        "flips_per_day": dev["flips"] / days if tracked else None,
        "window": typical_window(dev),
        "next_expected": next_expected_online(dev, when),
        "busiest_day": busiest_weekday(dev),
        "today": today_online(dev, when),
    }
    return out


MIN_HOUR_SAMPLE = 900   # need 15 min of observation in an hour bucket to trust it
ONLINE_RATIO = 0.5


def hour_ratios(dev: dict) -> list[float | None]:
    return [
        (dev["hour_online"][h] / dev["hour_total"][h])
        if dev["hour_total"][h] >= MIN_HOUR_SAMPLE else None
        for h in range(24)
    ]


def typical_window(dev: dict) -> str | None:
    """Contiguous local hours where the device is usually up, e.g. '09:00-18:00'."""
    ratios = hour_ratios(dev)
    flags = [r is not None and r >= ONLINE_RATIO for r in ratios]
    if not any(flags):
        return None
    if all(flags):
        return "always on (24/7)"

    windows, start = [], None
    for h in range(24):
        if flags[h] and start is None:
            start = h
        elif not flags[h] and start is not None:
            windows.append((start, h))
            start = None
    if start is not None:
        windows.append((start, 24))
    # merge a midnight-spanning window
    if len(windows) > 1 and windows[0][0] == 0 and windows[-1][1] == 24:
        windows[0] = (windows[-1][0], windows[0][1] + 24)
        windows.pop()
    windows.sort(key=lambda w: w[1] - w[0], reverse=True)
    return ", ".join(f"{s % 24:02d}:00-{e % 24:02d}:00" for s, e in windows[:2])


def next_expected_online(dev: dict, when: datetime) -> tuple[datetime, float] | None:
    if dev["status"] == "online":
        return None
    ratios = hour_ratios(dev)
    cursor = to_local(when).replace(minute=0, second=0, microsecond=0)
    for step in range(1, 49):
        candidate = cursor + timedelta(hours=step)
        ratio = ratios[candidate.hour]
        if ratio is not None and ratio >= ONLINE_RATIO:
            return candidate.astimezone(UTC), ratio
    return None


def busiest_weekday(dev: dict) -> str | None:
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    scored = [
        (dev["dow_online"][i] / dev["dow_total"][i], names[i])
        for i in range(7) if dev["dow_total"][i] >= 3600
    ]
    if not scored:
        return None
    ratio, name = max(scored)
    return f"{name} ({ratio * 100:.0f}%)"


def today_online(dev: dict, when: datetime) -> int:
    key = to_local(when).strftime("%Y-%m-%d")
    return dev["daily"].get(key, {}).get("online", 0)


# --------------------------------------------------------------------------- #
# message rendering
# --------------------------------------------------------------------------- #

def render_change(event: dict, dev: dict, device: dict | None, when: datetime) -> str:
    kind = event["type"]
    name = esc(dev["name"])
    stats = analytics(dev, when)
    lines: list[str] = []

    if kind == "online":
        lines.append(f"\U0001F7E2 <b>{name}</b> is <b>ONLINE</b>")
        lines.append(f"\U0001F550 {esc(stamp(when))}")
        lines.append(f"⏱ Was offline for <b>{fmt_duration(event.get('duration'))}</b>")
    elif kind == "offline":
        lines.append(f"\U0001F534 <b>{name}</b> went <b>OFFLINE</b>")
        lines.append(f"\U0001F550 {esc(stamp(when))}")
        lines.append(f"⏱ Was online for <b>{fmt_duration(event.get('duration'))}</b>")
    elif kind == "new":
        status = "online" if dev["status"] == "online" else "offline"
        icon = "\U0001F7E2" if status == "online" else "\U0001F534"
        lines.append(f"✨ <b>New device</b> joined the tailnet")
        lines.append(f"{icon} <b>{name}</b> - currently {status}")
        lines.append(f"\U0001F550 {esc(stamp(when))}")
    elif kind == "removed":
        lines.append(f"\U0001F5D1 <b>{name}</b> was <b>removed</b> from the tailnet")
        lines.append(f"\U0001F550 {esc(stamp(when))}")
        return "\n".join(lines)

    if device:
        detail = [device.get("os", "?")]
        addrs = device.get("addresses") or []
        if addrs:
            detail.append(addrs[0])
        version = (device.get("clientVersion") or "").split("-")[0]
        if version:
            detail.append(f"v{version}")
        lines.append(f"\U0001F4BB {esc(' · '.join(detail))}")

    if stats["uptime_pct"] is not None and stats["tracked_seconds"] > 3600:
        lines.append(
            f"\U0001F4C8 Uptime {stats['uptime_pct']:.1f}% · "
            f"avg session {fmt_duration(stats['avg_session'])} · "
            f"{fmt_duration(stats['online_per_day'])}/day"
        )
    if kind == "offline" and stats["next_expected"]:
        nxt, ratio = stats["next_expected"]
        lines.append(f"\U0001F52E Usually back around {esc(stamp(nxt))} ({ratio * 100:.0f}%)")
    if kind != "removed" and stats["window"]:
        lines.append(f"\U0001F4C5 Typical window: {esc(stats['window'])}")
    return "\n".join(lines)


def render_batch(rendered: list[str], when: datetime) -> str:
    if len(rendered) == 1:
        return rendered[0]
    header = (f"\U0001F501 <b>{len(rendered)} status changes</b> · "
              f"{esc(stamp(when))}\n")
    return header + "\n\n".join(rendered)


def monitor_coverage(state: dict, when: datetime) -> str | None:
    """How much of the day the monitor actually observed.

    Gaps longer than MAX_GAP_SECONDS are never attributed to a device, so a
    monitor outage shows up directly as missing coverage.
    """
    local = to_local(when)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed_today = (local - midnight).total_seconds()
    yesterday = (local - timedelta(days=1)).strftime("%Y-%m-%d")
    live = [d for d in state["devices"].values() if not d.get("removed")]

    def coverage(day: str, span: float) -> float | None:
        seen = [d["daily"][day]["total"] for d in live if day in d["daily"]]
        if not seen or span <= 0:
            return None      # nothing observed that day - say nothing
        return min(100.0, max(seen) / span * 100)

    parts = []
    today = coverage(local.strftime("%Y-%m-%d"), elapsed_today)
    if today is not None:
        parts.append(f"today {today:.0f}%")
    prior = coverage(yesterday, 86400)
    if prior is not None:
        parts.append(f"yesterday {prior:.0f}%")
    return " \u00b7 ".join(parts) if parts else None


def render_digest(state: dict, when: datetime, title: str = "Daily report") -> str:
    devices = {k: v for k, v in state["devices"].items() if not v.get("removed")}
    online = [d for d in devices.values() if d["status"] == "online"]
    lines = [
        f"\U0001F4CA <b>Tailscale {esc(title)}</b> · {esc(to_local(when).strftime('%Y-%m-%d %H:%M %Z'))}",
        f"{len(devices)} device(s) tracked · "
        f"\U0001F7E2 {len(online)} online · \U0001F534 {len(devices) - len(online)} offline",
    ]
    coverage = monitor_coverage(state, when)
    if coverage:
        lines.append(f"\U0001FA7A Monitor coverage: {esc(coverage)}")
    lines.append("")
    for dev in sorted(devices.values(),
                      key=lambda d: (d["status"] != "online", d["name"].lower())):
        stats = analytics(dev, when)
        icon = "\U0001F7E2" if dev["status"] == "online" else "\U0001F534"
        verb = "up" if dev["status"] == "online" else "down"
        lines.append(f"{icon} <b>{esc(dev['name'])}</b> <i>({esc(dev['os'])})</i> "
                     f"- {verb} {fmt_duration(stats['current_seconds'])}")
        bits = []
        if stats["uptime_pct"] is not None:
            bits.append(f"uptime {stats['uptime_pct']:.1f}%")
        if stats["online_per_day"] is not None:
            bits.append(f"{fmt_duration(stats['online_per_day'])}/day")
        bits.append(f"today {fmt_duration(stats['today'])}")
        lines.append("   • " + " · ".join(bits))
        bits = [f"{stats['sessions']} sessions"]
        if stats["avg_session"]:
            bits.append(f"avg {fmt_duration(stats['avg_session'])}")
        if stats["longest_session"]:
            bits.append(f"best {fmt_duration(stats['longest_session'])}")
        if stats["avg_outage"]:
            bits.append(f"avg outage {fmt_duration(stats['avg_outage'])}")
        lines.append("   • " + " · ".join(bits))
        extra = []
        if stats["window"]:
            extra.append(f"usually {stats['window']}")
        if stats["busiest_day"]:
            extra.append(f"peak {stats['busiest_day']}")
        if stats["next_expected"]:
            nxt, ratio = stats["next_expected"]
            extra.append(f"next expected {clock(nxt)} ({ratio * 100:.0f}%)")
        if extra:
            lines.append("   • " + esc(" · ".join(extra)))
        lines.append(f"   • last online {esc(stamp(parse_iso(dev['last_online'])))}")
        lines.append("")
    return "\n".join(lines).rstrip()


def render_startup(state: dict, when: datetime) -> str:
    body = render_digest(state, when, title="monitor started")
    return ("\U0001F680 <b>Tailscale monitor is live</b>\n"
            "Polling every 5 minutes; you only get a message when a device "
            "actually changes state.\n\n" + body)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def record_event(state: dict, event: dict) -> None:
    state["events"].append(event)
    if len(state["events"]) > MAX_EVENTS:
        del state["events"][:-MAX_EVENTS]


def process(state: dict, devices: list[dict], when: datetime) -> list[str]:
    first_run = not state["devices"]
    seen_ids: set[str] = set()
    rendered: list[str] = []

    for device in devices:
        dev_id = device.get("nodeId") or device.get("id")
        if not dev_id:
            continue
        seen_ids.add(dev_id)
        online = is_online(device, when)
        dev = state["devices"].get(dev_id)

        if dev is None:
            dev = new_device_state(device, online, when)
            state["devices"][dev_id] = dev
            event = {"ts": iso(when), "device": dev["name"], "type": "new",
                     "status": dev["status"]}
            record_event(state, event)
            append_log(f"{iso(when)}  NEW      {dev['name']:<24} status={dev['status']}", when)
            log(f"discovered {dev['name']} ({dev['status']})")
            if NOTIFY_NEW_DEVICES and not first_run:
                rendered.append(render_change(event, dev, device, when))
            continue

        # refresh mutable metadata
        dev["name"] = short_name(device)
        dev["os"] = device.get("os", dev.get("os", "unknown"))
        if dev.get("removed"):
            dev["removed"] = False

        # account for the time since the previous poll under the OLD status
        last_sampled = parse_iso(dev.get("last_sampled")) or when
        attribute_interval(dev, last_sampled, when, dev["status"] == "online")
        dev["last_sampled"] = iso(when)
        if online:
            dev["last_online"] = iso(when)

        new_status = "online" if online else "offline"
        if new_status == dev["status"]:
            dev["pending_status"] = None
            dev["pending_count"] = 0
            continue

        # anti-flap: require N consecutive disagreeing polls before flipping
        if dev.get("pending_status") == new_status:
            dev["pending_count"] = dev.get("pending_count", 0) + 1
        else:
            dev["pending_status"] = new_status
            dev["pending_count"] = 1
        if dev["pending_count"] < FLAP_CONFIRMATIONS:
            log(f"{dev['name']}: {new_status} pending "
                f"({dev['pending_count']}/{FLAP_CONFIRMATIONS})")
            continue

        since = parse_iso(dev["since"]) or when
        duration = (when - since).total_seconds()
        closed = {"start": dev["since"], "end": iso(when), "seconds": int(duration)}
        bucket = "sessions" if dev["status"] == "online" else "outages"
        dev[bucket].append(closed)
        if len(dev[bucket]) > MAX_SESSIONS:
            del dev[bucket][:-MAX_SESSIONS]

        dev["status"] = new_status
        dev["since"] = iso(when)
        dev["pending_status"] = None
        dev["pending_count"] = 0
        dev["flips"] = dev.get("flips", 0) + 1
        if new_status == "offline":
            dev["last_offline"] = iso(when)

        event = {"ts": iso(when), "device": dev["name"], "type": new_status,
                 "duration": int(duration)}
        record_event(state, event)
        append_log(
            f"{iso(when)}  {new_status.upper():<8} {dev['name']:<24} "
            f"previous state lasted {fmt_duration(duration)}", when)
        log(f"{dev['name']} -> {new_status} (after {fmt_duration(duration)})")
        rendered.append(render_change(event, dev, device, when))

    # devices that disappeared from the tailnet
    for dev_id, dev in state["devices"].items():
        if dev_id in seen_ids or dev.get("removed"):
            continue
        dev["removed"] = True
        dev["status"] = "offline"
        dev["since"] = iso(when)
        event = {"ts": iso(when), "device": dev["name"], "type": "removed"}
        record_event(state, event)
        append_log(f"{iso(when)}  REMOVED  {dev['name']:<24} no longer in tailnet", when)
        log(f"{dev['name']} removed from tailnet")
        if NOTIFY_REMOVED_DEVICES:
            rendered.append(render_change(event, dev, None, when))

    if first_run:
        append_log(f"{iso(when)}  START    monitor initialised with "
                   f"{len(state['devices'])} device(s)", when)
        return [render_startup(state, when)]
    return [render_batch(rendered, when)] if rendered else []


def digest_due(state: dict, when: datetime) -> bool:
    if FORCE_DIGEST:
        return True
    if not DIGEST_ENABLED:
        return False
    local = to_local(when)
    today = local.strftime("%Y-%m-%d")
    if state.get("last_digest_date") == today:
        return False
    return local.hour >= DIGEST_HOUR


def main() -> int:
    missing = [n for n, v in (("TAILSCALE_API_KEY", TAILSCALE_API_KEY),
                              ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
                              ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)) if not v]
    if missing:
        log(f"FATAL: missing environment variables: {', '.join(missing)}")
        return 2

    when = now_utc()
    state = load_state()

    try:
        devices = fetch_devices()
    except Exception as exc:
        log(f"FATAL: {exc}")
        return 1

    messages = process(state, devices, when)

    if digest_due(state, when) and state["devices"]:
        if not (messages and state["last_run"] is None):   # startup msg is already a digest
            messages.append(render_digest(state, when))
        state["last_digest_date"] = to_local(when).strftime("%Y-%m-%d")

    for message in messages:
        telegram_send(message)
    if not messages:
        online = sum(1 for d in state["devices"].values()
                     if d["status"] == "online" and not d.get("removed"))
        log(f"no changes ({online} online) - staying quiet")

    state["last_run"] = iso(when)
    state["version"] = STATE_VERSION
    save_state(state)
    log("state saved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
