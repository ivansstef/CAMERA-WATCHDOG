"""
Camera Watchdog - State Management (v2)
Tracks camera state, failures, and alerts.
v2: SQLite persistence — uptime stats and alert cooldowns survive restarts.
"""
import logging
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from modules.config_loader import CameraConfig
from modules.image_check import ImageIssue

logger = logging.getLogger(__name__)


class CameraState(Enum):
    UNKNOWN = "unknown"
    ONLINE = "online"
    OFFLINE = "offline"
    IMAGE_PROBLEM = "image_problem"
    STOLEN = "stolen"          # v2: вимкнена користувачем ("вкрадена") до /resume


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
    theft_prompted: bool = False   # v2: чи вже питаємо "вкрали?"

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

        elif self.state in (CameraState.IMAGE_PROBLEM, CameraState.UNKNOWN):
            self.state = CameraState.ONLINE

        # v2.2: успіх скидає pending offline-вікно (флейк не рахується як простій)
        if self.state == CameraState.ONLINE:
            self.offline_since = 0

    def record_failure(self, failure_type: str = "connection"):
        """Record failed check."""
        now = time.time()
        self.last_failure = now
        self.consecutive_failures += 1

        if failure_type == "image":
            self.consecutive_image_failures += 1
            self.last_image_failure = now

        if self.state in (CameraState.ONLINE, CameraState.UNKNOWN) and self.offline_since == 0:
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

    def mark_stolen(self):
        """v2: вкрадена/вимкнена камера — перестає моніторитись до /resume."""
        self.state = CameraState.STOLEN
        self.offline_since = 0
        self.consecutive_failures = 0
        self.consecutive_image_failures = 0

    def resume(self):
        """v2: зняти статус STOLEN — знов моніторимо з нуля."""
        if self.state == CameraState.STOLEN:
            self.state = CameraState.UNKNOWN
            self.total_downtime_seconds = 0
            self.offline_since = 0

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


class StateStore:
    """SQLite-backed persistence for camera statuses + alert cooldowns."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS camera_status (
        camera_id                TEXT PRIMARY KEY,
        state                    TEXT NOT NULL,
        consecutive_failures     INTEGER NOT NULL DEFAULT 0,
        total_downtime_seconds   REAL NOT NULL DEFAULT 0,
        total_uptime_seconds     REAL NOT NULL DEFAULT 0,
        offline_since            REAL NOT NULL DEFAULT 0,
        last_success             REAL NOT NULL DEFAULT 0,
        last_failure             REAL NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS alert_state (
        key                TEXT PRIMARY KEY,   -- '{camera_id}:{issue_type}'
        last_alert_time    REAL NOT NULL DEFAULT 0,
        last_issue         TEXT NOT NULL DEFAULT '',
        alert_count        INTEGER NOT NULL DEFAULT 0
    );
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.executescript(self._SCHEMA)
        self._conn.commit()

    def load_status(self, camera_id: str) -> dict | None:
        cur = self._conn.execute(
            "SELECT state, consecutive_failures, total_downtime_seconds, total_uptime_seconds,"
            " offline_since, last_success, last_failure FROM camera_status WHERE camera_id=?",
            (camera_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        keys = ("state", "consecutive_failures", "total_downtime_seconds", "total_uptime_seconds",
                "offline_since", "last_success", "last_failure")
        return dict(zip(keys, row))

    def load_alert_states(self) -> dict[str, tuple[float, str, int]]:
        """key -> (last_alert_time, last_issue, alert_count)."""
        cur = self._conn.execute("SELECT key, last_alert_time, last_issue, alert_count FROM alert_state")
        return {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}

    def save_status(self, status: "CameraStatus"):
        with self._conn as conn:
            conn.execute(
                "INSERT INTO camera_status (camera_id, state, consecutive_failures,"
                " total_downtime_seconds, total_uptime_seconds, offline_since, last_success, last_failure)"
                " VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(camera_id) DO UPDATE SET"
                "  state=excluded.state,"
                "  consecutive_failures=excluded.consecutive_failures,"
                "  total_downtime_seconds=excluded.total_downtime_seconds,"
                "  total_uptime_seconds=excluded.total_uptime_seconds,"
                "  offline_since=excluded.offline_since,"
                "  last_success=excluded.last_success,"
                "  last_failure=excluded.last_failure",
                (status.camera_id, status.state.value, status.consecutive_failures,
                 status.total_downtime_seconds, status.total_uptime_seconds, status.offline_since,
                 status.last_success, status.last_failure),
            )

    def save_alert(self, key: str, last_alert_time: float, last_issue: str, alert_count: int):
        with self._conn as conn:
            conn.execute(
                "INSERT INTO alert_state (key, last_alert_time, last_issue, alert_count)"
                " VALUES (?,?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET"
                "  last_alert_time=excluded.last_alert_time,"
                "  last_issue=excluded.last_issue,"
                "  alert_count=excluded.alert_count",
                (key, last_alert_time, last_issue, alert_count),
            )

    def delete_alert(self, key: str):
        with self._conn as conn:
            conn.execute("DELETE FROM alert_state WHERE key=?", (key,))

    def close(self):
        self._conn.close()


class StateManager:
    """Manages state for all cameras (in-memory + SQLite persistence)."""

    def __init__(self, cameras: dict[str, CameraConfig], store: StateStore | None = None):
        self.cameras = cameras
        self.store = store
        self.status: dict[str, CameraStatus] = {}
        for cam_id in cameras:
            st = CameraStatus(camera_id=cam_id)
            if store:
                saved = store.load_status(cam_id)
                if saved:
                    try:
                        st.state = CameraState(saved["state"])
                    except ValueError:
                        st.state = CameraState.UNKNOWN
                    st.consecutive_failures = saved["consecutive_failures"]
                    st.total_downtime_seconds = saved["total_downtime_seconds"]
                    st.total_uptime_seconds = saved["total_uptime_seconds"]
                    st.offline_since = saved["offline_since"]
                    st.last_success = saved["last_success"]
                    st.last_failure = saved["last_failure"]
            self.status[cam_id] = st

        # Restore alert cooldowns
        self.alert_states: dict[str, tuple[float, str, int]] = {}
        if store:
            self.alert_states = store.load_alert_states()

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

    def flush(self):
        """Persist all in-memory states to SQLite."""
        if not self.store:
            return
        for st in self.status.values():
            try:
                self.store.save_status(st)
            except Exception as e:
                logger.warning(f"State flush failed for {st.camera_id}: {e}")

    def close(self):
        self.flush()
        if self.store:
            try:
                self.store.close()
            except Exception:
                pass