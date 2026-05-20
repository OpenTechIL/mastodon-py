"""Application settings.

Reads the deployment's environment variables (DB_*, REDIS_*,
SECRET_KEY_BASE, LOCAL_DOMAIN, S3_*, ES_*, SMTP_*) and exposes them as
typed attributes. Defaults mirror `config/mastodon.yml`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.development"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    env: Literal["development", "test", "production"] = Field(
        default="development", validation_alias="MASTODON_ENV"
    )

    local_domain: str = "localhost:3000"
    web_domain: str | None = None
    secret_key_base: str = ""

    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str = "mastodon"
    db_pass: str = ""
    db_name: str = "mastodon_development"
    db_sslmode: str = "prefer"

    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: str | None = None
    redis_namespace: str | None = None
    redis_url: str | None = None

    es_enabled: bool = False
    es_host: str = "localhost"
    es_port: int = 9200
    es_user: str | None = None
    es_pass: str | None = None

    s3_enabled: bool = False
    s3_bucket: str | None = None
    s3_alias_host: str | None = None
    s3_region: str | None = None
    s3_endpoint: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None

    smtp_server: str | None = None
    smtp_port: int = 587
    smtp_login: str | None = None
    smtp_password: str | None = None
    smtp_from_address: str = "notifications@localhost"

    vapid_public_key: str | None = None
    vapid_private_key: str | None = None

    # Local-filesystem storage for media uploads. S3/Azure/Swift adapters
    # land alongside the full Paperclip-replacement pipeline.
    media_root: str = "public/system"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> PostgresDsn:
        return PostgresDsn.build(
            scheme="postgresql+asyncpg",
            username=self.db_user,
            password=self.db_pass or None,
            host=self.db_host,
            port=self.db_port,
            path=self.db_name,
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def redis_dsn(self) -> RedisDsn:
        if self.redis_url:
            return RedisDsn(self.redis_url)
        return RedisDsn.build(
            scheme="redis",
            password=self.redis_password,
            host=self.redis_host,
            port=self.redis_port,
        )

    @property
    def effective_web_domain(self) -> str:
        return self.web_domain or self.local_domain

    @property
    def url_scheme(self) -> str:
        return "https" if self.env == "production" else "http"

    def base_url(self, path: str = "") -> str:
        return f"{self.url_scheme}://{self.effective_web_domain}{path}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
