"""Tests for the notifications module (config gating / graceful no-op)."""
from wcf import notify


def test_telegram_unconfigured_is_noop(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert notify.telegram_configured() is False
    assert notify.send_telegram("hello") is False
    # Telegram-only channel returns nothing when unconfigured.
    assert notify.notify("t", "b", channels="telegram") == []


def test_telegram_configured_true_when_both_set(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "y")
    assert notify.telegram_configured() is True
