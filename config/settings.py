from __future__ import annotations

from pathlib import Path
from typing import Literal

import dj_database_url
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


_env = Settings()

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

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    # TODO: лимиты задушат SPA; переписать на ScopedThrottles после тестов
    "DEFAULT_THROTTLE_RATES": {
        "anon": "100/hour",
        "user": "1000/hour",
    },
    "EXCEPTION_HANDLER": "apps.core.exceptions.problem_detail_exception_handler",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Улица Радости API",
    "DESCRIPTION": "Детский центр развития — спецификация контрактов ядра.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
}

TASKIQ_BROKER_URL = str(_env.REDIS_URL)

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

CAPTCHA_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
CAPTCHA_SECRET_KEY = "dummy-secret-key"

TELEGRAM_BOT_TOKEN = "dummy-bot-token"
TELEGRAM_MANAGER_CHAT_ID = "dummy-chat-id"

UNFOLD = {
    "SITE_TITLE": "Улица Радости — админка",
    "SITE_HEADER": "Улица Радости",
    "DASHBOARD_CALLBACK": "apps.billing.dashboard.dashboard_callback",
}