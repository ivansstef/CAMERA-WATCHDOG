"""
Camera Watchdog v2.1 - ROI Motion Detection
Тригер на ЗМІНУ в заданій зоні кадру (не на рух загалом).
Класика: камера на електрощиток → алерт тільки коли хтось відкрив щиток.

Механіка:
- reference кадр зони зберігається; різниця (absdiff) з поточним кадром
- pixel_threshold фільтрує шум кодека/сенсора
- change_percent: скільки % пікселів зони мають змінитись для тригера
- reference оновлюється періодично (reference_refresh_minutes), щоб плавні
  зміни освітлення не давали фальшивих спрацювань
- cooldown на алерт; після тригера reference скидається одразу
"""
import logging
import time
from dataclasses import dataclass, field

import cv2
import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class MotionRegion:
    """Зона інтересу в % від розміру кадру (0..100)."""
    name: str
    x1: float
    y1: float
    x2: float
    y2: float
    # per-region override глобального change_percent (optional)
    change_percent: float | None = None

    def to_pixels(self, w: int, h: int) -> tuple[int, int, int, int]:
        px1 = max(0, min(w - 1, int(self.x1 / 100 * w)))
        py1 = max(0, min(h - 1, int(self.y1 / 100 * h)))
        px2 = max(px1 + 1, min(w, int(self.x2 / 100 * w)))
        py2 = max(py1 + 1, min(h, int(self.y2 / 100 * h)))
        return px1, py1, px2, py2


@dataclass
class MotionConfig:
    enabled: bool = False
    pixel_threshold: int = 25          # різниця яркості пікселя (0-255) = "змінено"
    change_percent: float = 2.0        # % пікселів зони що змінились = тригер
    cooldown_minutes: float = 2.0      # мін між алертами по одній зоні
    reference_refresh_minutes: float = 30  # оновлення reference (плавне світло не лякає)
    min_region_area: int = 400         # захист від вироджених зон


@dataclass
class RegionState:
    reference: np.ndarray | None = None
    reference_time: float = 0.0
    last_alert_time: float = 0.0


class ROIMotionDetector:
    def __init__(self, config: MotionConfig, regions: list[MotionRegion] | None = None):
        self.config = config
        self.regions: list[MotionRegion] = regions or []
        self.states: dict[str, RegionState] = {}   # key: f"{cam_id}:{region.name}"

    def _get_state(self, cam_id: str, region: MotionRegion) -> RegionState:
        key = f"{cam_id}:{region.name}"
        if key not in self.states:
            self.states[key] = RegionState()
        return self.states[key]

    def check_frame(self, cam_id: str, frame: np.ndarray) -> list[dict]:
        """
        Перевірка всіх зон камери на поточному кадрі.
        Returns list of trigger dicts: [{region, name, change_pct, snapshot(jpeg bytes)|None, cooldown_ok}]
        """
        if not self.config.enabled or not self.regions:
            return []
        if frame is None or frame.size == 0:
            return []

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
        now = time.time()
        triggers = []

        for region in self.regions:
            x1, y1, x2, y2 = region.to_pixels(gray.shape[1], gray.shape[0])
            if (x2 - x1) * (y2 - y1) < self.config.min_region_area:
                logger.warning(f"{cam_id}: region {region.name} занадто мала — skipped")
                continue

            current = gray[y1:y2, x1:x2]
            state = self._get_state(cam_id, region)
            threshold_pct = region.change_percent if region.change_percent is not None else self.config.change_percent

            # Нема reference — ставимо і чекаємо наступного кадру
            refresh = self.config.reference_refresh_minutes * 60
            if state.reference is None or now - state.reference_time >= refresh:
                state.reference = current.copy()
                state.reference_time = now
                continue

            # absdiff → % змінених пікселів
            diff = cv2.absdiff(current, state.reference)
            changed_pct = float((diff > self.config.pixel_threshold).mean() * 100)

            if changed_pct >= threshold_pct:
                cooldown_ok = (now - state.last_alert_time) >= self.config.cooldown_minutes * 60
                snapshot = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes() if cooldown_ok else None
                # вирізку зони поверх снапшота — box
                if snapshot and cooldown_ok:
                    vis = frame.copy()
                    cv2.rectangle(vis, (x1, y1), (x2, y2), (0, 0, 255), 3)
                    cv2.putText(vis, f"{region.name}: {changed_pct:.1f}%", (x1, max(20, y1 - 8)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
                    snapshot = cv2.imencode('.jpg', vis, [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes()
                if cooldown_ok:
                    state.last_alert_time = now
                    # після тригера reference = поточний кадр (нова базова норма)
                    state.reference = current.copy()
                    state.reference_time = now
                triggers.append({
                    'camera_id': cam_id,
                    'region': region.name,
                    'change_pct': round(changed_pct, 2),
                    'threshold_pct': threshold_pct,
                    'snapshot': snapshot,
                    'cooldown_ok': cooldown_ok,
                })
            else:
                # легка дрейф-адаптація: повільне оновлення reference (EMA 5%)
                state.reference = cv2.addWeighted(state.reference, 0.95, current, 0.05, 0)

        return triggers


def draw_region_grid(frame: np.ndarray, cell_pct: int = 10) -> np.ndarray:
    """Сітка cell_pct% — щоб зручно виміряти зону щитка по картинці."""
    vis = frame.copy()
    h, w = vis.shape[:2]
    step_x, step_y = int(w * cell_pct / 100), int(h * cell_pct / 100)
    for i, x in enumerate(range(0, w, step_x)):
        cv2.line(vis, (x, 0), (x, h), (0, 255, 0), 1)
        if i % 2 == 0 and x + 12 < w:
            cv2.putText(vis, str(int(x / w * 100 / 10) * 10), (x + 3, 14),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 0), 1)
    for i, y in enumerate(range(0, h, step_y)):
        cv2.line(vis, (0, y), (w, y), (0, 255, 0), 1)
        if i % 2 == 0 and y + 14 < h:
            cv2.putText(vis, str(int(y / h * 100 / 10) * 10), (3, y + 12),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 0), 1)
    return vis


def draw_region_box(frame: np.ndarray, region: MotionRegion) -> np.ndarray:
    """Червоний прямокутник zone на кадрі (перевірка налаштування)."""
    vis = frame.copy()
    h, w = vis.shape[:2]
    x1, y1, x2, y2 = region.to_pixels(w, h)
    cv2.rectangle(vis, (x1, y1), (x2, y2), (0, 0, 255), 3)
    label = region.name + (f" ({region.change_percent}%)" if region.change_percent else "")
    cv2.putText(vis, label, (x1, max(18, y1 - 8)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
    return vis


def parse_region_args(args: list[str], name: str = "custom") -> MotionRegion | None:
    """'/roi cam 30 20 60 90' → MotionRegion(30,20,60,90)."""
    try:
        x1, y1, x2, y2 = (float(a) for a in args[:4])
        if not (0 <= x1 < x2 <= 100 and 0 <= y1 < y2 <= 100):
            return None
        return MotionRegion(name=name, x1=x1, y1=y1, x2=x2, y2=y2)
    except (ValueError, TypeError):
        return None