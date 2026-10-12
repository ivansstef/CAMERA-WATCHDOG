# 📹 Camera Watchdog

**Self-hosted IP camera monitoring with image quality analysis and Telegram alerts.**

Built for real-world reliability: catches not just outages (ping/RTSP down), but the sneaky failures monitoring usually misses — blocked lenses, frozen streams, blurry or washed-out video — and notifies on state change only, so your phone doesn't drown in duplicate alerts.

Written in pure Python + OpenCV, ~1300 LOC, zero external services required (only ffmpeg + a Telegram bot token). Runs 24/7 on any Linux box, VM or container — proven in production monitoring Hikvision cameras at an apartment building (ОСББ) since 2025.

## Why

Typical uptime monitors ping the camera and call it a day. But cameras fail in ways ping never sees:

- **lens blocked** (dust, tape, insects) — camera is "online", streaming a black square
- **frozen stream** — RTSP keeps sending the same frame for hours
- **out of focus / dirty lens** — video slowly degrades into a blurry mess
- **washed out** — auto-exposure breakage returns a white or low-contrast feed

Camera Watchdog grabs frames over RTSP on a schedule and inspects each one with classical CV metrics — no ML models, no cloud, fast enough to run on a 10-year-old NUC.

## Features

### Connectivity
- ICMP ping with automatic TCP fallback (80 / 554 / 443) when raw sockets are unprivileged
- RTSP reachability via `ffprobe` with typed error classification: `timeout` / `connection_refused` / `auth_failed` / `not_found`
- Frame capture over RTSP (`ffmpeg` → mjpeg pipe → OpenCV), main or sub stream

### Image quality analysis (OpenCV)
| Issue | Detection | Default threshold |
|---|---|---|
| ⬛ Black frame | mean pixel value | < 0.02 |
| ⬜ White frame | mean pixel value | > 0.98 |
| 🌫 Low contrast | pixel std deviation | < 0.03 |
| 🚫 Blocked lens | minimum 4×4 block variance | < 0.005 |
| 🔍 Blur | Laplacian variance | < 50.0 |
| 🧊 Frozen frame | perceptual hash (DCT pHash) + SSIM double-check | similarity ≥ 0.995 |

Frozen-frame detection compares frames 5+ minutes apart (configurable) so legitimate static scenes don't trigger false alarms.

### Notifications (Telegram)
- Alerts on state change only: **OFFLINE** (🔴), **ONLINE recovery with downtime duration** (🟢), **image problem** (⚠️ with snapshot attached)
- Per-camera, per-issue cooldown (default 30 min) — issue escalation (e.g. blurry → frozen) still gets through immediately
- Startup message listing all monitored cameras
- `--test-telegram` dry-run flag

### Reliability
- One watcher thread per camera — cameras fail independently
- Configurable failure threshold before declaring OFFLINE (default: 3 consecutive failures)
- Automatic JPEG event retention with daily cleanup (default 30 days)
- Graceful SIGTERM/SIGINT shutdown (systemd-friendly)
- All state tracked in-memory; restart-safe thresholds

## Requirements

- Linux (tested on Ubuntu 22.04/24.04; any distro with ffmpeg works)
- Python 3.12+
- `ffmpeg` / `ffprobe` in PATH (`apt install ffmpeg`)
- A Telegram bot token (@BotFather) + chat id for notifications

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

> The same credentials are shared by all cameras (typical for one location).
> Per-camera credentials: extend `build_rtsp_url()` in `modules/config_loader.py`.
>
> By default secrets load from `/opt/camera-watchdog/.env`; override the path
> (`secrets_path`) in `modules/config_loader.py` for other installs.

### 2. Cameras (`config.yaml`)

```yaml
cameras:
  - id: entrance1-floor6
    name: "Entrance 1 / floor 6"
    ip: "192.168.0.101"
    enabled: true
    rtsp:
      path: "/h264/ch1/main/av_stream"   # Hikvision-style path; adjust for your vendor
      transport: "tcp"                   # tcp | udp
      timeout: 10
    # per-camera overrides (optional):
    # check_interval_seconds: 30
    # consecutive_failures_threshold: 3
```

Vendor RTSP paths cheat-sheet:
- **Hikvision**: `/h264/ch1/main/av_stream` (sub: `/h264/ch1/sub/av_stream`)
- **Dahua**: `/cam/realmonitor?channel=1&subtype=0`
- Other vendors: camera web UI → Stream settings

### 3. Tuning thresholds

All CV thresholds live under `image_check:` in `config.yaml` — tune per environment.
Defaults are proven on Hikvision 1080p outdoor + indoor; loosen `blur_threshold`
for wide-angle/fisheye lenses, which legitimately score low.

## Usage

```bash
# One-shot check of every camera (exit code 0 = all ok)
./venv/bin/python camera_watchdog.py --test

# Send a Telegram test message
./venv/bin/python camera_watchdog.py --test-telegram

# Run the service (foreground)
./venv/bin/python camera_watchdog.py

# Use an alternate config
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
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now camera-watchdog
```

ICMP ping needs raw-socket privileges; without them the service falls back
to TCP checks automatically. Explicit capability (optional):

```bash
sudo setcap cap_net_raw+ep $(readlink -f /opt/camera-watchdog/venv/bin/python3)
```

## Logs

```bash
journalctl -u camera-watchdog -f          # live
journalctl -u camera-watchdog -n 200      # tail
```

A `file` handler with a custom path is available via `logging.handler` in `config.yaml`.

## Project structure

```
CAMERA-WATCHDOG/
├── camera_watchdog.py        # main service: orchestrator, per-camera threads, CLI
├── config.yaml               # cameras + thresholds (example, no secrets)
├── requirements.txt
├── .env.example              # secrets template
└── modules/
    ├── config_loader.py      # dataclass config, YAML + env loading, RTSP URL builder
    ├── camera_check.py       # ping / TCP fallback / ffprobe / ffmpeg frame capture
    ├── image_check.py        # OpenCV analyzer: 6 issue detectors + pHash/SSIM
    ├── notifications.py      # Telegram notifier: cooldown, anti-spam, photo attach
    └── state.py              # per-camera state machine: ONLINE/OFFLINE/IMAGE_PROBLEM
```

## Event snapshots

On every image issue a JPEG snapshot is stored under `events.path`:

```
2026-10-08_08-42-13_entrance1-floor6_frozen_frame.jpg
```

Retention is automatic (`retention_days`, cleanup pass every `cleanup_interval_hours`).

## Alert flow

```
ping fail / RTSP fail / no frame ──(×3 consecutive)──▶ 🔴 OFFLINE alert
                                    │
online again ──▶ 🟢 ONLINE alert (with downtime duration)
                                    │
frame OK but quality bad ──▶ ⚠️ IMAGE alert (+ JPEG snapshot)
                                    │
quality restored ──▶ (state reset, no alert noise)
```

## Extending

- **New camera vendor**: another RTSP `path` in `config.yaml`
- **New detector**: add a check to `ImageAnalyzer.analyze_frame()`; return `ImageAnalysisResult(ImageIssue.YOURS, details, metrics)` and register a display name in `notifications.py`
- **Other messengers**: the notifier is a thin `requests` wrapper — a Slack/Discord port is ~50 LOC mirroring `TelegramNotifier`

## Privacy / Security

- `config.yaml` in the repo is a working example; no secrets ever committed
- Real camera IPs, passwords, bot tokens and chat IDs live in a non-committed `.env` (see `.env.example`, `.gitignore` covers `*.env`, `logs/`, `data/`)

## License

MIT

## Author

Built and battle-tested by [@ivansstef](https://github.com/ivansstef) for a real apartment-building (ОСББ) camera network.

If this saved you a "the camera worked yesterday" phone call — ⭐ the repo.