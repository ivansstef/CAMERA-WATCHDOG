"""
Camera Watchdog - Telegram Notifications
Handles sending alerts to Telegram with cooldown and anti-spam.
"""
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from modules.config_loader import CameraConfig, TelegramConfig
from modules.image_check import ImageIssue

logger = logging.getLogger(__name__)


@dataclass
class AlertState:
    """Track alert state per camera per issue type."""
    last_alert_time: float = 0
    last_issue: str = ""
    alert_count: int = 0


class TelegramNotifier:
    def __init__(self, config: TelegramConfig):
        self.config = config
        self.alert_states: dict[str, AlertState] = {}  # key: f"{camera_id}:{issue_type}"
        self.enabled = config.enabled and bool(config.bot_token) and bool(config.chat_id)

        if not self.enabled:
            logger.warning("Telegram notifications disabled (missing token or chat_id)")

    def _get_state_key(self, camera_id: str, issue_type: str) -> str:
        return f"{camera_id}:{issue_type}"

    def _can_send_alert(self, camera_id: str, issue_type: str) -> bool:
        """Check if we can send an alert (cooldown logic)."""
        if not self.enabled:
            return False

        key = self._get_state_key(camera_id, issue_type)
        state = self.alert_states.get(key)

        if state is None:
            return True

        # Check cooldown
        cooldown_seconds = self.config.cooldown_minutes * 60
        if time.time() - state.last_alert_time >= cooldown_seconds:
            return True

        # Allow repeat alert if issue type changed
        if state.last_issue != issue_type:
            return True

        return False

    def _record_alert(self, camera_id: str, issue_type: str):
        """Record that an alert was sent."""
        key = self._get_state_key(camera_id, issue_type)
        state = self.alert_states.get(key, AlertState())
        state.last_alert_time = time.time()
        state.last_issue = issue_type
        state.alert_count += 1
        self.alert_states[key] = state

    def _send_message(self, text: str, photo_path: str | None = None) -> bool:
        """Send message to Telegram, optionally with photo."""
        if not self.enabled:
            return False

        try:
            base_url = f"https://api.telegram.org/bot{self.config.bot_token}"

            if photo_path and Path(photo_path).exists():
                # Send photo with caption
                url = f"{base_url}/sendPhoto"
                with open(photo_path, 'rb') as photo:
                    files = {'photo': photo}
                    data = {'chat_id': self.config.chat_id, 'caption': text, 'parse_mode': 'HTML'}
                    response = requests.post(url, files=files, data=data, timeout=10)
            else:
                # Send text message
                url = f"{base_url}/sendMessage"
                data = {'chat_id': self.config.chat_id, 'text': text, 'parse_mode': 'HTML'}
                response = requests.post(url, json=data, timeout=10)

            if response.status_code == 200:
                return True
            else:
                logger.error(f"Telegram API error: {response.status_code} - {response.text}")
                return False

        except requests.Timeout:
            logger.error("Telegram request timeout")
            return False
        except Exception as e:
            logger.error(f"Telegram send error: {e}")
            return False

    def send_offline_alert(self, camera: CameraConfig, downtime_seconds: int = 0) -> bool:
        """Send camera OFFLINE alert."""
        if not self._can_send_alert(camera.id, "offline"):
            return False

        downtime_str = ""
        if downtime_seconds > 0:
            mins = downtime_seconds // 60
            secs = downtime_seconds % 60
            downtime_str = f"\nНедоступна: {mins} хв {secs} с"

        text = (
            "📹 <b>CAMERA WATCHDOG</b>\n\n"
            "🔴 <b>КАМЕРА OFFLINE</b>\n\n"
            f"{camera.name}\n"
            f"IP: {camera.ip}{downtime_str}\n"
            f"Час: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        )

        success = self._send_message(text)
        if success:
            self._record_alert(camera.id, "offline")
        return success

    def send_online_alert(self, camera: CameraConfig, offline_duration_seconds: int = 0) -> bool:
        """Send camera ONLINE (recovery) alert."""
        if not self._can_send_alert(camera.id, "online"):
            return False

        downtime_str = ""
        if offline_duration_seconds > 0:
            mins = offline_duration_seconds // 60
            secs = offline_duration_seconds % 60
            downtime_str = f"\nПростій: {mins} хв {secs} с"

        text = (
            "📹 <b>CAMERA WATCHDOG</b>\n\n"
            "🟢 <b>КАМЕРА ONLINE</b>\n\n"
            f"{camera.name}\n"
            f"IP: {camera.ip}{downtime_str}\n"
            f"Відновлено: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        )

        success = self._send_message(text)
        if success:
            # Clear offline state
            key = self._get_state_key(camera.id, "offline")
            if key in self.alert_states:
                del self.alert_states[key]
            self._record_alert(camera.id, "online")
        return success

    def send_image_issue_alert(self, camera: CameraConfig, issue: ImageIssue,
                               details: str, snapshot_path: str | None = None) -> bool:
        """Send image quality issue alert."""
        issue_names = {
            ImageIssue.BLACK: "Чорний кадр",
            ImageIssue.WHITE: "Білий кадр",
            ImageIssue.LOW_CONTRAST: "Низький контраст",
            ImageIssue.BLOCKED: "Заблокований об'єктив",
            ImageIssue.BLURRY: "Розмите зображення",
            ImageIssue.FROZEN: "Зависний кадр",
        }

        issue_name = issue_names.get(issue, issue.value)

        if not self._can_send_alert(camera.id, f"image_{issue.value}"):
            return False

        text = (
            "📹 <b>CAMERA WATCHDOG</b>\n\n"
            f"⚠️ <b>ПРОБЛЕМА ІЗ ЗОБРАЖЕННЯМ</b>\n\n"
            f"{camera.name}\n"
            f"IP: {camera.ip}\n"
            f"Причина: {issue_name}\n"
            f"Деталі: {details}\n"
            f"Час: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        )

        success = self._send_message(text, photo_path=snapshot_path if self.config.send_snapshot_on_image_issue else None)
        if success:
            self._record_alert(camera.id, f"image_{issue.value}")
        return success

    def send_startup_message(self, cameras: list[CameraConfig]) -> bool:
        """Send startup notification with list of monitored cameras."""
        if not self.enabled:
            return False

        cam_list = "\n".join([f"• {c.name} ({c.ip})" for c in cameras if c.enabled])

        text = (
            "📹 <b>CAMERA WATCHDOG</b>\n\n"
            "✅ <b>Camera Watchdog запущено</b>\n\n"
            f"Моніторинг активний для {len(cameras)} камер:\n"
            f"{cam_list}\n\n"
            f"Час: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        )

        return self._send_message(text)

    def send_test_message(self) -> bool:
        """Send a test message."""
        if not self.enabled:
            return False

        text = (
            "📹 <b>CAMERA WATCHDOG</b>\n\n"
            "🧪 <b>Тестове повідомлення</b>\n\n"
            "Система моніторингу камер працює.\n"
            f"Час: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        )

        return self._send_message(text)