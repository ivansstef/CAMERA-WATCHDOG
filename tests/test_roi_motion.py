"""
Тести v2.1: ROI motion detection (тригер на зміну в заданій зоні кадру).
"""
import sys
import time
from pathlib import Path

import numpy as np
import pytest

PROJ = Path(__file__).parent.parent
sys.path.insert(0, str(PROJ))

from modules.config_loader import CameraConfig, MotionConfig
from modules.roi_motion import (
    MotionRegion,
    ROIMotionDetector,
    draw_region_box,
    draw_region_grid,
    parse_region_args,
)


def _frame(gray_value: float, size: tuple = (320, 240)) -> np.ndarray:
    return np.full((*size[::-1], 3), gray_value, dtype=np.uint8) if False else np.full((size[1], size[0], 3), gray_value, dtype=np.uint8)


@pytest.fixture
def detector():
    cfg = MotionConfig(
        enabled=True, pixel_threshold=25, change_percent=2.0,
        cooldown_minutes=0.0,          # без cooldown для тестів тригера
        reference_refresh_minutes=0.05,
        min_region_area=100,
    )
    region = MotionRegion(name="shchitok", x1=40, y1=30, x2=70, y2=80)
    return ROIMotionDetector(cfg, [region]), cfg, region


class TestROIMotion:
    def test_first_frame_no_trigger(self, detector):
        """Перший кадр тільки ставить reference — тригера нема."""
        det, _cfg, _r = detector
        triggers = det.check_frame("cam70", _frame(100))
        assert triggers == []

    def test_no_change_no_trigger(self, detector):
        """Статична сцена (той же кадр) — не тригger."""
        det, _cfg, _r = detector
        det.check_frame("cam70", _frame(100))
        triggers = det.check_frame("cam70", _frame(100))
        assert triggers == []

    def test_zone_change_triggers(self, detector):
        """Зміна В зоні тригерить."""
        det, cfg, _r = detector
        base = _frame(100)
        det.check_frame("cam70", base)

        # поточний кадр: зона щитка стала світла (напри. відкрити двері щитка)
        changed = _frame(100)
        h, w = changed.shape[:2]
        x1, y1, x2, y2 = 40, 30, 70, 80
        px1, py1, px2, py2 = int(x1/100*w), int(y1/100*h), int(x2/100*w), int(y2/100*h)
        changed[py1:py2, px1:px2] = 220   # яскрава відмінність
        triggers = det.check_frame("cam70", changed)
        assert len(triggers) == 1
        t = triggers[0]
        assert t['region'] == 'shchitok'
        assert t['change_pct'] >= 2.0
        assert t['snapshot'] is not None

    def test_change_outside_zone_no_trigger(self, detector):
        """Зміна ПОЗА зоною (провулок) — не триггерить."""
        det, _cfg, _r = detector
        det.check_frame("cam70", _frame(100))
        changed = _frame(100)
        changed[0:40, :] = 220   # верхня частина — поза зоною (зона з 30% y)
        triggers = det.check_frame("cam70", changed)
        assert triggers == []

    def test_cooldown_blocks_repeat(self):
        """Другий спрацювання в межах cooldown — не відправляється."""
        cfg = MotionConfig(enabled=True, cooldown_minutes=5.0, change_percent=1.0,
                           reference_refresh_minutes=0.05, min_region_area=100)
        region = MotionRegion(name="z", x1=10, y1=10, x2=90, y2=90)
        det = ROIMotionDetector(cfg, [region])
        det.check_frame("c1", _frame(100))
        t1 = det.check_frame("c1", _frame(200))
        assert len(t1) == 1 and t1[0]['cooldown_ok'] is True
        # одразу again: reference оновився на попередньому тригgerі → нема нової зміни
        # симулюємо нову зміну
        det.states["c1:z"].reference = np.full((192, 256), 50, dtype=np.uint8)  # zone 10..90% of 320x240
        t2 = det.check_frame("c1", _frame(200))
        assert len(t2) == 1 and t2[0]['cooldown_ok'] is False

    def test_ema_drift_no_trigger(self, detector):
        """Плавна зміна освітлення (градієнт) — не триггер (EMA-адаптація)."""
        det, _cfg, _r = detector
        det.check_frame("cam70", _frame(100))
        # поступово змінюєм кожен кадр на +3 (менше pixel_threshold=25)
        for val in (103, 106, 109, 112, 115):
            triggers = det.check_frame("cam70", _frame(val))
        assert triggers == []

    def test_reference_refresh(self, detector):
        """Зміна reference_refresh_hours: reference періодно перезатирається."""
        det, cfg, _r = detector
        det.check_frame("cam70", _frame(100))
        # імітуємо старе reference
        det.states["cam70:shchitok"].reference_time = time.time() - (cfg.reference_refresh_minutes * 60 + 1)
        triggers = det.check_frame("cam70", _frame(200))
        # reference оновився без тригера
        assert triggers == []
        assert det.states["cam70:shchitok"].reference[0, 0] == 200

    def test_multiple_regions_independent(self):
        cfg = MotionConfig(enabled=True, cooldown_minutes=0, min_region_area=100)
        r1 = MotionRegion(name="door", x1=0, y1=0, x2=50, y2=100)
        r2 = MotionRegion(name="window", x1=50, y1=0, x2=100, y2=100)
        det = ROIMotionDetector(cfg, [r1, r2])
        det.check_frame("c1", _frame(100))
        frame = _frame(100)
        frame[:, : (320 // 2)] = 250   # лише ліва половина
        triggers = det.check_frame("c1", frame)
        names = [t['region'] for t in triggers if t['cooldown_ok']]
        assert names == ["door"]


class TestROIHelpers:
    def test_parse_region_args_valid(self):
        r = parse_region_args(["30", "20", "60", "90"], name="shchitok")
        assert r.x1 == 30 and r.y2 == 90 and r.name == "shchitok"

    def test_parse_region_args_inverted_rejected(self):
        assert parse_region_args(["60", "20", "30", "90"]) is None
        assert parse_region_args(["-5", "20", "30", "90"]) is None

    def test_draw_grid_returns_frame(self):
        f = _frame(128, size=(320, 240))
        vis = draw_region_grid(f, 10)
        assert vis.shape == f.shape
        assert not np.array_equal(vis, f)

    def test_draw_box_marks_region(self):
        f = _frame(128, size=(320, 240))
        r = MotionRegion(name="z", x1=10, y1=10, x2=50, y2=50)
        vis = draw_region_box(f, r)
        assert vis.shape == f.shape
        # в куті прямокутника — червоний (BGR 0,0,255): 10% від 320px=32, від 240=24
        assert vis[24, 32, 2] > 200

    def test_region_to_pixels(self):
        r = MotionRegion(name="z", x1=50, y1=50, x2=100, y2=100)
        px = r.to_pixels(640, 480)
        assert px == (320, 240, 640, 480)


class TestMotionConfigDefaults:
    def test_defaults(self):
        c = MotionConfig()
        assert c.enabled is False   # вимкнено за замовчуванням — не ламає v2 установок
        assert c.change_percent == 2.0
        assert c.pixel_threshold == 25

    def test_camera_regions_default_empty(self):
        cam = CameraConfig(id='x', name='x', ip='1.1.1.1')
        assert cam.motion_regions == []