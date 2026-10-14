# 📹 Camera Watchdog

**Self-hosted IP camera monitoring with image quality analysis, zone-based motion triggers, and an interactive Telegram bot.**

Built for real-world reliability: catches not just outages (ping/RTSP down), but the sneaky failures monitoring usually misses — blocked lenses, frozen streams, blurry or washed-out video — and notifies on state change only, so your phone doesn't drown in duplicate alerts.

Written in pure Python + OpenCV (~2600 LOC, 43 tests), zero external services required (only ffmpeg + a Telegram bot token). Runs 24/7 on any Linux box, VM or container — proven in production monitoring a mixed Hikvision/Dahua camera fleet at an apartment building since 2025.

## Why

Typical uptime monitors ping the camera and call it a day. But cameras fail in ways ping never sees:

- **lens blocked** (dust, tape, insects) — camera is "online", streaming a black square
- **frozen stream** — RTSP keeps sending the same frame for hours (codec stuck)
- **out of focus / dirty lens** — video slowly degrades into a blurry mess
- **washed out** — auto-exposure breakage returns a white or low-contrast feed
- **night IR scenes** — dark low-contrast footage is *normal* at 3 AM, but naive day thresholds fire false alerts

And some things matter only in a specific *part* of the frame: the electrical panel gets opened, a package lands by the door. A motion alert on "the whole camera" is noise; a trigger on a **zone** is signal.

Camera Watchdog grabs frames over RTSP on a schedule and inspects each one with classical CV metrics — no ML models, no cloud.

## Features

### Connectivity
- ICMP ping with automatic TCP fallback (80 / 554 / 443) when raw sockets are unprivileged
- RTSP reachability via `ffprobe` with typed error classification: `timeout` / `connection_refused` / `auth_failed` / `not_found`
- **Vendor fallback probing (v2)**: main path 404 → auto-probes typical paths for Hikvision/Dahua/Reolink/Uniview/ONVIF-generic, remembers which one worked
- `rtsps://` (TLS) support with per-camera `scheme` and `port`
- Frame capture over RTSP (`ffmpeg` → mjpeg pipe → OpenCV), **h264 and H.265/hevc streams both verified**
- ONVIF `GetProfiles` discovery helper (`discover_onvif_profiles()`)
- URL-encoded credentials (passwords with `@ : /` safe)

### Image quality analysis (OpenCV)
| Issue | Detection | Day threshold | Night (IR) threshold |
|---|---|---|---|
| ⬛ Black frame | mean pixel value | < 0.02 | < 0.005 |
| ⬜ White frame | mean pixel value | > 0.98 | – |
| 🌫 Low contrast | pixel std deviation | < 0.03 | < 0.015 |
| 🚫 Blocked lens | ≥80% of 16 blocks flat** | < 0.005 | < 0.0003 |
| 🔍 Blur | Laplacian variance | < 50.0 | < 30 |
| 🧊 Frozen frame | pHash + SSIM + **sensor-noise check (v2.2)** | diff = 0 only | (off by default) |

**v2.1 majority vote**: one smooth patch of asphalt/wall ≠ blocked lens. The whole frame must be textureless (≥80% of blocks flat) — verified on parking/stairwell scenes with large flat areas.
**v2.2 sensor-noise discriminator**: a *live* camera on a static scene has sensor noise (mean abs diff > 0.5); a *stuck codec* reproduces the exact same frame bit-for-bit (diff = 0). Frozen alerts only fire for the latter, and include `diff=` in the alert details for diagnosis.

### Day/Night profiles (v2.3: sun mode)
Fixed hours are wrong twice a year (sunrise 6:00 in March, 7:56 in December). `mode: sun` computes **real sunrise/sunset for your coordinates** (built-in NOAA algorithm, no dependencies):

- **Night** = sunset − 30 min → sunrise + 30 min (offsets configurable), recalculated daily
- Night thresholds (see table): dark IR scenes are evaluated with IR-appropriate limits — no more 7:30 AM "low contrast" false alerts
- Fallback to fixed `{start_hour, end_hour}` if the sun calc ever fails
- **`/location` bot command**: view coordinates + today's Kyiv sunrise/sunset, or change them (`/location 50.45 30.52`) — persisted to config.yaml

### 🎯 ROI Motion (v2.1) — trigger on change *in a zone*
For "camera aimed at the electrical panel" cases:

- `motion_regions` per camera, coordinates in **% of frame** (0-100), any number of zones
- Trigger = % of zone pixels changed vs a **reference frame** (with `pixel_threshold` noise filter)
- **EMA drift adaptation** (5% blend per frame) — gradual light changes (dawn, clouds) never trigger; sudden object/door changes do
- Reference auto-refresh (default 30 min) + reset on trigger (new baseline = post-event state)
- Per-region `change_percent` override; cooldown per camera+zone
- Alert arrives with the **zone boxed in red** and `% changed` in the caption

**Bot tooling for zone setup — no web UI needed:**
- `/roi <cam_id>` — frame with a **10%-labeled grid** → read off coordinates of the panel
- `/roi <cam_id> x1 y1 x2 y2 [name]` — frame with a **red box** → verify the zone before committing
- `/motion` — list all zones; `check <cam_id>` — forced immediate check (ignores cooldown, sends snapshots)

### 🤖 Interactive Telegram bot
| Command | What it does |
|---|---|
| `/status` | All cameras: state emoji, downtime |
| `/shot <id>` | Live snapshot + instant image analysis |
| `/test` | Parallel test of all cameras → summary + snapshots |
| `/roi <id>` / `/roi <id> x1 y1 x2 y2` | Grid overlay / zone preview |
| `/motion` / `/motion check <id>` | Zone list / forced zone check |
| `/mute 60` | Snooze alerts 60 min |
| `/digest` | One summary message instead of N alerts |
| `/stolen <id>` | Mark camera stolen — stop monitoring it |
| `/resume <id>` | Resume monitoring a stolen camera |
| `/location [lat lon]` | View / set coordinates for sun mode |

**Theft handling (v2)**: after 30 min of OFFLINE the bot asks *"stolen?"* with inline buttons:
**🚨 Так, вкрадена** (state → STOLEN, monitoring stops, no alerts until `/resume`) or **⚡ Мережа впала** (keep monitoring).

### Notifications (Telegram)
- Alerts on state change only: **OFFLINE** (🔴), **ONLINE recovery with downtime** (🟢), **image problem** (⚠️ with snapshot), **zone motion** (🎯 with boxed snapshot)
- Per-camera, per-issue cooldown (default 30 min); issue escalation (blurry → frozen) gets through immediately
- **v2.2 flake fix**: a successful check resets the pending offline window — RTSP flakes produce honest 1-minute 🔴→🟢 pairs, not "192 min downtime" from a stale failure
- Startup message listing all monitored cameras; digest mode for large fleets

### Reliability
- **asyncio event loop** (v2) — one async task per camera, blocking subprocess calls in threads; scales to 20+ cameras
- **SQLite state persistence (v2)** — total downtime, consecutive failures and alert cooldowns survive service restarts (verified: `State flush failed` count = 0)
- Configurable failure threshold before OFFLINE (default: 3 consecutive)
- Automatic JPEG event retention with daily cleanup (default 30 days)
- Graceful SIGTERM/SIGINT shutdown (systemd-friendly)
- **Prometheus metrics endpoint (v2)**: state, ping/RTSP latency, total downtime per camera (`metrics.enabled: true`)

## Requirements

- Linux (tested on Ubuntu 22.04/24.04; any distro with ffmpeg works)
- Python 3.12+
- `ffmpeg` / `ffprobe` in PATH (`apt install ffmpeg`)
- A Telegram bot token (@BotFather) + chat id

## Installation

```bash
git clone https://github.com/ivansstef/CAMERA-WATCHDOG.git
cd CAMERA-WATCHDOG
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## Configuration

### 1. Secrets (`.env` — never committed)

```bash
cp .env.example .env
chmod 600 .env
```

```dotenv
CAMERA_USERNAME=admin
CAMERA_PASSWORD=your-camera-password
TELEGRAM_BOT_TOKEN=123456789:AA...your-bot-token
TELEGRAM_CHAT_ID=123456789
```

> Same credentials for all cameras (typical for one site). Per-camera creds: extend
> `build_rtsp_url()` in `modules/config_loader.py`. Secrets load from the project
> `.env` first, then `/opt/camera-watchdog/.env` (deployment).

### 2. Cameras (`config.yaml`)

```yaml
cameras:
  - id: entrance1-floor6
    name: "Entrance 1 / floor 6"
    ip: "192.168.0.101"
    enabled: true
    rtsp:
      path: "/h264/ch1/main/av_stream"
      transport: "tcp"
      timeout: 10
      # scheme: rtsp          # rtsp | rtsps (TLS → port 322 typically)
      # port: 554
      # auth_type: auto
    # Zone for the panel: trigger on change inside this box (% of frame)
    motion_regions:
      - name: shchitok
        x1: 45
        y1: 20
        x2: 65
        y2: 75
```

Vendor RTSP paths cheat-sheet:
- **Hikvision**: `/h264/ch1/main/av_stream` (sub: `/h264/ch1/sub/av_stream`)
- **Dahua**: `/cam/realmonitor?channel=1&subtype=0`
- Others: auto-probed by fallback paths; or camera web UI → Stream settings

### 3. Day/Night + motion tuning

```yaml
check:
  interval_seconds: 30
  consecutive_failures_threshold: 3

theft_prompt_after_minutes: 30

image_check:
  interval_seconds: 60
  frozen_frame:
    enabled: true
    night_enabled: false    # static night entrance = normal
    min_interval_minutes: 5
    similarity_threshold: 0.995
  night_profile:
    mode: sun               # sunrise/sunset based (auto summer/winter)
    lat: 50.45
    lon: 30.52
    timezone: "Europe/Kyiv"
    morning_offset_min: 30
    evening_offset_min: 30
    overrides:
      black_threshold: 0.005
      contrast_threshold: 0.015
      blur_threshold: 30
      blocked_threshold: 0.0003

motion:
  enabled: true
  pixel_threshold: 25       # per-pixel change filter (codec noise)
  change_percent: 2.0       # % of zone pixels changed → trigger
  cooldown_minutes: 2
  reference_refresh_minutes: 30

events:
  path: "data/events"
  retention_days: 30

state_db: "data/state.sqlite3"

telegram:
  cooldown_minutes: 30
  send_snapshot_on_image_issue: true
  digest_minutes: 0         # 0 = instant alerts; N = digest every N min

metrics:
  enabled: false
  port: 8000
```

## Usage

```bash
# One-shot check of every camera (exit code 0 = all ok)
./venv/bin/python camera_watchdog.py --test

# Send a Telegram test message
./venv/bin/python camera_watchdog.py --test-telegram

# Run the service (foreground)
./venv/bin/python camera_watchdog.py

# Alternate config
./venv/bin/python camera_watchdog.py --config /etc/camera-watchdog/config.yaml
```

### systemd unit

```ini
# /etc/systemd/system/camera-watchdog.service
[Unit]
Description=Camera Watchdog — IP camera monitoring
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=camera
WorkingDirectory=/opt/camera-watchdog
EnvironmentFile=/opt/camera-watchdog/.env
ExecStart=/opt/camera-watchdog/venv/bin/python camera_watchdog.py
Restart=on-failure
RestartSec=10
# v2: SQLite state needs a writable data/ dir; ProtectSystem stays strict
ReadWritePaths=/opt/camera-watchdog/data

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now camera-watchdog
sudo setcap cap_net_raw+ep $(readlink -f /opt/camera-watchdog/venv/bin/python3)   # optional: real ICMP
```

## Logs

```bash
journalctl -u camera-watchdog -f          # live
journalctl -u camera-watchdog -n 200      # tail
```

## Project structure

```
CAMERA-WATCHDOG/
├── camera_watchdog.py        # main async service: orchestrator, per-camera tasks, CLI
├── config.yaml               # cameras, thresholds, night/motion config (no secrets)
├── requirements.txt          # opencv, pyyaml, requests, telegram, prometheus-client
├── .env.example              # secrets template
├── pytest.ini
├── tests/
│   ├── test_unit.py          # state persistence, vendor config, detectors, theft, sun mode
│   └── test_roi_motion.py    # zone triggers, EMA drift, cooldown, helpers
└── modules/
    ├── config_loader.py      # dataclass config, YAML + env loading, RTSP URL builder, zones
    ├── camera_check.py       # ping / TCP fallback / ffprobe / ffmpeg capture / ONVIF / fallback probing
    ├── image_check.py        # OpenCV analyzer: detectors + day/night profiles + frozen logic
    ├── roi_motion.py         # zone-based motion: reference, EMA drift, cooldown, grid/box drawings
    ├── suntimes.py           # sunrise/sunset (NOAA) — no dependencies
    ├── notifications.py      # Telegram notifier: cooldowns (SQLite-backed), digest, motion alerts
    ├── metrics.py            # Prometheus endpoint
    ├── state.py              # camera state machine + SQLite StateStore
    └── bot.py                # interactive Telegram bot (10 commands, inline theft buttons)
```

## Testing

```bash
./venv/bin/python -m pytest tests/ -v
```

43 tests: SQLite persistence across restarts, vendor config resolution (rtsps/ports/URL-encoding), all 6 detectors (incl. majority-vote blocked lens and sensor-noise frozen discrimination), zone triggers with EMA drift, theft flow, cooldown math, sun-mode day/night switching.

## Alert flow

```
ping/RTSP fail ──(×3 consecutive)──▶ 🔴 OFFLINE → (30+ min) ──▶ 🤔 "stolen?" buttons
                                                                        │🚨 stolen          │⚡ network
                                                                        ▼                   ▼
                                              STOLEN (silent until /resume)    keep monitoring
success ──▶ 🟢 ONLINE (real downtime duration; flakes → honest 1-min pairs)

frame OK but quality bad ──▶ ⚠️ IMAGE alert (+ JPEG snapshot)
zone pixels changed ──(≥ change_percent, not in cooldown)──▶ 🎯 MOTION alert (+ boxed snapshot)
```

## Extending

- **New camera vendor**: another RTSP `path` in `config.yaml` (fallback probes the rest)
- **New detector**: add a check to `ImageAnalyzer.analyze_frame()`; return `ImageAnalysisResult(...)` and register a display name in `notifications.py`
- **Other messengers**: the notifier is a thin `requests` wrapper — a Slack/Discord port is ~50 LOC mirroring `TelegramNotifier`

## Privacy / Security

- No secrets ever committed; `.gitignore` covers `*.env`, `logs/`, `data/`, `venv/`
- Real camera IPs/passwords/bot tokens live only in `.env` on the deployment host

## License

MIT

## Author

Built and battle-tested by [@ivansstef](https://github.com/ivansstef) for a real apartment-building (ОСББ) camera network.

If this saved you a "the camera worked yesterday" phone call — ⭐ the repo.