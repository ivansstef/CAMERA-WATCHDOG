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
        """Темний кадр ЗІ ШУМОМ (справді чорна камера/об'єктив) → BLACK алерт"""
        a = ImageAnalyzer(ImageCheckConfig())
        cam = CameraConfig(id='dg3', name='DG3', ip='1.1.1.1')
        rng = np.random.default_rng(5)
        frame = rng.normal(2, 3, (240, 320, 3)).clip(0, 255).astype(np.uint8)
        r = a.analyze_frame(frame, cam)
        assert r.issue == ImageIssue.BLACK