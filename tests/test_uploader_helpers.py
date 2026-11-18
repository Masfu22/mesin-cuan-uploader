import os
import pytest

from main import _get_upload_privacy, _load_creds_from_env


def test_privacy_defaults_to_private(monkeypatch):
    monkeypatch.delenv("YOUTUBE_PRIVACY_STATUS", raising=False)
    assert _get_upload_privacy({}) == "private"


@pytest.mark.parametrize("value", ["private", "unlisted", "public"])
def test_privacy_accepts_supported_values(monkeypatch, value):
    monkeypatch.setenv("YOUTUBE_PRIVACY_STATUS", value)
    assert _get_upload_privacy({}) == value


def test_privacy_rejects_unsupported_value(monkeypatch):
    monkeypatch.setenv("YOUTUBE_PRIVACY_STATUS", "friends")
    with pytest.raises(ValueError):
        _get_upload_privacy({})


def test_missing_channel_token_is_clear_error(monkeypatch):
    monkeypatch.delenv("TEST_CHANNEL_TOKEN", raising=False)
    with pytest.raises(EnvironmentError, match="TEST_CHANNEL_TOKEN"):
        _load_creds_from_env("TEST_CHANNEL_TOKEN")
