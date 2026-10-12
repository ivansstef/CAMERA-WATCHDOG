"""
Camera Watchdog - State Management
Tracks camera state, failures, and alerts.
"""
import logging
import time
from dataclasses import dataclass
from enum import Enum

from modules.config_loader import CameraConfig
from modules.image_check import ImageIssue

logger = logging.getLogger(__name__)


class CameraState(Enum):
    UNKNOWN = "unknown"
    ONLINE = "online"
    OFFLINE = "offline"
    IMAGE_PROBLEM = "image_problem"


@dataclass
class CameraStatus:
    """Current status of a single camera."""
    camera_id: str
    state: CameraState = CameraState.UNKNOWN
    consecutive_failures: int = 0
    consecutive_image_failures: int = 0
    last_success: float = 0
    last_failure: float = 0
    last_image_failure: float = 0
    offline_since: float = 0
    last_ping_latency: float = 0
    last_rtsp_latency: float = 0
    last_frame: bytes | None = None  # Store last successful frame for frozen detection
    last_image_issue: ImageIssue | None = None
    last_image_alert_time: float = 0
    total_downtime_seconds: float = 0
    total_uptime_seconds: float = 0

    def record_success(self, ping_latency: float = 0, rtsp_latency: float = 0):
        """Record successful check."""
        now = time.time()
        self.last_success = now
        self.consecutive_failures = 0
        self.consecutive_image_failures = 0
        self.last_ping_latency = ping_latency
        self.last_rtsp_latency = rtsp_latency

        if self.state == CameraState.OFFLINE:
            # Was offline, now online
            if self.offline_since > 0:
                self.total_downtime_seconds += now - self.offline_since
                self.offline_since = 0
            self.state = CameraState.ONLINE

        elif self.state == CameraState.IMAGE_PROBLEM:
            # Image problem resolved
            self.state = CameraState.ONLINE

        elif self.state == CameraState.UNKNOWN:
            self.state = CameraState.ONLINE

    def record_failure(self, failure_type: str = "connection"):
        """Record failed check."""
        now = time.time()
        self.last_failure = now
        self.consecutive_failures += 1

        if failure_type == "image":
            self.consecutive_image_failures += 1
            self.last_image_failure = now

        if self.state == CameraState.ONLINE and self.offline_since == 0:
            self.offline_since = now

    def record_image_issue(self, issue: ImageIssue):
        """Record image quality issue."""
        now = time.time()
        self.last_image_failure = now
        self.consecutive_image_failures += 1
        self.last_image_issue = issue
        self.last_image_alert_time = now

        if self.state == CameraState.ONLINE:
            self.state = CameraState.IMAGE_PROBLEM
            self.offline_since = now  # Track when problem started

    def get_offline_duration(self) -> float:
        """Get current offline duration in seconds."""
        if self.offline_since > 0:
            return time.time() - self.offline_since
        return 0

    def should_alert_offline(self, threshold: int) -> bool:
        """Check if we should alert for offline (threshold reached, not already alerted)."""
        return (self.consecutive_failures >= threshold and
                self.state == CameraState.OFFLINE and
                self.total_downtime_seconds == 0)  # First time reaching threshold

    def should_alert_recovery(self) -> bool:
        """Check if we should alert for recovery (was offline, now online)."""
        return self.state == CameraState.ONLINE and self.total_downtime_seconds > 0


class StateManager:
    """Manages state for all cameras."""

    def __init__(self, cameras: dict[str, CameraConfig]):
        self.cameras = cameras
        self.status: dict[str, CameraStatus] = {}
        for cam_id in cameras:
            self.status[cam_id] = CameraStatus(camera_id=cam_id)

    def get_status(self, camera_id: str) -> CameraStatus | None:
        return self.status.get(camera_id)

    def get_all_statuses(self) -> dict[str, CameraStatus]:
        return self.status

    def get_online_cameras(self) -> list[str]:
        return [cam_id for cam_id, status in self.status.items()
                if status.state in (CameraState.ONLINE, CameraState.IMAGE_PROBLEM)]

    def get_offline_cameras(self) -> list[str]:
        return [cam_id for cam_id, status in self.status.items()
                if status.state == CameraState.OFFLINE]

    def get_problem_cameras(self) -> list[str]:
        return [cam_id for cam_id, status in self.status.items()
                if status.state == CameraState.IMAGE_PROBLEM]