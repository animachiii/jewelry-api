from app.config import settings


def test_settings_default_app_env() -> None:
    assert settings.APP_ENV == "local"
