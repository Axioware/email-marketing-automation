"""Django settings. Every secret and deployment value comes from the environment or the project `.env` file."""
import os
import sys
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

TESTING = len(sys.argv) > 1 and sys.argv[1] == "test"


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


def database_from_url(url: str) -> dict:
    """Django settings for a postgres URL. SQLAlchemy-style schemes (postgresql+psycopg2://) are accepted too."""
    parts = urlsplit(url)
    if parts.scheme.split("+", 1)[0] not in {"postgres", "postgresql"}:
        raise ImproperlyConfigured("DATABASE_URL must be a postgresql:// URL")
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parts.path.lstrip("/")),
        "USER": unquote(parts.username or ""),
        "PASSWORD": unquote(parts.password or ""),
        "HOST": parts.hostname or "",
        "PORT": str(parts.port or ""),
        "OPTIONS": dict(parse_qsl(parts.query)),
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
        # Supabase's transaction pooler (port 6543) cannot keep server-side cursors open between transactions.
        "DISABLE_SERVER_SIDE_CURSORS": parts.port == 6543 or env_bool("DATABASE_POOLER"),
    }


DEBUG = env_bool("DJANGO_DEBUG")
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "").strip()
if not SECRET_KEY:
    if not (DEBUG or TESTING):
        raise ImproperlyConfigured(
            "Set DJANGO_SECRET_KEY in .env (generate one with: "
            "python -c \"import secrets; print(secrets.token_urlsafe(50))\")"
        )
    SECRET_KEY = "insecure-development-only-key"

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "pipeline",  # first, so its admin templates take precedence
    "pipeline.admin_config.PipelineAdminConfig",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework.authtoken",
    "django_filters",
    "drf_spectacular",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "emailautomation.urls"
WSGI_APPLICATION = "emailautomation.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if TESTING:
    # Never the real database: the test runner points this at TEST_DATABASE_URL or a throwaway Docker Postgres.
    DATABASES = {"default": database_from_url("postgresql://postgres:test@127.0.0.1:5432/postgres")}
elif DATABASE_URL:
    DATABASES = {"default": database_from_url(DATABASE_URL)}
else:
    raise ImproperlyConfigured("DATABASE_URL is not set in the environment or project .env file")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
TEST_RUNNER = "pipeline.test_runner.PostgresDockerRunner"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("DJANGO_TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = os.environ.get("DJANGO_STATIC_ROOT") or None  # only needed for collectstatic
WHITENOISE_USE_FINDERS = True  # serve admin assets without collectstatic, with or without DEBUG

LOGIN_URL = "admin:login"

SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"  # "no-referrer" makes browsers send Origin: null, which fails CSRF
if env_bool("DJANGO_SECURE_COOKIES"):
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

# Sending: your own mailbox over SMTP (pipeline/services/sending.py). Empty SMTP_HOST or SENDER_EMAIL: nothing is sent.
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = os.environ.get("SMTP_HOST", "").strip()
EMAIL_PORT = int(os.environ.get("SMTP_PORT", "587") or 587)
EMAIL_HOST_USER = os.environ.get("SMTP_USERNAME", "").strip()
EMAIL_HOST_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_SECURITY = os.environ.get("SMTP_SECURITY", "starttls").strip().lower()  # starttls (587), ssl (465) or none
EMAIL_USE_TLS = SMTP_SECURITY == "starttls"
EMAIL_USE_SSL = SMTP_SECURITY == "ssl"
EMAIL_TIMEOUT = 60
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "").strip()
SENDER_NAME = os.environ.get("SENDER_NAME", "").strip()
if TESTING:  # never your mailbox: tests use Django's in-memory outbox and set these themselves
    EMAIL_HOST = EMAIL_HOST_USER = EMAIL_HOST_PASSWORD = SENDER_EMAIL = ""

# Every API request must send this value in its `Auth` header (pipeline/api/security.py). Empty: API disabled.
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "").strip()

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "pipeline.api.security.AuthHeaderAuthentication",
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["pipeline.api.security.HasAuthHeader"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Email Marketing Automation API",
    "DESCRIPTION": "Campaigns, businesses, contacts, prospects, outreach emails and pipeline runs.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SERVE_PERMISSIONS": ["rest_framework.permissions.IsAdminUser"],
    "ENUM_NAME_OVERRIDES": {
        "ProgressStatusEnum": "pipeline.models.DiscoveryCampaign.Status",
        "EmailStatusEnum": "pipeline.models.Email.Status",
        "RunStatusEnum": "pipeline.models.PipelineRun.Status",
        "ContactEmailStatusEnum": "pipeline.models.BusinessContact.EmailStatus",
        "ProspectEmailStatusEnum": "pipeline.models.Prospect.EmailStatus",
        "OutreachStatusEnum": "pipeline.models.Prospect.OutreachStatus",
    },
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "WARNING"},
}
