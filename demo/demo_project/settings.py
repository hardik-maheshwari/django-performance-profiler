"""Minimal settings for the profiler demo. Not for production."""
import os

SECRET_KEY = "demo-only-not-secret"
DEBUG = True
ALLOWED_HOSTS = ["*"]
ROOT_URLCONF = "demo_project.urls"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
    "shop",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": os.environ.get("DB_HOST", "localhost"),
        "NAME": os.environ.get("DB_NAME", "profiler"),
        "USER": os.environ.get("DB_USER", "profiler"),
        "PASSWORD": os.environ.get("DB_PASSWORD", "profiler"),
    }
}

ES_URL = os.environ.get("ES_URL", "http://localhost:9200")

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": [],
    "UNAUTHENTICATED_USER": None,
}

# The profilers report through logging; show them on the console.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "loggers": {"django_perf_profiler": {"handlers": ["console"], "level": "INFO"}},
}
