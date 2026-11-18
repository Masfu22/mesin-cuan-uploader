import os
import pytest

from main import _configured_channels, _get_upload_privacy, _load_creds_from_env


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


def test_configured_channels_only_returns_channel_ids(monkeypatch):
    monkeypatch.setenv("TOKEN_CH_ID_HORROR", "token-json-secret")
    monkeypatch.delenv("TOKEN_CH_ID_PSYCH", raising=False)
    monkeypatch.delenv("TOKEN_CH_EN_HORROR", raising=False)
    monkeypatch.delenv("TOKEN_CH_EN_PSYCH", raising=False)
    assert _configured_channels() == ["ch_id_horror"]
    assert "token-json-secret" not in str(_configured_channels())


def test_configured_channels_empty_when_no_tokens(monkeypatch):
    for key in (
        "TOKEN_CH_ID_HORROR", "TOKEN_CH_ID_PSYCH",
        "TOKEN_CH_EN_HORROR", "TOKEN_CH_EN_PSYCH",
    ):
        monkeypatch.delenv(key, raising=False)
    assert _configured_channels() == []
