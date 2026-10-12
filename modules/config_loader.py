"""
Camera Watchdog - Configuration Loader (v2)
Loads config.yaml and secrets from environment file.
v2: multi-vendor support — rtsps, per-camera auth_type, ONVIF auto-discovery,
    RTSP path fallback probing.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class CameraConfig:
    id: str
    name: str
    ip: str
    enabled: bool = True
    scheme: str = "rtsp"                     # rtsp | rtsps (TLS, порт 322 зазвичай)
    port: int = 554
    rtsp_path: str = "/h264/ch1/main/av_stream"
    rtsp_transport: str = "tcp"              # tcp | udp
    rtsp_timeout: int = 10
    auth_type: str = "auto"                  # basic | digest | auto
    check_interval_seconds: int = 30
    consecutive_failures_threshold: int = 3
    fallback_paths: list[str] = field(default_factory=list)  # пробінг якщо основний 404
    motion_regions: list = field(default_factory=list)       # v2.1: MotionRegion[]

    @property
    def rtsp_url(self) -> str:
        """RTSP URL без кредів (кредитенcіали додаються на рантаймі)."""
        return f"{self.scheme}://{self.ip}:{self.port}{self.rtsp_path}"


@dataclass
class CheckConfig:
    interval_seconds: int = 30
    consecutive_failures_threshold: int = 3


@dataclass
class ImageCheckConfig:
    enabled: bool = True
    interval_seconds: int = 60
    consecutive_failures: int = 3
    black_threshold: float = 0.02
    white_threshold: float = 0.98
    contrast_threshold: float = 0.03
    blocked_threshold: float = 0.015
    blur_threshold: float = 50.0
    frozen_frame_enabled: bool = True
    frozen_frame_night_enabled: bool = True   # v2.1: ніччю замкнений під'їзд = статичність норма
    frozen_frame_min_interval_minutes: int = 5
    frozen_frame_similarity_threshold: float = 0.995
    frozen_frame_method: str = "phash"
    night_profile: dict | None = None        # v2: {start_hour, end_hour, overrides{...}}


@dataclass
class EventsConfig:
    path: str = "data/events"
    retention_days: int = 30
    cleanup_interval_hours: int = 24


@dataclass
class TelegramConfig:
    enabled: bool = True
    cooldown_minutes: int = 30
    send_snapshot_on_image_issue: bool = True
    bot_token: str = ""
    chat_id: str = ""
    allowed_users: list[str] = field(default_factory=list)
    digest_minutes: int = 0                  # v2: зведення раз на N хв (0 = миттєві алерти)


@dataclass
class LoggingConfig:
    level: str = "INFO"
    handler: str = "journald"
    file_path: str = ""


@dataclass
class MetricsConfig:
    enabled: bool = False
    port: int = 8000


@dataclass
class MotionConfig:
    """v2.1: ROI motion detection (тригер на зміну в заданій зоні кадру)."""
    enabled: bool = False
    pixel_threshold: int = 25
    change_percent: float = 2.0
    cooldown_minutes: float = 2.0
    reference_refresh_minutes: float = 30
    min_region_area: int = 400


@dataclass
class Config:
    cameras: dict[str, CameraConfig] = field(default_factory=dict)
    check: CheckConfig = field(default_factory=CheckConfig)
    image_check: ImageCheckConfig = field(default_factory=ImageCheckConfig)
    events: EventsConfig = field(default_factory=EventsConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)
    motion: MotionConfig = field(default_factory=MotionConfig)   # v2.1
    state_db: str = "data/state.sqlite3"     # v2: persist
    theft_prompt_after_minutes: int = 30     # v2: камера offline N хв → "вкрали?" prompt

    def get_camera(self, camera_id: str) -> CameraConfig | None:
        return self.cameras.get(camera_id)

    def get_enabled_cameras(self) -> list[CameraConfig]:
        return [c for c in self.cameras.values() if c.enabled]


# Типові RTSP-шляхи для авто-пробінгу якщо основний не відповідає
VENDOR_FALLBACK_PATHS = [
    "/h264/ch1/main/av_stream",                                # Hikvision
    "/cam/realmonitor?channel=1&subtype=0",                    # Dahua
    "/stream1",                                                # Reolink / TP-Link
    "/live/ch00_1",                                            # Uniview / HiWatch
    "/onvif1",                                                 # ONVIF generic
    "/user=admin&password=&channel=1&stream=0.sdp?realstream",  # Hikvision legacy
]


def load_config(config_path: str | Path | None = None) -> Config:
    """Load configuration from YAML file and environment secrets."""
    if config_path is None:
        config_path = Path(__file__).parent.parent / "config.yaml"
    else:
        config_path = Path(config_path)

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, 'r', encoding='utf-8') as f:
        raw_config = yaml.safe_load(f)

    config = Config()
    base_dir = config_path.parent

    # v2: відносні шляхи резолвимо від теки конфіга
    config.state_db = str((base_dir / raw_config.get("state_db", "data/state.sqlite3")).resolve())
    config.theft_prompt_after_minutes = int(raw_config.get("theft_prompt_after_minutes", 30))

    # Load cameras
    for cam_data in raw_config.get('cameras', []):
        rtsp_data = cam_data.get('rtsp', {})
        scheme = rtsp_data.get('scheme', 'rtsp')
        default_port = 322 if scheme == 'rtsps' else 554
        camera = CameraConfig(
            id=cam_data['id'],
            name=cam_data['name'],
            ip=cam_data['ip'],
            enabled=cam_data.get('enabled', True),
            scheme=scheme,
            port=int(rtsp_data.get('port', default_port)),
            rtsp_path=rtsp_data.get('path', '/h264/ch1/main/av_stream'),
            rtsp_transport=rtsp_data.get('transport', 'tcp'),
            rtsp_timeout=int(rtsp_data.get('timeout', 10)),
            auth_type=rtsp_data.get('auth_type', 'auto'),
            fallback_paths=rtsp_data.get('fallback_paths', VENDOR_FALLBACK_PATHS),
            check_interval_seconds=cam_data.get('check_interval_seconds',
                                                raw_config.get('check', {}).get('interval_seconds', 30)),
            consecutive_failures_threshold=cam_data.get('consecutive_failures_threshold',
                                                        raw_config.get('check', {}).get('consecutive_failures_threshold', 3))
        )
        config.cameras[camera.id] = camera

    # Load global check config
    check_data = raw_config.get('check', {})
    config.check = CheckConfig(
        interval_seconds=check_data.get('interval_seconds', 30),
        consecutive_failures_threshold=check_data.get('consecutive_failures_threshold', 3)
    )

    # Load image check config
    img_data = raw_config.get('image_check', {})
    frozen_data = img_data.get('frozen_frame', {})
    config.image_check = ImageCheckConfig(
        enabled=img_data.get('enabled', True),
        interval_seconds=img_data.get('interval_seconds', 60),
        consecutive_failures=img_data.get('consecutive_failures', 3),
        black_threshold=img_data.get('black_threshold', 0.02),
        white_threshold=img_data.get('white_threshold', 0.98),
        contrast_threshold=img_data.get('contrast_threshold', 0.03),
        blocked_threshold=img_data.get('blocked_threshold', 0.015),
        blur_threshold=img_data.get('blur_threshold', 50.0),
        frozen_frame_enabled=frozen_data.get('enabled', True),
        frozen_frame_night_enabled=frozen_data.get('night_enabled', True),
        frozen_frame_min_interval_minutes=frozen_data.get('min_interval_minutes', 5),
        frozen_frame_similarity_threshold=frozen_data.get('similarity_threshold', 0.995),
        frozen_frame_method=frozen_data.get('method', 'phash'),
        night_profile=img_data.get('night_profile')
    )

    # Load events config
    events_data = raw_config.get('events', {})
    events_path = events_data.get('path', 'data/events')
    if not Path(events_path).is_absolute():
        events_path = str((base_dir / events_path).resolve())
    config.events = EventsConfig(
        path=events_path,
        retention_days=events_data.get('retention_days', 30),
        cleanup_interval_hours=events_data.get('cleanup_interval_hours', 24)
    )

    # Load telegram config
    tg_data = raw_config.get('telegram', {})
    config.telegram = TelegramConfig(
        enabled=tg_data.get('enabled', True),
        cooldown_minutes=tg_data.get('cooldown_minutes', 30),
        send_snapshot_on_image_issue=tg_data.get('send_snapshot_on_image_issue', True),
        allowed_users=[str(u) for u in tg_data.get('allowed_users', [])],
        digest_minutes=tg_data.get('digest_minutes', 0)
    )

    # Load logging config
    log_data = raw_config.get('logging', {})
    config.logging = LoggingConfig(
        level=log_data.get('level', 'INFO'),
        handler=log_data.get('handler', 'journald'),
        file_path=log_data.get('file_path', '')
    )

    # Load metrics config (v2)
    m_data = raw_config.get('metrics', {})
    config.metrics = MetricsConfig(
        enabled=m_data.get('enabled', False),
        port=int(m_data.get('port', 8000))
    )

    # Load motion config (v2.1)
    mo_data = raw_config.get('motion', {})
    config.motion = MotionConfig(
        enabled=mo_data.get('enabled', False),
        pixel_threshold=int(mo_data.get('pixel_threshold', 25)),
        change_percent=float(mo_data.get('change_percent', 2.0)),
        cooldown_minutes=float(mo_data.get('cooldown_minutes', 2.0)),
        reference_refresh_minutes=float(mo_data.get('reference_refresh_minutes', 30)),
        min_region_area=int(mo_data.get('min_region_area', 400))
    )

    # v2.1: per-camera motion_regions (в % кадру)
    for cam_data in raw_config.get('cameras', []):
        regions_raw = cam_data.get('motion_regions', [])
        if regions_raw:
            from modules.roi_motion import MotionRegion
            config.cameras[cam_data['id']].__dict__  # ensure object exists
            config.cameras[cam_data['id']].motion_regions = [
                MotionRegion(
                    name=r.get('name', 'zone1'),
                    x1=float(r['x1']), y1=float(r['y1']),
                    x2=float(r['x2']), y2=float(r['y2']),
                    change_percent=r.get('change_percent'),
                ) for r in regions_raw
            ]

    # Load secrets: local .env спершу (розробка), потім deployment env-file
    for env_candidate in (base_dir / ".env", Path("/opt/camera-watchdog/.env")):
        if env_candidate.exists():
            with open(env_candidate, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, value = line.split('=', 1)
                        os.environ.setdefault(key, value)
            break

    # Apply secrets to telegram config
    config.telegram.bot_token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    config.telegram.chat_id = os.environ.get('TELEGRAM_CHAT_ID', '')

    return config


def get_camera_credentials() -> tuple[str, str]:
    """Get camera username and password from secrets."""
    username = os.environ.get('CAMERA_USERNAME', 'admin')
    password = os.environ.get('CAMERA_PASSWORD', '')
    return username, password


def build_rtsp_url(camera: CameraConfig, username: str | None = None,
                   password: str | None = None, path: str | None = None) -> str:
    """Build RTSP(S) URL with URL-encoded credentials. Optional path override (fallback probing)."""
    if username is None or password is None:
        username, password = get_camera_credentials()
    p = path or camera.rtsp_path
    from urllib.parse import quote
    user = quote(username, safe="")
    pwd = quote(password, safe="")
    creds = user + (f":{pwd}" if pwd else "")
    return f"{camera.scheme}://{creds}@{camera.ip}:{camera.port}{p}"