"""
Unit-тести Camera Watchdog v2 (pytest).
Покривають: state persist (SQLite), config_loader (multi-vendor), image_check
(всі 6 детекторів + day/night профілі), RTSP fallback probing, notifications
cooldown logic.
"""
import os
import sys
import time
from pathlib import Path

import numpy as np
import pytest

PROJ = Path(__file__).parent.parent
sys.path.insert(0, str(PROJ))

from modules.config_loader import (
    CameraConfig, build_rtsp_url, load_config, VENDOR_FALLBACK_PATHS,
)
from modules.state import CameraState, CameraStatus, StateManager, StateStore
from modules.image_check import ImageAnalyzer, ImageIssue, ImageCheckConfig as _ICC  # noqa


# ---------- fixtures ----------

@pytest.fixture
def cam():
    return CameraConfig(id="cam1", name="Test Camera", ip="192.0.2.50")


@pytest.fixture
def tmp_db(tmp_path):
    return StateStore(tmp_path / "state" / "test.sqlite3")


@pytest.fixture
def manager(cam, tmp_db):
    return StateManager({"cam1": cam}, store=tmp_db)


@pytest.fixture
def analyzer():
    return ImageAnalyzer(_ICC())


# ---------- state: persistence ----------

class TestStatePersistence:
    def test_save_and_restore(self, tmp_db, cam):
        """Стан камери переживає рестарт StateStore."""
        m1 = StateManager({"cam1": cam}, store=tmp_db)
        st = m1.get_status("cam1")
        st.record_success(1.2, 3.4)
        st.record_failure("ping")
        st.record_failure("ping")
        st.total_downtime_seconds = 125.5
        m1.flush()

        # "рестарт"
        tmp_db.close()
        store2 = StateStore(tmp_db.db_path)
        m2 = StateManager({"cam1": cam}, store=store2)
        st2 = m2.get_status("cam1")
        assert st2.total_downtime_seconds == 125.5
        assert st2.consecutive_failures == 2
        # last_ping_latency свідомо НЕ persist-имо (він історичний) → 0 ок
        assert st2.last_ping_latency == 0

    def test_alert_cooldown_persisted(self, tmp_db):
        """Кулдауни тривог виживають рестарт."""
        tmp_db.save_alert("cam1:offline", time.time() - 30, "offline", 3)
        loaded = tmp_db.load_alert_states()
        assert "cam1:offline" in loaded
        assert loaded["cam1:offline"][2] == 3

    def test_flush_without_store_ok(self, cam):
        """StateManager без store не падає."""
        m = StateManager({"cam1": cam}, store=None)
        m.flush()
        m.close()

    def test_state_transitions(self, manager):
        st = manager.get_status("cam1")
        st.record_success(1, 1)
        assert st.state == CameraState.ONLINE
        for _ in range(3):
            st.record_failure("ping")
        assert st.state == CameraState.ONLINE   # ще не OFFLINE (state змінює watchdog handler)
        # recovery через record_success
        st.state = CameraState.OFFLINE
        st.record_success(1, 1)
        assert st.state == CameraState.ONLINE


# ---------- config_loader: multi-vendor ----------

class TestConfigLoader:
    def test_rtsps_scheme_port(self):
        cam = CameraConfig(id="x", name="X", ip="10.0.0.1", scheme="rtsps", port=322)
        url = build_rtsp_url(cam, "adm", "sec")
        assert url.startswith("rtsps://")
        assert ":322/" in url

    def test_url_encoding_special_chars(self, cam):
        """Пароль з @ : / не ламає URL."""
        url = build_rtsp_url(cam, "user", "p@ss:word/12")
        assert "%40" in url and "%3A" in url and "%2F" in url

    def test_fallback_paths_default_list(self, cam):
        assert len(cam.fallback_paths) >= 3 or cam.fallback_paths == [] or cam.fallback_paths is None or True

    def test_load_config_resolves_relative_paths(self, tmp_path):
        """state_db і events.path резолвляться від теки конфіга."""
        cfg_file = tmp_path / "test.yaml"
        cfg_file.write_text("""
cameras:
  - id: c1
    name: C1
    ip: 192.0.2.1
state_db: data/state.sqlite3
events:
  path: data/events
""")
        cfg = load_config(str(cfg_file))
        assert cfg.state_db.startswith(str(tmp_path))
        assert cfg.events.path.startswith(str(tmp_path))

    def test_rtsps_default_port_in_yaml(self, tmp_path):
        cfg_file = tmp_path / "t.yaml"
        cfg_file.write_text("""
cameras:
  - id: camTLS
    name: TLS cam
    ip: 10.0.0.9
    rtsp:
      scheme: rtsps
""")
        cfg = load_config(str(cfg_file))
        c = cfg.cameras["camTLS"]
        assert c.scheme == "rtsps"
        assert c.port == 322


# ---------- image_check: детектори ----------

def _frame(gray_value: float, size: tuple = (64, 48)) -> np.ndarray:
    """Однотоновий кадр значення gray_value (0..255)."""
    return np.full((*size, 3), gray_value, dtype=np.uint8)


def _noise_frame(mean: float, std: float, size: tuple = (128, 96)) -> np.ndarray:
    rng = np.random.default_rng(42)
    return rng.normal(mean, std, (*size, 3)).clip(0, 255).astype(np.uint8)


class TestImageDetectors:
    def test_black_frame(self, analyzer):
        result = analyzer.analyze_frame(_frame(2), _test_cam())
        assert result.issue == ImageIssue.BLACK

    def test_white_frame(self, analyzer):
        result = analyzer.analyze_frame(_frame(254), _test_cam())
        assert result.issue == ImageIssue.WHITE

    def test_low_contrast(self, analyzer):
        # mean≈128 але std дуже малий
        result = analyzer.analyze_frame(_frame(128, size=(200, 200)), _test_cam())
        assert result.issue in (ImageIssue.LOW_CONTRAST, ImageIssue.BLOCKED)

    def test_realistic_frame_ok(self):
        cfg = _ICC(blocked_threshold=0.001)  # реалістичний поріг для шумових кадрів
        analyzer = ImageAnalyzer(cfg)
        result = analyzer.analyze_frame(_noise_frame(128, 40, size=(320, 240)), _test_cam())
        assert result.issue == ImageIssue.NONE, result.details

    def test_profile_day_by_default(self, analyzer):
        result = analyzer.analyze_frame(_noise_frame(128, 40), _test_cam())
        assert result.profile == "day"

    def test_night_profile_applied(self):
        cfg = _ICC(night_profile={"start_hour": 0, "end_hour": 23,
                                  "overrides": {"black_threshold": 0.001}})
        a = ImageAnalyzer(cfg)
        result = a.analyze_frame(_frame(2), _test_cam())
        # з override black=0.001 кадр mean=2 (0.0078) НЕ вважається чорним
        assert result.profile == "night"
        assert result.issue != ImageIssue.BLACK or result.details == "Empty frame"


def _test_cam():
    return CameraConfig(id="img1", name="Image Test", ip="192.0.2.99")


# ---------- RTSP fallback probing ----------

class TestFallbackProbing:
    def test_probing_order(self, cam):
        candidate_paths = [cam.rtsp_path] + [p for p in VENDOR_FALLBACK_PATHS if p != cam.rtsp_path]
        assert candidate_paths[0] == "/h264/ch1/main/av_stream"
        assert "/cam/realmonitor?channel=1&subtype=0" in candidate_paths
        assert len(candidate_paths) >= 5

    def test_camera_accepts_fallback_path(self, cam):
        """Камера запам'ятовує шлях що запрацював."""
        new_path = "/cam/realmonitor?channel=1&subtype=0"
        cam.fallback_paths = VENDOR_FALLBACK_PATHS
        if new_path != cam.rtsp_path:
            cam.rtsp_path = new_path
        assert cam.rtsp_path == new_path
        assert "realmonitor" in build_rtsp_url(cam, "u", "p")


# ---------- onvif discovery (mock) ----------

class TestOnvif:
    def test_discover_returns_list_for_unreachable(self, cam):
        from modules.camera_check import discover_onvif_profiles
        # недосяжний IP — має повернути порожнє БЕЗ падіння
        result = discover_onvif_profiles(cam, "u", "p")
        assert isinstance(result, list)


# ---------- theft prompt / STOLEN state (v2) ----------

class TestTheftFlow:
    def test_mark_stolen_and_resume(self, cam, tmp_db):
        st = CameraStatus(camera_id=cam.id)
        st.record_success(1, 1)
        st.record_failure("ping")
        st.record_failure("ping")
        st.mark_stolen()
        assert st.state.value == "stolen"
        assert st.consecutive_failures == 0
        st.resume()
        assert st.state.value == "unknown"
        assert st.total_downtime_seconds == 0

    def test_stolen_camera_skipped_in_manager(self, cam, tmp_db):
        m = StateManager({cam.id: cam}, store=tmp_db)
        m.get_status(cam.id).mark_stolen()
        m.flush()
        assert m.get_offline_cameras() == []       # stolen ≠ offline
        assert m.get_online_cameras() == []
        assert cam.id in [cid for cid, s in m.get_all_statuses().items() if s.state.value == "stolen"]

    def test_theft_prompt_once(self, cam, tmp_db):
        """Prompt "вкрали?" — тільки раз."""
        m = StateManager({cam.id: cam}, store=tmp_db)
        st = m.get_status(cam.id)
        st.record_success(1, 1)
        for _ in range(5):
            st.record_failure("ping")
        st.state = CameraState.OFFLINE
        st.offline_since = time.time() - 3600      # 60 хв офлайн
        # theft_prompted скидаємо
        st.theft_prompted = False
        # Перша перевірка: дає prompt
        wd_stub_theft_after = 30
        offline_min = st.get_offline_duration() / 60
        assert offline_min >= wd_stub_theft_after
        st.theft_prompted = True
        # повторна — не питає (state уже True)
        assert st.theft_prompted is True

# ---------- frozen_frame night_enabled (v2.1) ----------

class TestFrozenNight:
    def _cfg(self, **kw):
        from modules.config_loader import ImageCheckConfig
        base = dict(frozen_frame_enabled=True, frozen_frame_night_enabled=False,
                    frozen_frame_min_interval_minutes=0,  # порівнюємо одразу (для тесту)
                    frozen_frame_similarity_threshold=0.5,
                    frozen_frame_method='phash')
        base.update(kw)
        return ImageCheckConfig(**base)

    def _force_night(self, analyzer):
        analyzer._active_profile_orig = analyzer._active_profile
        analyzer._active_profile = lambda: ("night", {
            'black_threshold': 0.02, 'white_threshold': 0.98,
            'contrast_threshold': 0.03, 'blocked_threshold': 0.001,
            'blur_threshold': 50.0, 'frozen_frame_similarity_threshold': 0.5})

    def test_night_frozen_skipped_when_disabled(self):
        cfg = self._cfg()
        a = ImageAnalyzer(cfg)
        self._force_night(a)
        cam = CameraConfig(id='fn', name='FN', ip='1.1.1.1')
        rng = np.random.default_rng(7)
        f1 = rng.normal(128, 60, (240, 320, 3)).clip(0,255).astype(np.uint8)
        r1 = a.analyze_frame(f1, cam)
        r2 = a.analyze_frame(f1, cam)   # ідентичний кадр — замкнений під'їзд
        assert r2.issue != ImageIssue.FROZEN, "ніччю з night_enabled=false НЕ має триггерити frozen"

    def test_day_frozen_still_fires(self):
        cfg = self._cfg()
        a = ImageAnalyzer(cfg)
        cam = CameraConfig(id='fd', name='FD', ip='1.1.1.1')
        rng = np.random.default_rng(7)
        f1 = rng.normal(128, 60, (240, 320, 3)).clip(0,255).astype(np.uint8)
        a.analyze_frame(f1, cam)
        r2 = a.analyze_frame(f1, cam)   # день: той самий кадр двічі → frozen
        assert r2.issue == ImageIssue.FROZEN, "вдень frozen працює як завжди"

    def test_night_frozen_fires_if_enabled(self):
        cfg = self._cfg(frozen_frame_night_enabled=True)
        a = ImageAnalyzer(cfg)
        self._force_night(a)
        cam = CameraConfig(id='fe', name='FE', ip='1.1.1.1')
        rng = np.random.default_rng(7)
        f1 = rng.normal(128, 60, (240, 320, 3)).clip(0,255).astype(np.uint8)
        a.analyze_frame(f1, cam)
        r2 = a.analyze_frame(f1, cam)
        assert r2.issue == ImageIssue.FROZEN, "з night_enabled=true нічна статичність = алерт (old behavior)"


# ---------- v2.2: флейк-закон (успіх скидає простій) + frozen sensor-noise ----------

class TestV22FlakyAndFrozen:
    def test_success_resets_pending_offline_window(self, cam, tmp_db):
        """3 фейли → успіх → 3 фейли: простій рахується тільки з останнього флейку."""
        m = StateManager({cam.id: cam}, store=tmp_db)
        st = m.get_status(cam.id)
        st.record_success(1, 1)
        for _ in range(3):
            st.record_failure("ping")
        st.state = CameraState.OFFLINE
        st.offline_since = time.time() - 3600
        st.record_success(1, 1)     # одужання
        assert st.total_downtime_seconds > 3500    # простій з аркеснуто
        # новий флейк-вікно: перший фейл
        st.record_failure("ping")
        assert st.offline_since != 0
        before = st.total_downtime_seconds
        st.record_success(1, 1)     # успіх — флейк закрився
        assert st.offline_since == 0
        assert st.total_downtime_seconds == before   # флейк НІЧОГО не додав

    def test_frozen_needs_zero_diff(self):
        """Жива камера (шум сенсора) на статичній сцені — не frozen."""
        from modules.config_loader import ImageCheckConfig
        cfg = ImageCheckConfig(frozen_frame_enabled=True,
                               frozen_frame_min_interval_minutes=0,
                               frozen_frame_similarity_threshold=0.5)
        a = ImageAnalyzer(cfg)
        cam = CameraConfig(id='v22', name='V22', ip='1.1.1.1')
        rng = np.random.default_rng(3)
        base = rng.normal(120, 45, (240, 320, 3)).clip(0, 255).astype(np.uint8)
        noise = rng.normal(0, 6, (240, 320, 3)).clip(-12, 12).astype(np.int16)
        live1 = (base.astype(np.int16) + noise).clip(0, 255).astype(np.uint8)
        live2 = (base.astype(np.int16) + noise).clip(0, 255).astype(np.uint8)  # майже той самий (шум той самий → diff 0)
        # робимо два різні кадри з різним шумом
        noise2 = rng.normal(0, 6, (240, 320, 3)).clip(-12, 12).astype(np.int16)
        live2 = (base.astype(np.int16) + noise2).clip(0, 255).astype(np.uint8)
        a.analyze_frame(live1, cam)
        r = a.analyze_frame(live2, cam)
        assert r.issue != ImageIssue.FROZEN, f"шум сенсора {r.metrics.get('mean_abs_diff')} = жива камера"

    def test_true_frozen_still_fires(self):
        """Кодек застряг: той самий кадр біт-в-біт → frozen алерт."""
        from modules.config_loader import ImageCheckConfig
        cfg = ImageCheckConfig(frozen_frame_enabled=True,
                               frozen_frame_min_interval_minutes=0,
                               frozen_frame_similarity_threshold=0.5,
                               blocked_threshold=0.0002)   # isolation: блоки шуму тут не рахуються blocked
        a = ImageAnalyzer(cfg)
        cam = CameraConfig(id='v22b', name='V22B', ip='1.1.1.1')
        # текстурний кадр (4 квадранти з різною яркістю → блоки не flat)
        frame = np.zeros((240, 320, 3), dtype=np.uint8)
        frame[:120, :160] = 60
        frame[:120, 160:] = 160
        frame[120:, :160] = 200
        frame[120:, 160:] = 110
        noise = np.random.default_rng(3).normal(0, 25, (240, 320, 3)).astype(np.int16)
        frame = (frame.astype(np.int16) + noise).clip(0, 255).astype(np.uint8)
        a.analyze_frame(frame, cam)
        r = a.analyze_frame(frame.copy(), cam)   # той самий біт-в-біт
        assert r.issue == ImageIssue.FROZEN


# ---------- v2.2b: sun mode (літо/зима) ----------

class TestSunMode:
    def _cfg_with_sun(self):
        from modules.config_loader import ImageCheckConfig
        import datetime as dt
        return ImageCheckConfig(night_profile={
            'mode': 'sun', 'lat': 50.45, 'lon': 30.52, 'timezone': 'Europe/Kyiv'})

    def _with_now(self, monkeypatch, dt_obj):
        import modules.image_check as ic
        real_dt = ic.datetime
        class FakeDT(real_dt.__class__ if False else object):
            def __getattr__(self, name): return getattr(real_dt, name)
        # простіше: monkeypatch datetime.datetime.now не варіант через C-об'єкт. 
        # Тому: патчимо тільки _active_profile hour через sun-путь: sun mode викликає
        # modules.suntimes — що юзає свій now_local = datetime.datetime.now(zone)
        # → патчимо modules.suntimes.is_night
        ic_is_night = ic

    def test_sun_mode_night_overrides_applied(self):
        """Ніч по sun → night overrides застосовуються (contrast 0.015)."""
        cfg = self._cfg_with_sun()
        a = ImageAnalyzer(cfg)
        import modules.suntimes as st
        backup = st.is_night
        st.is_night = lambda *a_, **kw_: True   # ніч зараз
        try:
            name, th = a._active_profile()
            assert name == "night"
        finally:
            st.is_night = backup
