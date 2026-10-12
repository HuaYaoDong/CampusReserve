from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "CampusReserve"
    app_env: str = "development"
    log_level: str = "INFO"
    postgres_dsn: str = (
        "postgresql+asyncpg://campus:campus_dev_password@localhost:5432/campus_reserve"
    )
    redis_url: str = "redis://localhost:6379/0"
    booking_stream: str = "campus-reserve:{booking}:reservation-events"
    booking_dead_letter_stream: str = (
        "campus-reserve:{booking}:reservation-events:dead-letter"
    )
    booking_consumer_group: str = "booking-workers"
    booking_claim_idle_ms: int = 30_000
    booking_max_deliveries: int = 5
    booking_result_ttl_seconds: int = 7 * 24 * 60 * 60
    jwt_secret: str = "replace-this-in-a-real-environment"
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "campus-reserve"
    jwt_access_token_minutes: int = 30
    timezone: str = "Asia/Shanghai"


@lru_cache
def get_settings() -> Settings:
    return Settings()
