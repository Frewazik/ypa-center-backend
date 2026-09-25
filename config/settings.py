from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import sys
from typing import Literal

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    SECRET_KEY: str
    DEBUG: bool = True
    ALLOWED_HOSTS: list[str] = Field(
        default=["localhost", "127.0.0.1", "127.0.0.1:8000"]
    )
    ENVIRONMENT: Literal["local", "staging", "production"] = "local"
    ENABLE_THROTTLING: bool | None = None
    # Сколько своих прокси стоит перед Django (Caddy на VPS — 1).
    # Вне local обязательна: см. _resolve_trusted_proxy_count
    TRUSTED_PROXY_COUNT: int | None = Field(default=None, ge=0)

    DATABASE_URL: str = Field(
        default="postgresql://postgres:postgres@localhost:5432/yra"
    )
    REDIS_URL: str = Field(default="redis://localhost:6379/0")

    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_STORAGE_BUCKET_NAME: str = ""
    AWS_S3_ENDPOINT_URL: str = ""
    AWS_S3_REGION_NAME: str = "us-east-1"

    CORS_ALLOWED_ORIGINS: list[str] = Field(default=["http://localhost:3000"])

    EMAIL_HOST: str = "localhost"
    EMAIL_PORT: int = 25
    EMAIL_HOST_USER: str = ""
    EMAIL_HOST_PASSWORD: str = ""
    EMAIL_USE_TLS: bool = False

    CAPTCHA_VERIFY_URL: str = (
        "https://challenges.cloudflare.com/turnstile/v0/siteverify"
    )
    CAPTCHA_SECRET_KEY: str = "1x0000000000000000000000000000000AA"


# ПОЧЕМУ ignore: обязательные поля заполняет pydantic-settings из env/.env,
# mypy без pydantic-плагина видит их как незаполненные аргументы конструктора
_env = Settings()  # type: ignore[call-arg]


def _resolve_trusted_proxy_count(env: Settings) -> int:
    # ПОЧЕМУ: ошибка в любую сторону тихая и опасная. Поставили 1 без
    # прокси — клиент подделывает IP через X-Forwarded-For и обходит лимиты;
    # поставили 0 за прокси — все клиенты получают IP прокси, делят один
    # лимит, а вебхуки ЮКассы отклоняются. Поэтому вне local значение
    # задаётся явно, а забытая переменная роняет запуск
    if env.TRUSTED_PROXY_COUNT is not None:
        return env.TRUSTED_PROXY_COUNT
    if env.ENVIRONMENT == "local":
        return 0
    raise ImproperlyConfigured(
        "Задайте TRUSTED_PROXY_COUNT — число своих прокси перед приложением "
        "(Caddy на VPS — 1). См. docs/deploy.md"
    )


TRUSTED_PROXY_COUNT = _resolve_trusted_proxy_count(_env)

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = _env.SECRET_KEY
DEBUG = _env.DEBUG
ALLOWED_HOSTS = _env.ALLOWED_HOSTS

INSTALLED_APPS = [
    "unfold",
    "unfold.contrib.filters",
    "unfold.contrib.forms",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "phonenumber_field",
    "simple_history",
    "storages",
    "drf_spectacular",
    "apps.core",
    "apps.users",
    "apps.catalog.apps.CatalogConfig",
    "apps.schedule.apps.ScheduleConfig",
    "apps.billing",
    "apps.public_forms",
    "apps.events.apps.EventsConfig",
    "apps.content.apps.ContentConfig",
    "apps.public_api.apps.PublicApiConfig",
    "apps.me.apps.MeConfig",
    "apps.journal.apps.JournalConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "apps.core.middleware.RequestIDMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "simple_history.middleware.HistoryRequestMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": dj_database_url.parse(
        _env.DATABASE_URL,
        conn_max_age=600,  # ПОЧЕМУ: conn_max_age держит коннекты; при масштабировании подов упремся в лимит БД
        conn_health_checks=True,
        ssl_require=_env.ENVIRONMENT != "local",
    )
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": str(_env.REDIS_URL),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

_is_testing = "pytest" in sys.modules or any("pytest" in arg for arg in sys.argv)
_enable_throttling = (
    _env.ENABLE_THROTTLING
    if _env.ENABLE_THROTTLING is not None
    else (_env.ENVIRONMENT in ("staging", "production") or _is_testing)
)

if _enable_throttling:
    _throttle_classes = [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ]
    _throttle_rates: dict[str, str | None] = {
        "anon": "1000/hour",
        "user": "5000/hour",
        "public_forms_callback": "3/min",
        "public_forms_feedback": "3/min",
        "events_registration": "3/min",
        # ПОЧЕМУ: за одним IP сидят абоненты мобильного оператора (CGNAT)
        # и родители на Wi-Fi ресепшена. Ящик жертвы бережёт лимит по email
        "otp_request_ip": "30/hour",
        "otp_request_email": "5/hour",
        "otp_verify_ip": "10/min",
        "auth_token_refresh": "60/min",
        "auth_logout": "60/min",
    }
else:
    _throttle_classes = []
    _throttle_rates = {
        "anon": None,
        "user": None,
        "public_forms_callback": None,
        "public_forms_feedback": None,
        "events_registration": None,
        "otp_request_ip": None,
        "otp_request_email": None,
        "otp_verify_ip": None,
        "auth_token_refresh": None,
        "auth_logout": None,
    }

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    # ПОЧЕМУ: анкета закрывает всё по умолчанию — забытая новая ручка ЛК
    # не утечёт молча. Профиль и дети открыты явно в apps/me/views.py
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
        "apps.users.permissions.IsProfileCompleted",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_THROTTLE_CLASSES": _throttle_classes,
    "DEFAULT_THROTTLE_RATES": _throttle_rates,
    "EXCEPTION_HANDLER": "apps.core.exceptions.problem_detail_exception_handler",
    # ПОЧЕМУ: единственная ручка доверия к X-Forwarded-For — её читают и
    # встроенные троттлы DRF, и apps.core.net.client_ip
    "NUM_PROXIES": TRUSTED_PROXY_COUNT,
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=30),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Улица Радости API",
    "DESCRIPTION": "Детский центр развития - спецификация контрактов ядра.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
}

if _env.AWS_STORAGE_BUCKET_NAME:
    STORAGES = {
        "default": {"BACKEND": "storages.backends.s3boto3.S3Boto3Storage"},
        "staticfiles": {"BACKEND": "storages.backends.s3boto3.S3StaticStorage"},
    }
    AWS_ACCESS_KEY_ID = _env.AWS_ACCESS_KEY_ID
    AWS_SECRET_ACCESS_KEY = _env.AWS_SECRET_ACCESS_KEY
    AWS_STORAGE_BUCKET_NAME = _env.AWS_STORAGE_BUCKET_NAME
    AWS_S3_ENDPOINT_URL = _env.AWS_S3_ENDPOINT_URL
    AWS_S3_REGION_NAME = _env.AWS_S3_REGION_NAME
    AWS_S3_FILE_OVERWRITE = False
    AWS_DEFAULT_ACL = None
else:
    MEDIA_ROOT = BASE_DIR / "media"
    MEDIA_URL = "/media/"
    STATIC_ROOT = BASE_DIR / "staticfiles"

STATIC_URL = "/static/"

CORS_ALLOWED_ORIGINS = _env.CORS_ALLOWED_ORIGINS
CORS_ALLOW_CREDENTIALS = True

LANGUAGE_CODE = "ru-ru"
TIME_ZONE = "Asia/Novosibirsk"
USE_I18N = True
USE_TZ = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
PHONENUMBER_DEFAULT_REGION = "RU"

EMAIL_BACKEND = (
    "django.core.mail.backends.console.EmailBackend"
    if _env.ENVIRONMENT == "local"
    else "django.core.mail.backends.smtp.EmailBackend"
)
EMAIL_HOST = _env.EMAIL_HOST
EMAIL_PORT = _env.EMAIL_PORT
EMAIL_HOST_USER = _env.EMAIL_HOST_USER
EMAIL_HOST_PASSWORD = _env.EMAIL_HOST_PASSWORD
EMAIL_USE_TLS = _env.EMAIL_USE_TLS
AUTH_USER_MODEL = "users.Parent"

CAPTCHA_VERIFY_URL = _env.CAPTCHA_VERIFY_URL
CAPTCHA_SECRET_KEY = _env.CAPTCHA_SECRET_KEY
# ПОЧЕМУ: public_forms читает этот таймаут для httpx; без него проверка
# капчи падала бы в AttributeError на первом же запросе
EXTERNAL_HTTP_TIMEOUT_SECONDS = 5.0

TELEGRAM_BOT_TOKEN = "dummy-bot-token"
TELEGRAM_MANAGER_CHAT_ID = "dummy-chat-id"

UNFOLD = {
    "SITE_TITLE": "Улица Радости - админка",
    "SITE_HEADER": "Улица Радости",
    "DASHBOARD_CALLBACK": "apps.core.dashboard.dashboard_callback",
    "COLORS": {
        "primary": {
            "50": "240 253 244",
            "100": "220 252 231",
            "200": "187 247 208",
            "300": "134 239 172",
            "400": "74 222 128",
            "500": "34 197 94",
            "600": "22 163 74",
            "700": "21 128 61",
            "800": "22 101 52",
            "900": "20 83 45",
            "950": "5 46 22",
        },
    },
}
