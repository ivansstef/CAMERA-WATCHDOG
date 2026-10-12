"""
Camera Watchdog - Configuration Loader
Loads config.yaml and secrets from environment file.
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
    rtsp_path: str = "/h264/ch1/main/av_stream"
    rtsp_transport: str = "tcp"
    rtsp_timeout: int = 10
    check_interval_seconds: int = 30
    consecutive_failures_threshold: int = 3

    @property
    def rtsp_url(self) -> str:
        """RTSP URL without credentials (credentials added at runtime)"""
        return f"rtsp://{self.ip}{self.rtsp_path}"


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
    frozen_frame_min_interval_minutes: int = 5
    frozen_frame_similarity_threshold: float = 0.995
    frozen_frame_method: str = "phash"


@dataclass
class EventsConfig:
    path: str = "/opt/osbb/data/camera-watchdog/events"
    retention_days: int = 30
    cleanup_interval_hours: int = 24


@dataclass
class TelegramConfig:
    enabled: bool = True
    cooldown_minutes: int = 30
    send_snapshot_on_image_issue: bool = True
    bot_token: str = ""
    chat_id: str = ""


@dataclass
class LoggingConfig:
    level: str = "INFO"
    handler: str = "journald"
    file_path: str = ""


@dataclass
class Config:
    cameras: dict[str, CameraConfig] = field(default_factory=dict)
    check: CheckConfig = field(default_factory=CheckConfig)
    image_check: ImageCheckConfig = field(default_factory=ImageCheckConfig)
    events: EventsConfig = field(default_factory=EventsConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    def get_camera(self, camera_id: str) -> CameraConfig | None:
        return self.cameras.get(camera_id)

    def get_enabled_cameras(self) -> list[CameraConfig]:
        return [c for c in self.cameras.values() if c.enabled]


def load_config(config_path: str = None) -> Config:
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

    # Load cameras
    for cam_data in raw_config.get('cameras', []):
        camera = CameraConfig(
            id=cam_data['id'],
            name=cam_data['name'],
            ip=cam_data['ip'],
            enabled=cam_data.get('enabled', True),
            rtsp_path=cam_data.get('rtsp', {}).get('path', '/h264/ch1/main/av_stream'),
            rtsp_transport=cam_data.get('rtsp', {}).get('transport', 'tcp'),
            rtsp_timeout=cam_data.get('rtsp', {}).get('timeout', 10),
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
        frozen_frame_min_interval_minutes=frozen_data.get('min_interval_minutes', 5),
        frozen_frame_similarity_threshold=frozen_data.get('similarity_threshold', 0.995),
        frozen_frame_method=frozen_data.get('method', 'phash')
    )

    # Load events config
    events_data = raw_config.get('events', {})
    config.events = EventsConfig(
        path=events_data.get('path', '/opt/osbb/data/camera-watchdog/events'),
        retention_days=events_data.get('retention_days', 30),
        cleanup_interval_hours=events_data.get('cleanup_interval_hours', 24)
    )

    # Load telegram config
    tg_data = raw_config.get('telegram', {})
    config.telegram = TelegramConfig(
        enabled=tg_data.get('enabled', True),
        cooldown_minutes=tg_data.get('cooldown_minutes', 30),
        send_snapshot_on_image_issue=tg_data.get('send_snapshot_on_image_issue', True)
    )

    # Load logging config
    log_data = raw_config.get('logging', {})
    config.logging = LoggingConfig(
        level=log_data.get('level', 'INFO'),
        handler=log_data.get('handler', 'journald'),
        file_path=log_data.get('file_path', '')
    )

    # Load secrets from environment file
    secrets_path = Path("/opt/osbb/secrets/camera-watchdog.env")
    if secrets_path.exists():
        with open(secrets_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ[key] = value

    # Apply secrets to telegram config
    config.telegram.bot_token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    config.telegram.chat_id = os.environ.get('TELEGRAM_CHAT_ID', '')

    return config


def get_camera_credentials() -> tuple[str, str]:
    """Get camera username and password from secrets."""
    username = os.environ.get('CAMERA_USERNAME', 'admin')
    password = os.environ.get('CAMERA_PASSWORD', '')
    return username, password


def build_rtsp_url(camera: CameraConfig, username: str = None, password: str = None) -> str:
    """Build RTSP URL with credentials."""
    if username is None or password is None:
        username, password = get_camera_credentials()
    return f"rtsp://{username}:{password}@{camera.ip}{camera.rtsp_path}"