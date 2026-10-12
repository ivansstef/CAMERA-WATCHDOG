#!/usr/bin/env python3
"""
Camera Watchdog v2 - Main Service
Monitors IP cameras for availability and image quality.
v2: asyncio event loop (scales to 20+ cameras), CLI test-commands preserved.
"""
import argparse
import asyncio
import logging
import signal
import sys
import time
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from modules.camera_check import (
    capture_frame,
    check_rtsp_connection,
    ping_camera,
)
from modules.config_loader import CameraConfig, get_camera_credentials, load_config
from modules.image_check import (
    ImageAnalyzer,
    ImageIssue,
    cleanup_old_events,
    save_event_frame,
)
from modules.notifications import TelegramNotifier
from modules.state import CameraState, CameraStatus, StateManager, StateStore

logger = logging.getLogger("camera_watchdog")


def setup_logging(config):
    level = getattr(logging, config.logging.level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler()]
    )
    return logging.getLogger(__name__)


class CameraWatchdog:
    def __init__(self, config_path: str | None = None):
        self.config = load_config(config_path)
        self.logger = setup_logging(self.config)
        self.running = False
        self.tasks: dict[str, asyncio.Task] = {}
        self.bot = None   # v2: WatchdogBot (для theft-prompt), тип — modules.bot.WatchdogBot

        # v2: SQLite-backed state store
        self.store = StateStore(self.config.state_db)
        self.state_manager = StateManager(self.config.cameras, store=self.store)
        self.image_analyzer = ImageAnalyzer(self.config.image_check)
        self.notifier = TelegramNotifier(self.config.telegram, alert_store=self.store)
        self.username, self.password = get_camera_credentials()
        # v2.1: ROI motion detector
        from modules.roi_motion import ROIMotionDetector
        self.motion_detector = ROIMotionDetector(self.config.motion)
        self.bot = None   # v2: WatchdogBot (для theft-prompt)

        self.logger.info(f"Camera Watchdog v2 initialized with {len(self.config.cameras)} cameras")
        for cam in self.config.get_enabled_cameras():
            self.logger.info(f"  - {cam.name} ({cam.ip})")

    async def check_camera(self, camera: CameraConfig):
        """Perform full check cycle for a single camera."""
        status = self.state_manager.get_status(camera.id)
        if not status:
            return

        # v2: украдена/вимкнена камера — пропускаємо моніторинг до /resume
        if status.state == CameraState.STOLEN:
            self.logger.debug(f"{camera.name}: STOLEN — skipped")
            return

        self.logger.debug(f"Checking {camera.name} ({camera.id})")

        # 1. Ping check (subprocess blocking → thread)
        ping_result = await asyncio.to_thread(ping_camera, camera.ip, 5.0)

        if not ping_result.success:
            self.logger.warning(f"Ping failed for {camera.name}: {ping_result.error}")
            status.record_failure("ping")
            self._handle_offline(camera, status)
            self._maybe_theft_prompt(camera, status)
            self.state_manager.flush()
            return

        # 2. RTSP check (ffprobe + ffmpeg blocking → thread)
        rtsp_result = await asyncio.to_thread(
            check_rtsp_connection, camera, self.username, self.password
        )

        if not rtsp_result.success:
            self.logger.warning(
                f"RTSP failed for {camera.name}: {rtsp_result.error} ({rtsp_result.result_type.value})")
            status.record_failure("rtsp")
            self._handle_offline(camera, status)
            self.state_manager.flush()
            return

        # 3. Frame capture successful
        frame = rtsp_result.frame
        if frame is None:
            frame = await asyncio.to_thread(capture_frame, camera, self.username, self.password)

        if frame is None:
            self.logger.warning(f"Frame capture failed for {camera.name}")
            status.record_failure("frame")
            self._handle_offline(camera, status)
            self.state_manager.flush()
            return

        # 4. All checks passed
        status.record_success(ping_result.latency_ms, rtsp_result.latency_ms)
        self.logger.debug(
            f"{camera.name}: OK (ping={ping_result.latency_ms:.1f}ms, rtsp={rtsp_result.latency_ms:.1f}ms)")

        # 5. Image analysis (if enabled and interval passed)
        await self._check_image_quality(camera, status, frame)

        # v2.1: ROI motion (тригер на зміну в заданій зоні)
        await self._check_roi_motion(camera, frame)

        # 6. Handle recovery
        self._handle_recovery(camera, status)
        self.state_manager.flush()

    async def _check_roi_motion(self, camera: CameraConfig, frame):
        """v2.1: перевірка motion-зон камери."""
        if not camera.motion_regions or not getattr(self, 'motion_detector', None):
            return
        triggers = await asyncio.to_thread(
            self.motion_detector.check_frame, camera.id, frame)
        for t in triggers:
            if not t['cooldown_ok']:
                continue
            self.logger.info(
                f"ROI MOTION {camera.id}/{t['region']}: {t['change_pct']}% "
                f"(поріг {t['threshold_pct']}%)")
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, self.notifier.send_motion_alert, camera, t)

    async def _check_image_quality(self, camera: CameraConfig, status: CameraStatus, frame):
        """Check image quality at configured interval."""
        if not self.config.image_check.enabled:
            return

        now = time.time()
        if now - status.last_image_failure < self.config.image_check.interval_seconds:
            return

        self.logger.debug(f"Analyzing image quality for {camera.name}")
        # v2: day/night профіль порогів застосовується всередині analyze_frame
        result = await asyncio.to_thread(self.image_analyzer.analyze_frame, frame, camera)

        if result.issue != ImageIssue.NONE:
            self.logger.warning(f"Image issue for {camera.name}: {result.issue.value} - {result.details}")
            status.record_image_issue(result.issue)

            snapshot_path = save_event_frame(
                frame, camera, result.issue, self.config.events.path
            )

            self.notifier.send_image_issue_alert(
                camera, result.issue, result.details, snapshot_path
            )
        else:
            status.consecutive_image_failures = 0
            if status.state == CameraState.IMAGE_PROBLEM:
                self.logger.info(f"Image quality restored for {camera.name}")

    def _handle_offline(self, camera: CameraConfig, status: CameraStatus):
        """Handle offline state transitions."""
        threshold = camera.consecutive_failures_threshold

        if status.consecutive_failures == threshold:
            self.logger.warning(f"{camera.name} OFFLINE after {threshold} consecutive failures")
            status.state = CameraState.OFFLINE
            if status.offline_since == 0:
                status.offline_since = time.time()

            offline_duration = int(status.get_offline_duration())
            self.notifier.send_offline_alert(camera, offline_duration)

    def _maybe_theft_prompt(self, camera: CameraConfig, status: CameraStatus):
        """
        v2: камера offline довше theft_after_minutes → один раз питаємо "вкрали?"
        У відповідь отримує inline кнопки; STOLEN вимикає моніторинг до /resume.
        """
        after_min = self.config.theft_prompt_after_minutes
        if after_min <= 0:
            return
        if status.state != CameraState.OFFLINE:
            return
        offline_min = status.get_offline_duration() / 60
        if offline_min >= after_min and not status.theft_prompted:
            status.theft_prompted = True
            # Async-постінг промпту (з thread-контексту через thread-safe hook)
            asyncio.get_event_loop().call_soon_threadsafe(
                self._theft_prompt_async, camera.id)

    def _theft_prompt_async(self, camera_id: str):
        """Викликається із thread → планує async prompt в loop."""
        if not self.bot:
            return None
        return asyncio.create_task(self.bot_prompt_theft(camera_id))

    async def bot_prompt_theft(self, camera_id: str):
        """Надсилає 'вкрали?' prompt з inline-кнопками (через бот)."""
        if not self.bot or not self.bot.app:
            return
        camera = self.config.get_camera(camera_id)
        if not camera:
            return
        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🚨 Так, вкрадена — чекати /resume", callback_data=f"stolen:{camera_id}"),
                 InlineKeyboardButton("⚡ Мережа впала — моніторити далі", callback_data=f"resume:{camera_id}")]
            ])
            await self.bot.app.bot.send_message(
                chat_id=self.config.telegram.chat_id,
                text=(f"🤔 <b>{camera.name}</b> ({camera.ip}) офлайн вже "
                      f"{self.config.theft_prompt_after_minutes}+ хв\n\n"
                      "Вкрадена? Саботована? Треба вимкнути моніторинг?"),
                parse_mode="HTML", reply_markup=kb)
        except Exception as e:
            self.logger.warning(f"Theft prompt failed for {camera_id}: {e}")

    def _handle_recovery(self, camera: CameraConfig, status: CameraStatus):
        """Handle recovery from offline or image problem."""
        if status.state == CameraState.ONLINE and status.total_downtime_seconds > 0:
            offline_duration = int(status.total_downtime_seconds)
            self.logger.info(f"{camera.name} ONLINE again after {offline_duration}s downtime")

            self.notifier.send_online_alert(camera, offline_duration)
            status.total_downtime_seconds = 0

    async def run_camera_loop(self, camera: CameraConfig):
        """Main check loop for a single camera."""
        self.logger.info(f"Starting async monitor loop for {camera.name}")

        while self.running:
            try:
                start_time = time.time()
                await self.check_camera(camera)
                elapsed = time.time() - start_time

                sleep_time = max(0, camera.check_interval_seconds - elapsed)
                await asyncio.sleep(sleep_time)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger.error(f"Error in camera loop for {camera.name}: {e}")
                await asyncio.sleep(5)

    async def cleanup_loop(self):
        """Periodic cleanup of old event files."""
        while self.running:
            try:
                await asyncio.sleep(self.config.events.cleanup_interval_hours * 3600)
                if self.running:
                    deleted = await asyncio.to_thread(
                        cleanup_old_events,
                        self.config.events.path,
                        self.config.events.retention_days
                    )
                    if deleted > 0:
                        self.logger.info(f"Cleaned up {deleted} old event files")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger.error(f"Cleanup error: {e}")

    async def start(self):
        """Start monitoring all cameras."""
        self.running = True
        self.logger.info("Starting Camera Watchdog v2...")

        enabled_cameras = self.config.get_enabled_cameras()
        if enabled_cameras:
            await asyncio.to_thread(self.notifier.send_startup_message, enabled_cameras)

        for camera in enabled_cameras:
            task = asyncio.create_task(self.run_camera_loop(camera), name=f"camera-{camera.id}")
            self.tasks[camera.id] = task

        self.tasks["_cleanup"] = asyncio.create_task(self.cleanup_loop(), name="cleanup")

        self.logger.info("Camera Watchdog v2 started")

    async def stop(self):
        """Stop monitoring."""
        self.logger.info("Stopping Camera Watchdog...")
        self.running = False

        for _camera_id, task in self.tasks.items():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)

        self.state_manager.close()
        self.logger.info("Camera Watchdog v2 stopped")


async def _run_service(args) -> int:
    watchdog = CameraWatchdog(args.config)

    if args.test_telegram:
        success = await asyncio.to_thread(watchdog.notifier.send_test_message)
        print(f"Test message sent: {success}")
        await asyncio.to_thread(watchdog.state_manager.close)
        return 0 if success else 1

    if args.test:
        print("Running single test check...")
        username, password = watchdog.username, watchdog.password
        loop = asyncio.get_running_loop()
        for camera in watchdog.config.get_enabled_cameras():
            print(f"\n--- Checking {camera.name} ({camera.ip}) ---")
            ping_result = await loop.run_in_executor(None, ping_camera, camera.ip)
            print(f"Ping: {'OK' if ping_result.success else 'FAIL'} ({ping_result.latency_ms:.1f}ms)")
            if ping_result.success:
                rtsp_result = await loop.run_in_executor(
                    None, check_rtsp_connection, camera, username, password)
                print(f"RTSP: {'OK' if rtsp_result.success else 'FAIL'} ({rtsp_result.result_type.value})")
                if rtsp_result.success:
                    frame = rtsp_result.frame
                    if frame is None:
                        frame = await loop.run_in_executor(
                            None, capture_frame, camera, username, password)
                    print(f"Frame: {'OK' if frame is not None else 'FAIL'}")
                    if frame is not None:
                        result = await loop.run_in_executor(
                            None, watchdog.image_analyzer.analyze_frame, frame, camera)
                        print(f"Image: {result.issue.value} - {result.details}")
                        print(f"Metrics: {result.metrics}")
        await asyncio.to_thread(watchdog.state_manager.close)
        return 0

    # Graceful shutdown on SIGTERM/SIGINT
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    await watchdog.start()

    # v2: Telegram inline bot (optional)
    from modules.bot import WatchdogBot
    bot = WatchdogBot(watchdog)
    watchdog.bot = bot   # для theft-prompt
    bot_app = await bot.start()

    # v2: metrics endpoint (optional)
    if watchdog.config.metrics.enabled:
        from modules.metrics import start_metrics_server
        await start_metrics_server(watchdog, watchdog.config.metrics.port)

    try:
        await stop_event.wait()
    finally:
        try:
            await bot.stop()
        except Exception:
            pass
        await watchdog.stop()
    return 0


def main():
    parser = argparse.ArgumentParser(description="Camera Watchdog Service v2")
    parser.add_argument('--config', default=None, help='Path to config.yaml')
    parser.add_argument('--test', action='store_true', help='Run single test and exit')
    parser.add_argument('--test-telegram', action='store_true', help='Send test Telegram message and exit')
    args = parser.parse_args()

    try:
        return asyncio.run(_run_service(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())