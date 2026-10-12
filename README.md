# Camera Watchdog

Сервіс моніторингу IP-камер. Перевіряє доступність камер по IP, RTSP-потоки, якість зображення та надсилає сповіщення в Telegram при зміні стану.

## Features

- **Доступність**: ping + TCP-перевірка RTSP порту (554)
- **RTSP**: захват кадру з основного/додаткового потоку
- **Аналіз зображення** (OpenCV):
  - Чорний кадр (mean < 0.02)
  - Білий кадр (mean > 0.98)
  - Низький контраст (std < 0.03)
  - Заблокований об'єктив (локальна дисперсія < 0.015)
  - Розмите зображення (Laplacian variance < 50)
  - Завислий кадр (perceptual hash similarity > 0.995, phash/SSIM/diff)
- **Telegram сповіщення**: тільки на зміні стану + кулдаун 30 хв (налаштовується)
- **Snapshot** в сповіщеннях при проблемах зображення
- **State persistence**: переживає рестарт без хибних тривог

## Структура проєкту

```
camera-watchdog/
├── camera_watchdog.py          # Головний сервіс
├── config.yaml                 # Конфігурація камер та порогів (без секретів)
├── requirements.txt            # Python залежності
├── .env.example                # Шаблон секретів (скопіюй в .env)
├── modules/
│   ├── config_loader.py        # Завантаження конфігу та секретів
│   ├── camera_check.py         # Ping, RTSP, захват кадрів
│   ├── image_check.py          # Аналіз якості зображення
│   ├── notifications.py        # Telegram сповіщення
│   └── state.py                # Управління станом камер
```

## Встановлення

```bash
git clone git@github.com:ivansstef/CAMERA-WATCHDOG.git
cd CAMERA-WATCHDOG
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## Конфігурація

### 1. Секрети (`.env` — НЕ комітиться)

```bash
cp .env.example .env
chmod 600 .env
```

```bash
# .env
CAMERA_USERNAME=admin
CAMERA_PASSWORD=<пароль камер>
TELEGRAM_BOT_TOKEN=<токен бота з @BotFather>
TELEGRAM_CHAT_ID=<chat id>
```

### 2. Камери (`config.yaml`)

```yaml
cameras:
  - id: entrance1-floor6
    name: "1 під'їзд 6 поверх"
    ip: "192.168.0.101"          # IP камери у своїй мережі
    enabled: true
    rtsp:
      path: "/h264/ch1/main/av_stream"   # Hikvision-стиль RTSP path
      transport: "tcp"
      timeout: 10
```

Основні параметри:
- `check.interval_seconds` - інтервал перевірки (30с)
- `check.consecutive_failures_threshold` - невдач підряд для OFFLINE (3)
- `image_check` - пороги аналізу зображення (тюняться під камеру)
- `events.retention_days` - зберігання snapshot подій (30 днів)
- `telegram.cooldown_minutes` - кулдаун між сповіщеннями (30 хв)

## Запуск

### Ручний запуск (тест)

```bash
./venv/bin/python camera_watchdog.py --test
```

### Тест Telegram

```bash
./venv/bin/python camera_watchdog.py --test-telegram
```

### systemd сервіс

```ini
# /etc/systemd/system/camera-watchdog.service
[Unit]
Description=Camera Watchdog - IP camera monitoring
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

## Логи

```bash
sudo journalctl -u camera-watchdog -f        # реальний час
sudo journalctl -u camera-watchdog -n 100    # останні 100
```

## Додавання камери

Додай блок у `config.yaml` → `cameras:` і перезапусти:

```bash
sudo systemctl restart camera-watchdog
```

Кожна камера підтримує override глобальних інтервалів (`check_interval_seconds`, `consecutive_failures_threshold`).

## RTSP URL шаблон

```
rtsp://{username}:{password}@{ip}{path}
```

- Основний потік: `/h264/ch1/main/av_stream` (Hikvision-стиль)
- Додатковий потік: `/h264/ch1/sub/av_stream`
- Порти камери: 80 (web), 443, 554 (RTSP), 8000 (ONVIF)

## Privacy / Security

`config.yaml` у репо — робочий приклад. Реальні IP камер, паролі, токен Telegram бота і chat ID живуть у некомітьованому `.env` (див. `.env.example`).

## Ліцензія

MIT