import os
from datetime import date


def _bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-in-production")

    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg2://spidqah:spidqah@localhost:5432/spidqah",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    WTF_CSRF_ENABLED = True

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _bool(os.environ.get("SESSION_COOKIE_SECURE"), False)
    REMEMBER_COOKIE_HTTPONLY = True

    ORG_NAME = os.environ.get("ORG_NAME", "SPIDQAH")
    CURRENCY_CODE = "UGX"
    APP_TIMEZONE = "Africa/Kampala"

    # Default go-live date. This is only the *seed* default for the
    # AppSetting row "go_live_date" - once the app has run once, the
    # value stored in the database (editable by Admin) always wins.
    DEFAULT_GO_LIVE_DATE = date.fromisoformat(
        os.environ.get("DEFAULT_GO_LIVE_DATE", "2026-09-18")
    )

    RATELIMIT_ENABLED = _bool(os.environ.get("RATELIMIT_ENABLED"), True)
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")

    FIRST_ADMIN_USERNAME = os.environ.get("FIRST_ADMIN_USERNAME")
    FIRST_ADMIN_EMAIL = os.environ.get("FIRST_ADMIN_EMAIL")
    FIRST_ADMIN_PASSWORD = os.environ.get("FIRST_ADMIN_PASSWORD")


class DevelopmentConfig(Config):
    DEBUG = True


class TestingConfig(Config):
    TESTING = True
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_DATABASE_URI = os.environ.get("TEST_DATABASE_URL", "sqlite:///:memory:")
    RATELIMIT_ENABLED = False

    if SQLALCHEMY_DATABASE_URI.startswith("sqlite") and ":memory:" in SQLALCHEMY_DATABASE_URI:
        from sqlalchemy.pool import StaticPool
        SQLALCHEMY_ENGINE_OPTIONS = {
            "poolclass": StaticPool,
            "connect_args": {"check_same_thread": False},
        }


class ProductionConfig(Config):
    DEBUG = False
    SESSION_COOKIE_SECURE = _bool(os.environ.get("SESSION_COOKIE_SECURE"), True)


config_by_name = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}


def get_config(name=None):
    name = name or os.environ.get("FLASK_ENV", "production")
    return config_by_name.get(name, ProductionConfig)
