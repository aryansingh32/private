# Tailscale → Telegram device monitor

Polls the Tailscale API every 5 minutes from a GitHub Actions cron, tracks every
device's online/offline history, and sends a Telegram message **only when a
device actually changes state** — plus one analytics digest per day.

No server, no database, no dependencies (Python standard library only).

```
🟢 laptop is ONLINE
🕐 2026-09-16 09:05:00 IST
⏱ Was offline for 15h
💻 linux · 100.64.0.1 · v1.102.4
📈 Uptime 74.2% · avg session 4h 25m · 5h 8m/day
📅 Typical window: 09:00-18:00
```

## What it does

| | |
|---|---|
| **Polls** | every 5 min via `schedule: '*/5 * * * *'` |
| **Detects** | online / offline / new device / device removed from tailnet |
| **Notifies** | one Telegram message per change — several changes in the same poll are merged into a single message |
| **Stays quiet** | no change → no message (a typical tailnet produces ~2–10 messages/day instead of 288 polls) |
| **Logs** | `logs/YYYY-MM.log`, one timestamped line per transition, kept forever |
| **Analyses** | uptime %, session count, average/longest session, average outage, hours online per day, typical online window, busiest weekday, and a prediction of when an offline device usually comes back |
| **Digests** | full per-device analytics once a day at `DIGEST_HOUR` |
| **Self-reports** | a failed run (expired API token, API outage) pings Telegram instead of dying silently |

## How state survives between runs

Each run is a fresh container, so the monitor keeps its memory on a dedicated
branch (`monitor-state`), rewritten as **a single commit** each poll:

```
monitor-state
├── state/state.json     device states, session history, hourly/daily buckets
└── logs/2026-09.log     append-only transition log
```

`main` stays clean — no 288-commits-a-day history, and the repo does not grow.

## Setup

**1. Push this folder to a GitHub repo** (see the warning about public vs private below):

```bash
cd tailscale-telegram-monitor
git init -b main
git add .
git commit -m "Tailscale to Telegram device monitor"
git remote add origin https://github.com/<you>/tailscale-telegram-monitor.git
git push -u origin main
```

**2. Add the secrets** — repo → *Settings* → *Secrets and variables* → *Actions* → *Secrets*:

| Secret | Value |
|---|---|
| `TAILSCALE_API_KEY` | Tailscale **API access token** (`tskey-api-…`) from [login.tailscale.com/admin/settings/keys](https://login.tailscale.com/admin/settings/keys) |
| `TELEGRAM_BOT_TOKEN` | the token @BotFather gave you |
| `TELEGRAM_CHAT_ID` | your chat id (from `https://api.telegram.org/bot<TOKEN>/getUpdates` → `result[].message.chat.id`) |

**3. Optionally add variables** (same page, *Variables* tab) — all have defaults:

`TIMEZONE` (e.g. `Asia/Kolkata`), `DIGEST_HOUR` (default `9`), `TAILNET`
(default `-`), `FLAP_CONFIRMATIONS`, `ONLINE_SOURCE`.

**4. Enable and test**: *Actions* tab → enable workflows → *Tailscale monitor* →
*Run workflow*. The first run sends a startup summary of every device; after
that it only speaks up on changes.

## Cost: use a **public** repo

GitHub bills Actions **per started minute**. A 5-minute cron is 288 runs/day, so
even though each run takes ~20 s it bills ~288 min/day ≈ **8,600 min/month** —
far past the 2,000 free minutes on a private repo.

* **Public repo → Actions minutes are free and unlimited.** This is the
  configuration this project is built for.
* **Private repo?** Change the cron to `'*/30 * * * *'` (≈1,440 min/month, fits
  the free tier) and accept 30-minute detection latency.

What is visible in a public repo: device short names, OS, and uptime statistics
on the `monitor-state` branch. Tailscale IPs, user emails and client versions are
fetched live per run and go **only** to Telegram — they are never written to the
repo. Your tokens live in encrypted GitHub Secrets and are masked in logs.

## Two things that will eventually break it

1. **Tailscale API tokens expire** (90 days by default). Regenerate the token,
   update the secret. You will get a Telegram failure alert when it happens.
2. **GitHub disables cron workflows after 60 days of repository inactivity.**
   The state pushes usually count as activity; if GitHub emails you anyway, hit
   *Enable workflow* in the Actions tab or push any commit.

Also note GitHub's scheduler is best-effort — `*/5` can drift by several minutes
under load. The script measures real elapsed time, so drift changes detection
latency but never corrupts the statistics.

## Local testing

```bash
cp .env.example .env      # fill in your values
./run_local.sh            # DRY_RUN=true in .env prints instead of sending
python3 tests/simulate.py # 5.5 days of fake polls, offline, no messages sent
```

## Configuration reference

| Variable | Default | Meaning |
|---|---|---|
| `TAILNET` | `-` | tailnet name; `-` = the token's own tailnet |
| `TIMEZONE` | `UTC` | IANA zone for timestamps, daily buckets and analytics |
| `DIGEST_HOUR` | `9` | local hour for the daily analytics report |
| `DIGEST_ENABLED` | `true` | turn the daily report off |
| `FORCE_DIGEST` | `false` | send the digest on this run (workflow_dispatch input) |
| `FLAP_CONFIRMATIONS` | `1` | consecutive polls that must agree before a flip is announced; `2` suppresses one-poll flaps |
| `ONLINE_SOURCE` | `auto` | `auto` trusts the API's `online`/`connectedToControl` flag; `lastseen` judges purely by `lastSeen` age |
| `ONLINE_THRESHOLD_SECONDS` | `900` | how stale `lastSeen` may be and still count as online (fallback path) |
| `MAX_GAP_SECONDS` | `7200` | gaps longer than this are excluded from statistics (monitor was down — status unknown) |
| `NOTIFY_NEW_DEVICES` / `NOTIFY_REMOVED_DEVICES` | `true` | announce tailnet membership changes |
| `MAX_SESSIONS` / `MAX_DAYS` / `MAX_EVENTS` | `200` / `90` / `500` | history retention caps |
| `STATE_FILE` / `LOG_DIR` | `state/state.json` / `logs` | where state and logs live |
| `DRY_RUN` | `false` | print messages instead of sending them |

## How the analytics work

Every poll attributes the elapsed interval since the previous poll to the
device's status, split across local hour-of-day, weekday and calendar-day
buckets. Transitions close a session and append it to the history.

* **Typical window** — contiguous local hours where the device is online ≥50% of
  the observed time in that hour (needs ≥15 min of observation per hour bucket).
* **Next expected online** — the first upcoming hour in the next 48 h whose
  historical online ratio is ≥50%, reported with that ratio as confidence.
* **Uptime %** — online seconds ÷ observed seconds, excluding gaps where the
  monitor itself was down.

Accuracy is bounded by the poll interval: a transition is timestamped at the
poll that detected it, so it can be up to ~5 minutes late.
