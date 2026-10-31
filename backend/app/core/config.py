"""Environment-driven application settings."""

from functools import lru_cache
from secrets import token_urlsafe
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", case_sensitive=False, populate_by_name=True
    )

    app_env: Literal["development", "production"] = "development"
    database_url: str | None = Field(default=None, repr=False)
    demo_challenge_amount_minor: int = Field(default=500_000, gt=0)
    demo_block_amount_minor: int = Field(default=1_000_000, gt=0)
    jwt_secret: str | None = Field(default=None, repr=False)
    jwt_issuer: str = "finguard"
    jwt_audience: str = "finguard-api"
    access_token_minutes: int = Field(default=10, ge=1, le=60)
    refresh_token_days: int = Field(default=7, ge=1, le=30)
    argon2_time_cost: int = Field(default=3, ge=2)
    argon2_memory_cost: int = Field(default=65536, ge=19456)
    argon2_parallelism: int = Field(default=4, ge=1)
    cookie_secure: bool = True
    local_insecure_cookie: bool = False
    allowed_origins: str = ""
    trusted_hosts: str = "127.0.0.1,localhost,testserver"
    kafka_bootstrap_servers: str | None = Field(
        default=None, validation_alias="FINGUARD_KAFKA_BOOTSTRAP_SERVERS"
    )
    kafka_client_id: str = Field(
        default="finguard-outbox", validation_alias="FINGUARD_KAFKA_CLIENT_ID"
    )
    kafka_security_protocol: str = Field(
        default="PLAINTEXT", validation_alias="FINGUARD_KAFKA_SECURITY_PROTOCOL"
    )
    kafka_topic: str = Field(default="finguard.events", validation_alias="FINGUARD_KAFKA_TOPIC")
    redis_url: str | None = Field(default=None, repr=False, validation_alias="FINGUARD_REDIS_URL")
    feature_freshness_seconds: int = Field(
        default=120, ge=1, le=86400, validation_alias="FINGUARD_FEATURE_FRESHNESS_SECONDS"
    )
    feature_projection_version: str = Field(
        default="v1", validation_alias="FINGUARD_FEATURE_PROJECTION_VERSION"
    )
    outbox_batch_size: int = Field(default=50, ge=1, le=500)
    outbox_poll_seconds: float = Field(default=1.0, gt=0, le=60)
    outbox_lease_seconds: int = Field(default=60, ge=5, le=3600)
    outbox_max_attempts: int = Field(default=10, ge=1, le=100)

    @model_validator(mode="after")
    def validate_database_url(self) -> "Settings":
        if self.app_env == "production" and not self.database_url:
            raise ValueError("DATABASE_URL is required in production")
        if self.database_url and not self.database_url.startswith("postgresql+psycopg://"):
            raise ValueError("DATABASE_URL must use the postgresql+psycopg driver")
        if self.demo_block_amount_minor <= self.demo_challenge_amount_minor:
            raise ValueError("DEMO_BLOCK_AMOUNT_MINOR must exceed DEMO_CHALLENGE_AMOUNT_MINOR")
        if self.app_env == "production":
            if (
                not self.jwt_secret
                or len(self.jwt_secret.encode()) < 32
                or len(set(self.jwt_secret)) < 12
                or any(
                    word in self.jwt_secret.casefold()
                    for word in ("placeholder", "test-only", "changeme")
                )
            ):
                raise ValueError("Production JWT_SECRET must be at least 32 bytes")
            if not self.cookie_secure or self.local_insecure_cookie:
                raise ValueError("Production requires secure refresh cookies")
            if not self.allowed_origins or any(
                not origin.strip().startswith("https://")
                for origin in self.allowed_origins.split(",")
            ):
                raise ValueError("Production requires explicit CORS origins")
            if "*" in [host.strip() for host in self.trusted_hosts.split(",")]:
                raise ValueError("Production requires explicit trusted hosts")
        elif not self.jwt_secret:
            self.jwt_secret = token_urlsafe(48)
        if self.local_insecure_cookie and self.app_env != "development":
            raise ValueError("Insecure cookies are local development only")
        if "*" in [origin.strip() for origin in self.allowed_origins.split(",")]:
            raise ValueError("Wildcard CORS origins are unsupported")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
