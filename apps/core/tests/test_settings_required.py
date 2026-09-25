from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured

from config.settings import Settings, _resolve_pd_consent_version


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
