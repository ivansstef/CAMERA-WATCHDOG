"""
Camera Watchdog - Image Analysis
Detects black/white frames, low contrast, blocked lens, blur, frozen frames.
"""
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
    metrics: dict[str, float] = None
    frame: np.ndarray | None = None

    def __post_init__(self):
        if self.metrics is None:
            self.metrics = {}


class ImageAnalyzer:
    def __init__(self, config: ImageCheckConfig):
        self.config = config
        self.last_frames: dict[str, tuple[np.ndarray, float]] = {}  # camera_id -> (frame_hash, timestamp)
        self.last_phashes: dict[str, tuple[str, float]] = {}  # camera_id -> (phash, timestamp)

    def analyze_frame(self, frame: np.ndarray, camera: CameraConfig) -> ImageAnalysisResult:
        """Analyze a single frame for issues."""
        if frame is None or frame.size == 0:
            return ImageAnalysisResult(ImageIssue.BLACK, "Empty frame", {})

        # Convert to grayscale for analysis
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame

        # Normalize to 0-1 range
        normalized = gray.astype(np.float32) / 255.0

        metrics = {}

        # 1. Check for black frame
        mean_val = float(np.mean(normalized))
        metrics['mean'] = mean_val
        if mean_val < self.config.black_threshold:
            return ImageAnalysisResult(
                ImageIssue.BLACK,
                f"Frame is nearly black (mean={mean_val:.4f})",
                metrics
            )

        # 2. Check for white frame
        if mean_val > self.config.white_threshold:
            return ImageAnalysisResult(
                ImageIssue.WHITE,
                f"Frame is nearly white (mean={mean_val:.4f})",
                metrics
            )

        # 3. Check contrast (standard deviation)
        std_val = float(np.std(normalized))
        metrics['std'] = std_val
        if std_val < self.config.contrast_threshold:
            return ImageAnalysisResult(
                ImageIssue.LOW_CONTRAST,
                f"Very low contrast (std={std_val:.4f})",
                metrics
            )

        # 4. Check for blocked lens (very low variance in local regions)
        # Divide into 4x4 grid and check variance
        h, w = gray.shape
        block_vars = []
        for y in range(0, h, h // 4):
            for x in range(0, w, w // 4):
                block = normalized[y:y + h // 4, x:x + w // 4]
                if block.size > 0:
                    block_vars.append(float(np.var(block)))

        if block_vars:
            min_var = min(block_vars)
            metrics['min_block_var'] = min_var
            if min_var < self.config.blocked_threshold:
                return ImageAnalysisResult(
                    ImageIssue.BLOCKED,
                    f"Possible blocked lens (min block variance={min_var:.6f})",
                    metrics
                )

        # 5. Check for blur using Laplacian variance
        laplacian = cv2.Laplacian(gray, cv2.CV_64F)
        lap_var = float(laplacian.var())
        metrics['laplacian_var'] = lap_var
        if lap_var < self.config.blur_threshold:
            return ImageAnalysisResult(
                ImageIssue.BLURRY,
                f"Blurry image (laplacian_var={lap_var:.2f})",
                metrics
            )

        # 6. Check for frozen frame
        if self.config.frozen_frame_enabled:
            frozen_result = self._check_frozen_frame(frame, camera.id)
            if frozen_result:
                metrics.update(frozen_result.metrics)
                return frozen_result

        return ImageAnalysisResult(ImageIssue.NONE, "Frame OK", metrics)

    def _compute_phash(self, frame: np.ndarray, hash_size: int = 8) -> str:
        """Compute perceptual hash of frame."""
        # Resize to hash_size x hash_size
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
        resized = cv2.resize(gray, (hash_size, hash_size), interpolation=cv2.INTER_AREA)

        # Compute DCT
        dct = cv2.dct(np.float32(resized))
        dct_low = dct[:hash_size, :hash_size]

        # Median
        median = np.median(dct_low)

        # Create hash
        diff = dct_low > median
        phash = ''.join(['1' if b else '0' for b in diff.flatten()])
        return phash

    def _hamming_distance(self, hash1: str, hash2: str) -> int:
        """Compute Hamming distance between two hashes."""
        return sum(c1 != c2 for c1, c2 in zip(hash1, hash2))

    def _compute_ssim(self, frame1: np.ndarray, frame2: np.ndarray) -> float:
        """Compute Structural Similarity Index (simplified)."""
        gray1 = cv2.cvtColor(frame1, cv2.COLOR_BGR2GRAY) if len(frame1.shape) == 3 else frame1
        gray2 = cv2.cvtColor(frame2, cv2.COLOR_BGR2GRAY) if len(frame2.shape) == 3 else frame2

        # Resize to same size if needed
        if gray1.shape != gray2.shape:
            gray2 = cv2.resize(gray2, (gray1.shape[1], gray1.shape[0]))

        # Simple correlation-based similarity
        gray1 = gray1.astype(np.float32) / 255.0
        gray2 = gray2.astype(np.float32) / 255.0

        correlation = np.corrcoef(gray1.flatten(), gray2.flatten())[0, 1]
        return float(correlation) if not np.isnan(correlation) else 0.0

    def _check_frozen_frame(self, frame: np.ndarray, camera_id: str) -> ImageAnalysisResult | None:
        """Check if frame is frozen (same as previous frames)."""
        now = time.time()
        min_interval = self.config.frozen_frame_min_interval_minutes * 60

        # Compute perceptual hash
        phash = self._compute_phash(frame)

        if camera_id in self.last_phashes:
            last_phash, last_time = self.last_phashes[camera_id]

            # Check if enough time has passed
            if now - last_time >= min_interval:
                # Compute Hamming distance
                hamming = self._hamming_distance(phash, last_phash)
                max_bits = 64  # 8x8 = 64 bits
                similarity = 1.0 - (hamming / max_bits)

                if similarity >= self.config.frozen_frame_similarity_threshold:
                    # Also verify with SSIM on actual frames
                    if camera_id in self.last_frames:
                        last_frame, _ = self.last_frames[camera_id]
                        ssim = self._compute_ssim(frame, last_frame)
                        if ssim >= self.config.frozen_frame_similarity_threshold:
                            return ImageAnalysisResult(
                                ImageIssue.FROZEN,
                                f"Frozen frame detected (phash similarity={similarity:.4f}, SSIM={ssim:.4f})",
                                {'phash_similarity': similarity, 'ssim': ssim, 'hamming_distance': hamming}
                            )

                # Update stored hash and frame
                self.last_phashes[camera_id] = (phash, now)
                self.last_frames[camera_id] = (frame.copy(), now)
        else:
            # First frame for this camera
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