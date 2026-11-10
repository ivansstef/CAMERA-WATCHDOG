"""Регресійний тест бага /mute: notifier мав читати muted_until (2.4.4 НЕ читав)."""
import time

from modules.config_loader import TelegramConfig
from modules.notifications import TelegramNotifier


def _notifier():
    cfg = TelegramConfig(bot_token="", chat_id="0")
    n = TelegramNotifier(cfg, alert_store=None)
    n.enabled = True
    n.config.cooldown_minutes = 0
    return n


def test_mute_silences_all_alerts():
    n = _notifier()
    n.mute_provider = lambda: time.time() + 3600
    for path in ("_can_send_alert",):
        assert not getattr(n, path)("cam1", "offline"), "mute має глушити алерти"


def test_mute_expired_allows_alerts():
    n = _notifier()
    n.mute_provider = lambda: time.time() - 1
    assert n._can_send_alert("cam1", "offline")


def test_no_mute_provider_by_default():
    n = _notifier()
    assert n.mute_provider is None
    assert n._can_send_alert("cam1", "offline")
