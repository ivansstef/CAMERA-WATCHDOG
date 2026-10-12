#!/usr/bin/env python3
"""
Camera Watchdog - Main Service
Monitors IP cameras for availability and image quality.
"""
import sys
import os
import time
import signal
import logging
import threading
from pathlib import Path
from typing import Dict, Optional

# Add project root to path
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from modules.config_loader import load_config, get_camera_credentials, CameraConfig
from modules.camera_check import ping_camera, check_rtsp_connection, capture_frame, get_stream_info
from modules.image_check import ImageAnalyzer, save_event_frame, cleanup_old_events, ImageIssue
from modules.notifications import TelegramNotifier
from modules.state import StateManager, CameraState, CameraStatus

# Setup logging
def setup_logging(config):
    level = getattr(logging, config.logging.level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler()]
    )
    return logging.getLogger(__name__)


class CameraWatchdog:
    def __init__(self, config_path: str = None):
        self.config = load_config(config_path)
        self.logger = setup_logging(self.config)
        self.running = False
        self.threads: Dict[str, threading.Thread] = {}

        # Initialize components
        self.state_manager = StateManager(self.config.cameras)
        self.image_analyzer = ImageAnalyzer(self.config.image_check)
        self.notifier = TelegramNotifier(self.config.telegram)
        self.username, self.password = get_camera_credentials()

        # Cleanup thread
        self.cleanup_thread: Optional[threading.Thread] = None

        self.logger.info(f"Camera Watchdog initialized with {len(self.config.cameras)} cameras")
        for cam in self.config.get_enabled_cameras():
            self.logger.info(f"  - {cam.name} ({cam.ip})")

    def check_camera(self, camera: CameraConfig):
        """Perform full check cycle for a single camera."""
        status = self.state_manager.get_status(camera.id)
        if not status:
            return

        self.logger.debug(f"Checking {camera.name} ({camera.id})")

        # 1. Ping check
        ping_result = ping_camera(camera.ip, timeout=5.0)

        if not ping_result.success:
            self.logger.warning(f"Ping failed for {camera.name}: {ping_result.error}")
            status.record_failure("ping")
            self._handle_offline(camera, status)
            return

        # 2. RTSP check
        rtsp_result = check_rtsp_connection(camera, self.username, self.password)

        if not rtsp_result.success:
            self.logger.warning(f"RTSP failed for {camera.name}: {rtsp_result.error} ({rtsp_result.result_type.value})")
            status.record_failure("rtsp")
            self._handle_offline(camera, status)
            return

        # 3. Frame capture successful
        frame = rtsp_result.frame
        if frame is None:
            frame = capture_frame(camera, self.username, self.password)

        if frame is None:
            self.logger.warning(f"Frame capture failed for {camera.name}")
            status.record_failure("frame")
            self._handle_offline(camera, status)
            return

        # 4. All checks passed
        status.record_success(ping_result.latency_ms, rtsp_result.latency_ms)
        self.logger.debug(f"{camera.name}: OK (ping={ping_result.latency_ms:.1f}ms, rtsp={rtsp_result.latency_ms:.1f}ms)")

        # 5. Image analysis (if enabled and interval passed)
        self._check_image_quality(camera, status, frame)

        # 6. Handle recovery
        self._handle_recovery(camera, status)

    def _check_image_quality(self, camera: CameraConfig, status: CameraStatus, frame):
        """Check image quality at configured interval."""
        if not self.config.image_check.enabled:
            return

        now = time.time()
        # Check if enough time passed since last image check
        if now - status.last_image_failure < self.config.image_check.interval_seconds:
            return

        self.logger.debug(f"Analyzing image quality for {camera.name}")
        result = self.image_analyzer.analyze_frame(frame, camera)

        if result.issue != ImageIssue.NONE:
            self.logger.warning(f"Image issue for {camera.name}: {result.issue.value} - {result.details}")
            status.record_image_issue(result.issue)

            # Save event frame
            snapshot_path = save_event_frame(
                frame, camera, result.issue, self.config.events.path
            )

            # Send alert
            self.notifier.send_image_issue_alert(
                camera, result.issue, result.details, snapshot_path
            )
        else:
            # Image OK
            status.consecutive_image_failures = 0
            if status.state == CameraState.IMAGE_PROBLEM:
                # Image problem resolved
                self.logger.info(f"Image quality restored for {camera.name}")

    def _handle_offline(self, camera: CameraConfig, status: CameraStatus):
        """Handle offline state transitions."""
        threshold = camera.consecutive_failures_threshold

        if status.consecutive_failures == threshold:
            # Just reached threshold - go offline
            self.logger.warning(f"{camera.name} OFFLINE after {threshold} consecutive failures")
            status.state = CameraState.OFFLINE
            if status.offline_since == 0:
                status.offline_since = time.time()

            # Send alert
            offline_duration = int(status.get_offline_duration())
            self.notifier.send_offline_alert(camera, offline_duration)

    def _handle_recovery(self, camera: CameraConfig, status: CameraStatus):
        """Handle recovery from offline or image problem."""
        if status.state == CameraState.ONLINE and status.total_downtime_seconds > 0:
            # Was offline, now recovered
            offline_duration = int(status.total_downtime_seconds)
            self.logger.info(f"{camera.name} ONLINE again after {offline_duration}s downtime")

            # Send recovery alert
            self.notifier.send_online_alert(camera, offline_duration)

            # Reset downtime counter
            status.total_downtime_seconds = 0

    def run_camera_loop(self, camera: CameraConfig):
        """Main check loop for a single camera."""
        self.logger.info(f"Starting monitor loop for {camera.name}")

        while self.running:
            try:
                start_time = time.time()
                self.check_camera(camera)
                elapsed = time.time() - start_time

                # Sleep for remaining interval
                sleep_time = max(0, camera.check_interval_seconds - elapsed)
                time.sleep(sleep_time)

            except Exception as e:
                self.logger.error(f"Error in camera loop for {camera.name}: {e}")
                time.sleep(5)  # Brief pause on error

    def cleanup_loop(self):
        """Periodic cleanup of old event files."""
        while self.running:
            try:
                time.sleep(self.config.events.cleanup_interval_hours * 3600)
                if self.running:
                    deleted = cleanup_old_events(
                        self.config.events.path,
                        self.config.events.retention_days
                    )
                    if deleted > 0:
                        self.logger.info(f"Cleaned up {deleted} old event files")
            except Exception as e:
                self.logger.error(f"Cleanup error: {e}")

    def start(self):
        """Start monitoring all cameras."""
        self.running = True
        self.logger.info("Starting Camera Watchdog...")

        # Send startup notification
        enabled_cameras = self.config.get_enabled_cameras()
        if enabled_cameras:
            self.notifier.send_startup_message(enabled_cameras)

        # Start camera monitor threads
        for camera in enabled_cameras:
            thread = threading.Thread(
                target=self.run_camera_loop,
                args=(camera,),
                name=f"camera-{camera.id}",
                daemon=True
            )
            thread.start()
            self.threads[camera.id] = thread

        # Start cleanup thread
        self.cleanup_thread = threading.Thread(target=self.cleanup_loop, name="cleanup", daemon=True)
        self.cleanup_thread.start()

        self.logger.info("Camera Watchdog started")

    def stop(self):
        """Stop monitoring."""
        self.logger.info("Stopping Camera Watchdog...")
        self.running = False

        # Wait for threads to finish (with timeout)
        for camera_id, thread in self.threads.items():
            thread.join(timeout=5)

        if self.cleanup_thread:
            self.cleanup_thread.join(timeout=5)

        self.logger.info("Camera Watchdog stopped")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Camera Watchdog Service")
    parser.add_argument('--config', default=None, help='Path to config.yaml')
    parser.add_argument('--test', action='store_true', help='Run single test and exit')
    parser.add_argument('--test-telegram', action='store_true', help='Send test Telegram message and exit')
    args = parser.parse_args()

    watchdog = CameraWatchdog(args.config)

    if args.test_telegram:
        # Just send test message
        success = watchdog.notifier.send_test_message()
        print(f"Test message sent: {success}")
        return 0 if success else 1

    if args.test:
        # Run single check for all cameras
        print("Running single test check...")
        for camera in watchdog.config.get_enabled_cameras():
            print(f"\n--- Checking {camera.name} ({camera.ip}) ---")
            ping_result = ping_camera(camera.ip)
            print(f"Ping: {'OK' if ping_result.success else 'FAIL'} ({ping_result.latency_ms:.1f}ms)")
            if ping_result.success:
                rtsp_result = check_rtsp_connection(camera, watchdog.username, watchdog.password)
                print(f"RTSP: {'OK' if rtsp_result.success else 'FAIL'} ({rtsp_result.result_type.value})")
                if rtsp_result.success:
                    frame = rtsp_result.frame
                    if frame is None:
                        frame = capture_frame(camera, watchdog.username, watchdog.password)
                    print(f"Frame: {'OK' if frame is not None else 'FAIL'}")
                    if frame is not None:
                        result = watchdog.image_analyzer.analyze_frame(frame, camera)
                        print(f"Image: {result.issue.value} - {result.details}")
                        print(f"Metrics: {result.metrics}")
        return 0

    # Setup signal handlers
    def signal_handler(signum, frame):
        watchdog.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Run main loop
    watchdog.start()

    # Keep main thread alive
    try:
        while watchdog.running:
            time.sleep(1)
    except KeyboardInterrupt:
        watchdog.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())