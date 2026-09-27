from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured

from config.settings import (
    Settings,
    _check_manager_notification_settings,
    _resolve_captcha_secret_key,
    _resolve_pd_consent_version,
)

CAPTCHA_ALWAYS_PASS = "1x0000000000000000000000000000000AA"
NOTIFICATIONS = {
    "TELEGRAM_BOT_TOKEN": "123:abc",
    "TELEGRAM_MANAGER_CHAT_ID": "-100500",
    "ADMIN_BASE_URL": "https://api.example.ru",
}


def _env(**overrides: object) -> Settings:
    # _env_file=None — не подмешивать локальный .env разработчика
    return Settings(SECRET_KEY="test", _env_file=None, **overrides)  # type: ignore[call-arg]


class TestPdConsentVersionSetting:
    def test_local_uses_draft_marker(self) -> None:
        assert _resolve_pd_consent_version(_env(ENVIRONMENT="local")) == "local-draft"

    @pytest.mark.parametrize("environment", ["staging", "production"])
    def test_required_outside_local(self, environment: str) -> None:
        # ПОЧЕМУ: версия — часть доказательства согласия; выдуманный дефолт
        # в проде записал бы «согласие на неизвестный текст»
        with pytest.raises(ImproperlyConfigured):
            _resolve_pd_consent_version(_env(ENVIRONMENT=environment))

    def test_explicit_version_wins(self) -> None:
        env = _env(ENVIRONMENT="production", PD_CONSENT_VERSION="2026-10-01")

        assert _resolve_pd_consent_version(env) == "2026-10-01"


class TestCaptchaSecretKeySetting:
    def test_local_falls_back_to_test_key(self) -> None:
        assert (
            _resolve_captcha_secret_key(_env(ENVIRONMENT="local"))
            == CAPTCHA_ALWAYS_PASS
        )

    @pytest.mark.parametrize("environment", ["staging", "production"])
    def test_required_outside_local(self, environment: str) -> None:
        # ПОЧЕМУ: раньше дефолтом был тестовый ключ «всегда успех» —
        # забытая переменная на проде молча открывала формы ботам
        with pytest.raises(ImproperlyConfigured):
            _resolve_captcha_secret_key(_env(ENVIRONMENT=environment))

    @pytest.mark.parametrize(
        "test_secret",
        [
            CAPTCHA_ALWAYS_PASS,
            "2x0000000000000000000000000000000AA",
            "3x0000000000000000000000000000000AA",
        ],
    )
    def test_production_rejects_cloudflare_test_keys(self, test_secret: str) -> None:
        env = _env(ENVIRONMENT="production", CAPTCHA_SECRET_KEY=test_secret)

        with pytest.raises(ImproperlyConfigured):
            _resolve_captcha_secret_key(env)

    def test_staging_accepts_explicit_test_key(self) -> None:
        env = _env(ENVIRONMENT="staging", CAPTCHA_SECRET_KEY=CAPTCHA_ALWAYS_PASS)

        assert _resolve_captcha_secret_key(env) == CAPTCHA_ALWAYS_PASS

    def test_explicit_secret_wins(self) -> None:
        env = _env(ENVIRONMENT="production", CAPTCHA_SECRET_KEY="0x4AAA-real")

        assert _resolve_captcha_secret_key(env) == "0x4AAA-real"


class TestManagerNotificationSettings:
    @pytest.mark.parametrize("missing", list(NOTIFICATIONS))
    def test_production_requires_each_setting(self, missing: str) -> None:
        # ПОЧЕМУ: без бота заявки копятся в админке, и о них никто не знает
        env = _env(ENVIRONMENT="production", **{**NOTIFICATIONS, missing: ""})

        with pytest.raises(ImproperlyConfigured, match=missing):
            _check_manager_notification_settings(env)

    def test_production_with_all_settings_starts(self) -> None:
        _check_manager_notification_settings(
            _env(ENVIRONMENT="production", **NOTIFICATIONS)
        )

    @pytest.mark.parametrize("environment", ["local", "staging"])
    def test_optional_outside_production(self, environment: str) -> None:
        _check_manager_notification_settings(_env(ENVIRONMENT=environment))

    def test_defaults_are_empty_not_placeholders(self) -> None:
        # ПОЧЕМУ: заглушка вида "dummy-bot-token" — непустая строка, и проверка
        # «Telegram не настроен» её не замечала: задача слала запросы
        # с фальшивым токеном и уходила в ретраи
        env = _env()

        assert env.TELEGRAM_BOT_TOKEN == ""
        assert env.TELEGRAM_MANAGER_CHAT_ID == ""
