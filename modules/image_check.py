"""
Camera Watchdog - Image Analysis (v2)
Detects black/white frames, low contrast, blocked lens, blur, frozen frames.
v2: day/night threshold profiles — night scenes legitimately darker → different thresholds.
"""
import datetime
import logging
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import cv2
import numpy as np

from modules.config_loader import CameraConfig, ImageCheckConfig

logger = logging.getLogger(__name__)


class ImageIssue(Enum):
    NONE = "none"
    BLACK = "black_frame"
    WHITE = "white_frame"
    LOW_CONTRAST = "low_contrast"
    BLOCKED = "blocked_lens"
    BLURRY = "blurry"
    FROZEN = "frozen_frame"


@dataclass
class ImageAnalysisResult:
    issue: ImageIssue = ImageIssue.NONE
    details: str = ""
    metrics: dict[str, float] | None = None
    frame: np.ndarray | None = None
    profile: str = "day"   # v2: який профіль застосовувався

    def __post_init__(self):
        if self.metrics is None:
            self.metrics = {}


class ImageAnalyzer:
    def __init__(self, config: ImageCheckConfig):
        self.config = config
        self.last_frames: dict[str, tuple[np.ndarray, float]] = {}
        self.last_phashes: dict[str, tuple[str, float]] = {}
        self._effective: dict[str, dict] = {}   # v2: пороги що діють зараз (по камерах)

    def _active_profile(self) -> tuple[str, dict]:
        """
        v2.2: day/night профіль з трьома режимами:
        - {start_hour, end_hour} — фіксовані години (Kyiv за замовчуванням)
        - mode: sun (coords у конфігу) — за сходом/заходом: ніч = [захід-30м, схід+30м]
          самостійно підлаштовується літо/зима (жовтень схід 7:56 замість 6:00)
        Returns (profile_name, effective_thresholds).
        """
        base = {
            'black_threshold': self.config.black_threshold,
            'white_threshold': self.config.white_threshold,
            'contrast_threshold': self.config.contrast_threshold,
            'blocked_threshold': self.config.blocked_threshold,
            'blur_threshold': self.config.blur_threshold,
            'frozen_frame_similarity_threshold': self.config.frozen_frame_similarity_threshold,
        }
        prof = self.config.night_profile
        if not prof:
            return "day", base

        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(prof.get('timezone', 'Europe/Kyiv'))
        except Exception:
            from datetime import timezone, timedelta
            tz = timezone(timedelta(hours=3))
        now_tz = datetime.datetime.now(tz)
        hour = now_tz.hour

        if prof.get('mode') == 'sun':
            try:
                from modules.suntimes import is_night
                lat = float(prof.get('lat', 50.45))
                lon = float(prof.get('lon', 30.52))
                import zoneinfo as _zi
                now_local = datetime.datetime.now(_zi.ZoneInfo(prof.get('timezone', 'Europe/Kyiv')))
                in_night = is_night(lat, lon, now_local,
                                    morning_offset_min=int(prof.get('morning_offset_min', 30)),
                                    evening_offset_min=int(prof.get('evening_offset_min', 30)))
            except Exception as e:
                logger.warning(f"sun mode failed ({e}) → fallback fixed hours")
                in_night = (hour >= int(prof.get('start_hour', 22)) or hour < int(prof.get('end_hour', 6)))
        else:
            start = int(prof.get('start_hour', 22))
            end = int(prof.get('end_hour', 6))
            in_night = (hour >= start or hour < end) if start != end else False

        if in_night:
            effective = dict(base)
            effective.update(prof.get('overrides', {}))
            return "night", effective
        return "day", base

    def analyze_frame(self, frame: np.ndarray, camera: CameraConfig) -> ImageAnalysisResult:
        """Analyze a single frame for issues."""
        profile_name, th = self._active_profile()
        self._effective[camera.id] = th

        if frame is None or frame.size == 0:
            return ImageAnalysisResult(ImageIssue.BLACK, "Empty frame", {}, profile=profile_name)

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
        normalized = gray.astype(np.float32) / 255.0
        metrics: dict[str, float] = {}

        # v2.4.3: вироджений кадр (std=0 — усі піксели однакові) = glitch передачи
        # (Dahua іноді віддає такі при старті потоку / I-frame переході). Це НЕ
        # "низький контраст" сцени і не blocked lens: справжня камера з заблокованим
        # об'єктивом рахується мажоритарним детектором на реальних кадрах.
        # → пропускаємо аналіз цього кадру цілком, чекаємо наступний.
        pre_std = float(np.std(normalized))
        if pre_std < 0.001:
            logger.debug(f"{camera.id}: degenerate frame (std={pre_std:.6f}) — skipped, no alert")
            return ImageAnalysisResult(ImageIssue.NONE, f"Degenerate frame skipped (std≈0, profile={profile_name})",
                                       {'std': pre_std}, profile=profile_name)

        # 1. Black frame — v2.4.4: подвійний критерій. Дуже темна сцена зі слабим
        # контентом (сутінки, IR не ввімкнена: mean 0.004, std 0.006) — жива камера.
        # Справді зламана/повністю чорна: mean < порогy і std майже нуль.
        mean_val = float(np.mean(normalized))
        std_preview = float(np.std(normalized))
        metrics['mean'] = mean_val
        metrics['std'] = std_preview
        if mean_val < th['black_threshold'] and std_preview < 0.004:
            return ImageAnalysisResult(
                ImageIssue.BLACK,
                f"Frame truly black (mean={mean_val:.4f}, std={std_preview:.4f}, profile={profile_name})",
                metrics, profile=profile_name)

        # 2. White frame
        if mean_val > th['white_threshold']:
            return ImageAnalysisResult(
                ImageIssue.WHITE,
                f"Frame nearly white (mean={mean_val:.4f}, profile={profile_name})",
                metrics, profile=profile_name)

        # 3. Contrast
        std_val = float(np.std(normalized))
        metrics['std'] = std_val
        if std_val < th['contrast_threshold']:
            return ImageAnalysisResult(
                ImageIssue.LOW_CONTRAST,
                f"Very low contrast (std={std_val:.4f}, profile={profile_name})",
                metrics, profile=profile_name)

        # 4. Blocked lens (4x4 grid block variance) — v2: мажоритарний критерій.
        # Закритий об'єктив = ВЕСЬ кадр рівний (≥80% блоків гладких). Один шматок
        # тротуару/стіни з нульовою текстурою — нормальна сцена, не алерт.
        h, w = gray.shape
        block_vars = []
        for y in range(0, h, h // 4):
            for x in range(0, w, w // 4):
                block = normalized[y:y + h // 4, x:x + w // 4]
                if block.size > 0:
                    block_vars.append(float(np.var(block)))

        if block_vars:
            flat_blocks = sum(1 for v in block_vars if v < th['blocked_threshold'])
            min_var = min(block_vars)
            metrics['min_block_var'] = min_var
            metrics['flat_blocks'] = float(flat_blocks)
            if flat_blocks >= len(block_vars) * 0.8:
                return ImageAnalysisResult(
                    ImageIssue.BLOCKED,
                    f"Blocked lens ({flat_blocks}/{len(block_vars)} blocks flat,"
                    f" min_block_var={min_var:.6f}, profile={profile_name})",
                    metrics, profile=profile_name)

        # 5. Blur (Laplacian variance)
        laplacian = cv2.Laplacian(gray, cv2.CV_64F)
        lap_var = float(laplacian.var())
        metrics['laplacian_var'] = lap_var
        if lap_var < th['blur_threshold']:
            return ImageAnalysisResult(
                ImageIssue.BLURRY,
                f"Blurry image (laplacian_var={lap_var:.2f}, profile={profile_name})",
                metrics, profile=profile_name)

        # 6. Frozen frame — v2.1: ніччю вимкнений (ніч на під'їзді з замкненими дверми
        # = статичність — норма, не поломка). Конфіг: frozen_frame.night_enabled: false
        if self.config.frozen_frame_enabled:
            night_now = profile_name == "night"
            night_frozen_allowed = getattr(self.config, 'frozen_frame_night_enabled', True)
            if not (night_now and not night_frozen_allowed):
                frozen_result = self._check_frozen_frame(frame, camera.id, th)
                if frozen_result:
                    frozen_result.profile = profile_name
                    if frozen_result.metrics:
                        metrics.update(frozen_result.metrics)
                    return frozen_result
            else:
                logger.debug(f"frozen_frame skipped ({profile_name} profile, night_enabled=false)")

        return ImageAnalysisResult(ImageIssue.NONE, f"Frame OK ({profile_name})", metrics, profile=profile_name)

    def _compute_phash(self, frame: np.ndarray, hash_size: int = 8) -> str:
        """Compute perceptual hash of frame."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
        resized = cv2.resize(gray, (hash_size, hash_size), interpolation=cv2.INTER_AREA)
        dct = cv2.dct(np.float32(resized))
        dct_low = dct[:hash_size, :hash_size]
        median = np.median(dct_low)
        diff = dct_low > median
        return ''.join(['1' if b else '0' for b in diff.flatten()])

    def _hamming_distance(self, hash1: str, hash2: str) -> int:
        """Compute Hamming distance between two hashes."""
        return sum(c1 != c2 for c1, c2 in zip(hash1, hash2))

    def _compute_ssim(self, frame1: np.ndarray, frame2: np.ndarray) -> float:
        """Compute Structural Similarity Index (simplified)."""
        gray1 = cv2.cvtColor(frame1, cv2.COLOR_BGR2GRAY) if len(frame1.shape) == 3 else frame1
        gray2 = cv2.cvtColor(frame2, cv2.COLOR_BGR2GRAY) if len(frame2.shape) == 3 else frame2

        if gray1.shape != gray2.shape:
            gray2 = cv2.resize(gray2, (gray1.shape[1], gray1.shape[0]))

        gray1 = gray1.astype(np.float32) / 255.0
        gray2 = gray2.astype(np.float32) / 255.0

        # v2.1: суцільні (constant) кадри — corrcoef дає nan → лікуємо:
        # два рівні кадри з однаковим значенням = 1.0 (ідентичні), з різним = 0.0
        std1, std2 = float(np.std(gray1)), float(np.std(gray2))
        if std1 < 1e-6 and std2 < 1e-6:
            return 1.0 if abs(float(np.mean(gray1)) - float(np.mean(gray2))) < 0.02 else 0.0

        correlation = np.corrcoef(gray1.flatten(), gray2.flatten())[0, 1]
        return float(correlation) if not np.isnan(correlation) else 0.0

    def _check_frozen_frame(self, frame: np.ndarray, camera_id: str, th: dict) -> ImageAnalysisResult | None:
        """Check if frame is frozen (same as previous frames)."""
        now = time.time()
        min_interval = self.config.frozen_frame_min_interval_minutes * 60

        phash = self._compute_phash(frame)

        if camera_id in self.last_phashes:
            last_phash, last_time = self.last_phashes[camera_id]

            if now - last_time >= min_interval:
                hamming = self._hamming_distance(phash, last_phash)
                max_bits = 64
                similarity = 1.0 - (hamming / max_bits)

                if similarity >= th['frozen_frame_similarity_threshold']:
                    if camera_id in self.last_frames:
                        last_frame, _ = self.last_frames[camera_id]
                        ssim = self._compute_ssim(frame, last_frame)
                        # v2.2: жива камера на статичній сцені має шум сенсора —
                        # кадри попіксельно НЕ ідентичні. Справді заморожений потік
                        # (кодек застряг) видає той самий кадр: diff = 0.
                        mean_abs_diff = float(np.mean(cv2.absdiff(
                            cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame,
                            cv2.cvtColor(last_frame, cv2.COLOR_BGR2GRAY) if len(last_frame.shape) == 3 else last_frame)))
                        sensor_noise = mean_abs_diff > 0.5   # живий шум → не frozen
                        if ssim >= th['frozen_frame_similarity_threshold'] and not sensor_noise:
                            return ImageAnalysisResult(
                                ImageIssue.FROZEN,
                                f"Frozen frame (phash={similarity:.4f}, SSIM={ssim:.4f},"
                                f" diff={mean_abs_diff:.3f})",
                                {'phash_similarity': similarity, 'ssim': ssim,
                                 'mean_abs_diff': mean_abs_diff,
                                 'hamming_distance': hamming})
                        elif sensor_noise and similarity >= 0.99:
                            logger.debug(f"{camera_id}: static scene with sensor noise"
                                         f" (diff={mean_abs_diff:.3f}) — жива, не frozen")

                self.last_phashes[camera_id] = (phash, now)
                self.last_frames[camera_id] = (frame.copy(), now)
        else:
            self.last_phashes[camera_id] = (phash, now)
            self.last_frames[camera_id] = (frame.copy(), now)

        return None


def save_event_frame(frame: np.ndarray, camera: CameraConfig, issue: ImageIssue, events_path: str) -> str | None:
    """Save event frame to disk with timestamped filename."""
    try:
        events_dir = Path(events_path)
        events_dir.mkdir(parents=True, exist_ok=True)

        timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"{timestamp}_{camera.id}_{issue.value}.jpg"
        filepath = events_dir / filename

        cv2.imwrite(str(filepath), frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
        logger.info(f"Saved event frame: {filepath}")
        return str(filepath)
    except Exception as e:
        logger.error(f"Failed to save event frame: {e}")
        return None


def cleanup_old_events(events_path: str, retention_days: int) -> int:
    """Delete event files older than retention_days."""
    try:
        events_dir = Path(events_path)
        if not events_dir.exists():
            return 0

        cutoff_time = time.time() - (retention_days * 86400)
        deleted = 0

        for filepath in events_dir.glob("*.jpg"):
            try:
                if filepath.stat().st_mtime < cutoff_time:
                    filepath.unlink()
                    deleted += 1
            except Exception:
                pass

        if deleted > 0:
            logger.info(f"Cleaned up {deleted} old event frames")

        return deleted
    except Exception as e:
        logger.error(f"Event cleanup error: {e}")
        return 0