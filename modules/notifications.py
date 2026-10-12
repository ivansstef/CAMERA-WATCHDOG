"""
Camera Watchdog - Telegram Notifications (v2)
Sending alerts to Telegram with cooldown, anti-spam, SQLite-persisted cooldown state,
and optional digest mode (periodic summary instead of instant alerts).
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
    def __init__(self, config: TelegramConfig, alert_store=None):
        """
        alert_store: modules.state.StateStore (optional) — persist cooldowns across restarts.
        """
        self.config = config
        self.alert_store = alert_store
        self.alert_states: dict[str, AlertState] = {}
        self.enabled = config.enabled and bool(config.bot_token) and bool(config.chat_id)

        if not self.enabled:
            logger.warning("Telegram notifications disabled (missing token or chat_id)")
        elif alert_store:
            for key, (ts, issue, cnt) in alert_store.load_alert_states().items():
                self.alert_states[key] = AlertState(last_alert_time=ts, last_issue=issue, alert_count=cnt)

    def send_motion_alert(self, camera, trigger: dict) -> bool:
        """v2.1: ROI motion alert з підписаним снапшотом (box на зоні)."""
        if not self._can_send_alert(camera.id, f"motion_{trigger['region']}"):
            return False
        text = (
            "📹 <b>CAMERA WATCHDOG — РУХ</b>\n\n"
            f"🎯 <b>{camera.name}</b> ({camera.ip})\n"
            f"Зона: <b>{trigger['region']}</b>\n"
            f"Змінено: {trigger['change_pct']}% (поріг {trigger['threshold_pct']}%)\n"
            f"Час: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        photo = None
        if trigger.get('snapshot'):
            import tempfile
            with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp:
                tmp.write(trigger['snapshot'])
                photo = tmp.name
        success = self._send_message(text, photo_path=photo)
        if photo:
            import os
            try:
                os.unlink(photo)
            except OSError:
                pass
        if success:
            self._record_alert(camera.id, f"motion_{trigger['region']}")
        return success

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

        cooldown_seconds = self.config.cooldown_minutes * 60
        if time.time() - state.last_alert_time >= cooldown_seconds:
            return True

        return state.last_issue != issue_type

    def _record_alert(self, camera_id: str, issue_type: str):
        """Record that an alert was sent (memory + SQLite)."""
        key = self._get_state_key(camera_id, issue_type)
        state = self.alert_states.get(key, AlertState())
        state.last_alert_time = time.time()
        state.last_issue = issue_type
        state.alert_count += 1
        self.alert_states[key] = state

        if self.alert_store:
            try:
                self.alert_store.save_alert(key, state.last_alert_time, state.last_issue, state.alert_count)
            except Exception as e:
                logger.debug(f"Alert persist failed for {key}: {e}")

    def _clear_alert(self, camera_id: str, issue_type: str):
        key = self._get_state_key(camera_id, issue_type)
        self.alert_states.pop(key, None)
        if self.alert_store:
            try:
                self.alert_store.delete_alert(key)
            except Exception:
                pass

    def _send_message(self, text: str, photo_path: str | None = None) -> bool:
        """Send message to Telegram, optionally with photo."""
        if not self.enabled:
            return False

        try:
            base_url = f"https://api.telegram.org/bot{self.config.bot_token}"

            if photo_path and Path(photo_path).exists():
                url = f"{base_url}/sendPhoto"
                with open(photo_path, 'rb') as photo:
                    files = {'photo': photo}
                    data = {'chat_id': self.config.chat_id, 'caption': text, 'parse_mode': 'HTML'}
                    response = requests.post(url, files=files, data=data, timeout=10)
            else:
                url = f"{base_url}/sendMessage"
                data = {'chat_id': self.config.chat_id, 'text': text, 'parse_mode': 'HTML'}
                response = requests.post(url, json=data, timeout=10)

            if response.status_code == 200:
                return True
            logger.error(f"Telegram API error: {response.status_code} - {response.text[:200]}")
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
            self._clear_alert(camera.id, "offline")
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

        success = self._send_message(
            text, photo_path=snapshot_path if self.config.send_snapshot_on_image_issue else None)
        if success:
            self._record_alert(camera.id, f"image_{issue.value}")
        return success

    def send_startup_message(self, cameras: list[CameraConfig]) -> bool:
        """Send startup notification with list of monitored cameras."""
        if not self.enabled:
            return False

        cam_list = "\n".join([f"• {c.name} ({c.ip})" for c in cameras if c.enabled])

        text = (
            "📹 <b>CAMERA WATCHDOG v2</b>\n\n"
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
            "📹 <b>CAMERA WATCHDOG v2</b>\n\n"
            "🧪 <b>Тестове повідомлення</b>\n\n"
            "Система моніторингу камер працює.\n"
            f"Час: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        )

        return self._send_message(text)

    def send_digest(self, cameras: list[CameraConfig], statuses: dict) -> bool:
        """
        Digest mode: one summary message instead of N separate alerts.
        statuses: {camera_id: CameraStatus}
        """
        if not self.enabled:
            return False

        lines = []
        for c in cameras:
            st = statuses.get(c.id)
            if not st:
                continue
            emoji = {"online": "🟢", "offline": "🔴", "image_problem": "⚠️", "unknown": "⚫"}.get(
                st.state.value, "⚫")
            downtime = f" (простій {int(st.total_downtime_seconds)}с)" if st.total_downtime_seconds else ""
            lines.append(f"{emoji} {c.name} — {st.state.value}{downtime}")

        if not lines:
            return False

        text = (
            "📹 <b>CAMERA WATCHDOG — DIGEST</b>\n\n"
            f"{len(cameras)} камер:\n" + "\n".join(lines) +
            f"\n\nЧас: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        return self._send_message(text)