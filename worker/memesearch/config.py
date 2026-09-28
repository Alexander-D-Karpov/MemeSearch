from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    redis_url: str = "redis://redis:6379/0"
    internal_token: str
    ml_url: str = "http://ml:8001"
    web_url: str = "http://web:8080"
    public_url: str = "http://localhost:8080"

    upload_dir: Path = Path("/media")
    data_dir: Path = Path("/data")
    codex_dir: Path | None = None
    work_dir: Path | None = None

    worker_concurrency: int = 4
    max_attempts: int = 3
    job_timeout_seconds: int = 900
    max_file_mb: int = 512

    codex_model: str | None = None
    codex_reasoning_effort: str = "low"
    codex_concurrency: int = 2
    codex_per_session: int = 2
    codex_timeout_seconds: int = 300
    codex_default_cooldown_minutes: int = 30
    codex_login_timeout_seconds: int = 900
    openai_proxy: str | None = None
    codex_http_proxy: str | None = None
    codex_https_proxy: str | None = None
    codex_all_proxy: str | None = None
    no_proxy: str = "127.0.0.1,localhost,ml,web,postgres,redis"

    fallback_base_url: str = "https://api.openai.com/v1"
    fallback_api_key: str | None = None
    fallback_use_proxy: bool = True
    fallback_model: str = "gpt-4.1-mini"
    fallback_concurrency: int = 4
    fallback_timeout_seconds: int = 180
    fallback_image_detail: str = "high"

    clip_model: str = "google/siglip2-base-patch16-256"
    text_model: str = "intfloat/multilingual-e5-small"
    clip_dim: int = 768
    text_dim: int = 384
    embed_threads: int = 4

    whisper_model: str | None = "small"
    whisper_compute_type: str = "int8"
    whisper_threads: int = 4
    max_transcribe_seconds: int = 600

    max_video_frames: int = 8
    max_gif_frames: int = 6
    analysis_max_side: int = 1600
    frame_max_side: int = 1024
    thumb_size: int = 480

    telegram_bot_token: str | None = None
    telegram_admin_ids: str = ""
    telegram_api_url: str | None = None
    telegram_api_local: bool = False
    telegram_proxy: str | None = None
    bot_max_file_mb: int = 20
    telegram_inline_public: bool = True
    telegram_cache_chat_id: int | None = None
    telegram_cache_interval: float = 1.2

    channel_poll_minutes: int = 30
    channel_max_video_seconds: int = 180
    channel_max_file_mb: int = 100
    channel_dedup_distance: int = 12
    channel_page_delay: float = 1.5
    telegram_web_proxy: str | None = None
    telegram_web_url: str = "https://t.me"

    @field_validator(
        "codex_model",
        "openai_proxy",
        "codex_http_proxy",
        "codex_https_proxy",
        "codex_all_proxy",
        "fallback_api_key",
        "whisper_model",
        "telegram_bot_token",
        "telegram_api_url",
        "telegram_proxy",
        "telegram_web_proxy",
        "telegram_cache_chat_id",
        mode="before",
    )
    @classmethod
    def blank_to_none(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    def model_post_init(self, __context: object) -> None:
        if self.codex_dir is None:
            self.codex_dir = self.data_dir / "codex"
        if self.work_dir is None:
            self.work_dir = self.data_dir / "work"

    @property
    def admin_ids(self) -> set[int]:
        return {int(x) for x in self.telegram_admin_ids.replace(" ", "").split(",") if x}

    @property
    def cache_chat_id(self) -> int | None:
        if self.telegram_cache_chat_id:
            return self.telegram_cache_chat_id
        ids = [int(x) for x in self.telegram_admin_ids.replace(" ", "").split(",") if x]
        return ids[0] if ids else None

    @property
    def web_proxy(self) -> str | None:
        return self.telegram_web_proxy or self.telegram_proxy or self.openai_proxy

    @property
    def fallback_configured(self) -> bool:
        return bool(self.fallback_api_key and self.fallback_base_url)

    def ensure_dirs(self) -> None:
        for p in (
            self.codex_dir,
            self.work_dir,
            self.upload_dir / "originals",
            self.upload_dir / "thumbs",
            self.upload_dir / ".tmp",
            self.upload_dir / "inbox",
        ):
            p.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
