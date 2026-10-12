# Camera Watchdog

Сервіс моніторингу IP-камер ОСББ. Перевіряє доступність камер по IP, RTSP потоки, якість зображення та надсилає сповіщення в Telegram.

## Структура проєкту

```
/opt/osbb/agents/monitoring/camera-watchdog/
├── camera_watchdog.py          # Головний сервіс
├── config.yaml                 # Конфігурація камер та порогів
├── requirements.txt            # Python залежності
├── venv/                       # Віртуальне оточення
├── modules/
│   ├── config_loader.py        # Завантаження конфігу та секретів
│   ├── camera_check.py         # Ping, RTSP, захват кадрів
│   ├── image_check.py          # Аналіз якості зображення
│   ├── notifications.py        # Telegram сповіщення
│   └── state.py                # Управління станом камер
```

## Конфігурація

### config.yaml
Основні параметри:
- `cameras` - список камер з ID, ім'ям, IP
- `check.interval_seconds` - інтервал перевірки (за замовчуванням 30с)
- `check.consecutive_failures_threshold` - кількість невдач для OFFLINE (3)
- `image_check` - пороги для аналізу зображення
- `events.path` - папка для збереження snapshot при аваріях
- `events.retention_days` - зберігання подій (30 днів)
- `telegram.cooldown_minutes` - кулдаун між сповіщеннями (30 хв)

### Секрети
Файл: `/opt/osbb/secrets/camera-watchdog.env` (chmod 600)
```bash
CAMERA_USERNAME=admin
CAMERA_PASSWORD=hikSK555
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=428021663
```

## Запуск

### Ручний запуск (тест)
```bash
cd /opt/osbb/agents/monitoring/camera-watchdog
./venv/bin/python camera_watchdog.py --test
```

### Тест Telegram
```bash
./venv/bin/python camera_watchdog.py --test-telegram
```

### Як systemd сервіс
```bash
sudo systemctl start camera-watchdog
sudo systemctl enable camera-watchdog
sudo systemctl status camera-watchdog
```

## Логи
```bash
# Журнали systemd
sudo journalctl -u camera-watchdog -f

# Останні 100 рядків
sudo journalctl -u camera-watchdog -n 100
```

## Додавання нової камери

Відредагуйте `/opt/osbb/agents/monitoring/camera-watchdog/config.yaml`:

```yaml
cameras:
  - id: entrance1-floor6
    name: "1 під'їзд 6 поверх"
    ip: "192.168.31.201"
    enabled: true
    rtsp:
      path: "/h264/ch1/main/av_stream"

  - id: entrance1-floor5
    name: "1 під'їзд 5 поверх"
    ip: "192.168.31.xxx"
    enabled: true
    rtsp:
      path: "/h264/ch1/main/av_stream"
```

Потім перезапустіть сервіс:
```bash
sudo systemctl restart camera-watchdog
```

## Команди

```bash
# Статус
sudo systemctl status camera-watchdog

# Логи в реальному часі
sudo journalctl -u camera-watchdog -f

# Перезапуск
sudo systemctl restart camera-watchdog

# Зупинка
sudo systemctl stop camera-watchdog

# Перевірка конфігурації
./venv/bin/python camera_watchdog.py --test

# Тест Telegram
./venv/bin/python camera_watchdog.py --test-telegram
```

## RTSP URL для Seven камер

Основний потік (main): `rtsp://admin:password@IP/h264/ch1/main/av_stream`
Додатковий потік (sub): `rtsp://admin:password@IP/h264/ch1/sub/av_stream`

Порти камери:
- 80 - веб-інтерфейс
- 443 - HTTPS
- 554 - RTSP
- 8000 - альтернативний веб/ONVIF

## Аналіз зображення

Система виявляє:
- **Чорний кадр** - mean < 0.02
- **Білий кадр** - mean > 0.98
- **Низький контраст** - std < 0.03
- **Заблокований об'єктив** - локальна дисперсія < 0.015
- **Розмите зображення** - Laplacian variance < 50
- **Зависний кадр** - perceptual hash similarity > 0.995

Сповіщення надсилаються лише при зміні стану та з кулдауном 30 хв.