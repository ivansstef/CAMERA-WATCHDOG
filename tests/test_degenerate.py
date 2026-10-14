"""
Тести v2.4.3: degenerate frame skip (суцільний кадр = glitch, не алерт).
"""
import sys
import numpy as np
import pytest
from pathlib import Path

PROJ = Path(__file__).parent.parent
sys.path.insert(0, str(PROJ))

from modules.config_loader import CameraConfig, ImageCheckConfig
from modules.image_check import ImageAnalyzer, ImageIssue


class TestDegenerateFrame:
    def test_flat_gray_frame_skipped(self):
        """Суцільно-сірий кадр (glitch передачі) НЕ алертить low_contrast."""
        a = ImageAnalyzer(ImageCheckConfig())
        cam = CameraConfig(id='dg', name='DG', ip='1.1.1.1')
        r = a.analyze_frame(np.full((240, 320, 3), 128, dtype=np.uint8), cam)
        assert r.issue == ImageIssue.NONE

    def test_flat_black_frame_skipped_too(self):
        """Суцільно-чорний рівний кадр — теж glitch-скіп (сцена чорна ніч IR
        ловиться нічними порогами, а вироджена передача не алертить)."""
        a = ImageAnalyzer(ImageCheckConfig())
        cam = CameraConfig(id='dg2', name='DG2', ip='1.1.1.1')
        r = a.analyze_frame(np.full((240, 320, 3), 0, dtype=np.uint8), cam)
        assert r.issue == ImageIssue.NONE

    def test_noisy_dark_frame_still_black(self):
        """Справді чорна камера: mean≈0.002, std≈0.002 (дуже слабий шум) → BLACK"""
        a = ImageAnalyzer(ImageCheckConfig())
        cam = CameraConfig(id='dg3', name='DG3', ip='1.1.1.1')
        rng = np.random.default_rng(5)
        frame = rng.normal(0.5, 0.5, (240, 320, 3)).clip(0, 255).astype(np.uint8)
        r = a.analyze_frame(frame, cam)
        assert r.issue == ImageIssue.BLACK

class TestBlackDoubleCriterion:
    def _analyzer(self):
        return ImageAnalyzer(ImageCheckConfig())

    def test_twilight_stairs_not_black(self):
        """Сутінки (mean 0.004) але сходи видно (std 0.006) — не BLACK."""
        a = self._analyzer()
        cam = CameraConfig(id='tw', name='TW', ip='1.1.1.1')
        rng = np.random.default_rng(9)
        frame = np.zeros((240, 320, 3), dtype=np.uint8)
        # слабі сходинки: горизонтальні смуги 4-7 значення на чорному
        for y in range(20, 200, 20):
            frame[y:y+10, 40:280] = 6
        frame = (frame.astype(np.int16) + rng.normal(0, 3, frame.shape).astype(np.int16)).clip(0, 255).astype(np.uint8)
        r = a.analyze_frame(frame, cam)
        assert r.issue in (ImageIssue.NONE, ImageIssue.LOW_CONTRAST), r.details

    def test_true_black_pixel_noise_detected(self):
        """Повністю чорний + шум сенсора — все ще BLACK (в середньому mean < 0.02, std < 0.004)"""
        a = self._analyzer()
        cam = CameraConfig(id='tb', name='TB', ip='1.1.1.1')
        rng = np.random.default_rng(4)
        frame = rng.normal(0.5, 0.7, (240, 320, 3)).clip(0, 255).astype(np.uint8)
        r = a.analyze_frame(frame, cam)
        assert r.issue == ImageIssue.BLACK
