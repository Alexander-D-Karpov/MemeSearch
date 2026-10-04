from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import asyncpg
from pgvector.asyncpg import register_vector

log = logging.getLogger(__name__)


async def _init(conn: asyncpg.Connection) -> None:
    await register_vector(conn)
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


SCHEMA_PROBE = "song"


async def connect(dsn: str, min_size: int = 1, max_size: int = 10) -> asyncpg.Pool:
    last: Exception | None = None
    for _ in range(60):
        try:
            conn = await asyncpg.connect(dsn)
            try:
                exists = await conn.fetchval(
                    """SELECT count(*) = 1 FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = 'memes' AND column_name = $1""",
                    SCHEMA_PROBE,
                )
            finally:
                await conn.close()
            if exists:
                return await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size, init=_init)
            log.info("waiting for schema migrations (run by web)")
        except (OSError, asyncpg.PostgresError) as exc:
            last = exc
            log.info("waiting for database: %s", exc)
        await asyncio.sleep(2)
    raise RuntimeError(f"database not ready: {last}")


@dataclass
class AnalysisSettings:
    prompt_extra: str = ""
    codex_enabled: bool = True
    fallback_enabled: bool = True
    codex_model: str = ""
    codex_effort: str = ""
    fallback_model: str = ""
    transcribe: bool = True


@dataclass
class SettingsCache:
    pool: asyncpg.Pool
    ttl: float = 20.0
    _value: AnalysisSettings = field(default_factory=AnalysisSettings)
    _at: float = 0.0

    async def get(self) -> AnalysisSettings:
        if time.monotonic() - self._at < self.ttl:
            return self._value
        raw = await self.pool.fetchval("SELECT value FROM app_settings WHERE key='analysis'")
        value = AnalysisSettings()
        if isinstance(raw, dict):
            for k, v in raw.items():
                if hasattr(value, k):
                    setattr(value, k, v)
        self._value = value
        self._at = time.monotonic()
        return value


async def fetch_meme(pool: asyncpg.Pool, meme_id: int) -> dict[str, Any] | None:
    row = await pool.fetchrow("SELECT * FROM memes WHERE id=$1", meme_id)
    return dict(row) if row else None
