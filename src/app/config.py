# All third-party fields are optional so the app can boot (and /health can serve)
# with no environment configured. Region defaults are EU per CLAUDE.md — never
# change SUPABASE_REGION or the R2 bucket jurisdiction to a non-EU value.

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "dev"
    log_level: str = "INFO"
    cors_allow_origins: str = "*"

    supabase_region: str = "eu-central-1"
    supabase_url: str | None = None
    supabase_jwt_secret: str | None = None
    supabase_service_role_key: str | None = None

    r2_account_id: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None
    r2_bucket: str | None = None
    r2_region: str = "auto"

    firebase_service_account_b64: str | None = None

    resend_api_key: str | None = None
    resend_from_email: str | None = None

    # Gates /dev/* routes. MUST be false in production.
    dev_endpoints_enabled: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
