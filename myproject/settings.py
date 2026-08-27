"""
Django settings for myproject.
Secrets and SMTP credentials are read from the root .env file.
"""

import os
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


# Never hardcode real keys or SMTP passwords in this file.
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    raise RuntimeError("DJANGO_SECRET_KEY is missing. Add it in the root .env file.")

DEBUG = os.environ.get("DJANGO_DEBUG", "False").lower() == "true"

ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get(
        "DJANGO_ALLOWED_HOSTS",
        "127.0.0.1,localhost",
    ).split(",")
    if host.strip()
]

CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]


INSTALLED_APPS = [
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "channels",
    "myapp",
    "food",
    "network",
    "scraper_app",
    "django.contrib.sites",
    "rest_framework",
    "rest_framework.authtoken",
    "api_app",
]

MIDDLEWARE = [
    "api_app.middleware.SimpleCorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "myproject.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
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

WSGI_APPLICATION = "myproject.wsgi.application"
ASGI_APPLICATION = "myproject.asgi.application"

if os.environ.get("DATABASE_URL"):
    # Render/Supabase Transaction Pooler
    import dj_database_url

    DATABASES = {
        "default": dj_database_url.config(
            conn_max_age=0,
            ssl_require=True,
        )
    }

    DISABLE_SERVER_SIDE_CURSORS = True
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
            "OPTIONS": {
                "timeout": 20,
            },
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# ⭐ Cloudinary (free, no card) — media permanent + CDN-fast.
# Env set na ho to local disk fallback (dev ke liye).
if os.environ.get("CLOUDINARY_CLOUD_NAME"):
    STORAGES["default"] = {
        "BACKEND": "cloudinary_storage.storage.RawMediaCloudinaryStorage",
    }
    CLOUDINARY_STORAGE = {
        "CLOUD_NAME": os.environ.get("CLOUDINARY_CLOUD_NAME", ""),
        "API_KEY": os.environ.get("CLOUDINARY_API_KEY", ""),
        "API_SECRET": os.environ.get("CLOUDINARY_API_SECRET", ""),
        "PREFIX": "cunnect",
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
SITE_ID = 1

# Realtime chat development layer. Use Redis on production/multiple servers.
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer",
    },
}

# Persistent login session: one year while the user remains active.
SESSION_COOKIE_AGE = 60 * 60 * 24 * 365
SESSION_SAVE_EVERY_REQUEST = True
SESSION_EXPIRE_AT_BROWSER_CLOSE = False

# SMTP / OTP emails. Values are supplied through .env only.
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = "smtp.gmail.com"
EMAIL_PORT = 587
EMAIL_USE_TLS = True
EMAIL_USE_SSL = False
EMAIL_TIMEOUT = 20

EMAIL_HOST_USER = os.environ.get("CUNNECT_SMTP_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("CUNNECT_SMTP_APP_PASSWORD", "")

DEFAULT_FROM_EMAIL = (
    f"CUnnect <{EMAIL_HOST_USER}>"
    if EMAIL_HOST_USER
    else "CUnnect"
)
SERVER_EMAIL = DEFAULT_FROM_EMAIL
