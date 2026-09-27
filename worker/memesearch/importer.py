from __future__ import annotations

import asyncio
import logging
import os
import uuid
import zipfile
from pathlib import Path, PurePosixPath

import asyncpg
from redis.asyncio import Redis

from .rqueue import STREAM_LOW
from .storage import Storage, TooLarge, Unsupported

log = logging.getLogger(__name__)

MEDIA_SUFFIXES = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".heic",
    ".heif",
    ".avif",
    ".mp4",
    ".m4v",
    ".mov",
    ".webm",
    ".mkv",
    ".avi",
}


class Counters:
    def __init__(self) -> None:
        self.added = self.duplicates = self.skipped = self.failed = 0


LOCK_TTL = 900

_EXTEND = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


def lock_key(job_id: int) -> str:
    return f"ms:lock:import:{job_id}"


class Importer:
    def __init__(self, pool: asyncpg.Pool, storage: Storage, redis: Redis) -> None:
        self.pool = pool
        self.storage = storage
        self.redis = redis
        self._extend = redis.register_script(_EXTEND)
        self._release = redis.register_script(_RELEASE)

    async def _heartbeat(self, key: str, token: str) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                if not await self._extend(keys=[key], args=[token, LOCK_TTL]):
                    log.error("lost import lock %s", key)
            except Exception as exc:
                log.warning("import lock heartbeat failed: %s", exc)

    async def run(self, job_id: int) -> None:
        key = lock_key(job_id)
        token = uuid.uuid4().hex
        if not await self.redis.set(key, token, nx=True, ex=LOCK_TTL):
            log.info("import %s is already running", job_id)
            return
        beat = asyncio.create_task(self._heartbeat(key, token))
        try:
            await self._run(job_id)
        finally:
            beat.cancel()
            await self._release(keys=[key], args=[token])

    async def _run(self, job_id: int) -> None:
        job = await self.pool.fetchrow("SELECT * FROM import_jobs WHERE id=$1", job_id)
        if job is None or job["status"] in ("done", "failed"):
            return
        await self.pool.execute("UPDATE import_jobs SET status='running', started_at=now(), error='' WHERE id=$1", job_id)
        c = Counters()
        try:
            if job["kind"] == "zip":
                await self._zip(job_id, Path(job["path"]), job["name"], c)
                Path(job["path"]).unlink(missing_ok=True)
            else:
                await self._dir(job_id, Path(job["path"]), c)
            await self._progress(job_id, c, final="done")
        except Exception as exc:
            log.exception("import %s failed", job_id)
            await self._progress(job_id, c, final="failed", error=str(exc)[:2000])

    async def _progress(self, job_id: int, c: Counters, final: str | None = None, error: str = "") -> None:
        await self.pool.execute(
            """UPDATE import_jobs SET added=$2, duplicates=$3, skipped=$4, failed=$5,
            status=COALESCE($6, status), error=CASE WHEN $7 <> '' THEN $7 ELSE error END,
            finished_at=CASE WHEN $6 IS NOT NULL THEN now() ELSE finished_at END WHERE id=$1""",
            job_id,
            c.added,
            c.duplicates,
            c.skipped,
            c.failed,
            final,
            error,
        )

    async def _one(self, c: Counters, stage, name: str, source: str, source_ref: str) -> bool:
        try:
            staged = await asyncio.to_thread(stage)
        except (Unsupported, TooLarge) as exc:
            log.info("skip %s: %s", name, exc)
            c.skipped += 1
            return False
        except Exception as exc:
            log.warning("import %s failed: %s", name, exc)
            c.failed += 1
            return False
        try:
            res = await self.storage.commit(staged, name=name, source=source, source_ref=source_ref, stream=STREAM_LOW)
        except Exception as exc:
            log.warning("import %s failed: %s", name, exc)
            c.failed += 1
            return False
        if res.duplicate:
            c.duplicates += 1
        else:
            c.added += 1
        return True

    async def _zip(self, job_id: int, path: Path, zip_name: str, c: Counters) -> None:
        zf = await asyncio.to_thread(zipfile.ZipFile, path)
        try:
            entries = []
            for info in zf.infolist():
                p = PurePosixPath(info.filename)
                if info.is_dir() or "__MACOSX" in p.parts or p.name.startswith("."):
                    continue
                entries.append(info)
            await self.pool.execute("UPDATE import_jobs SET total=$2 WHERE id=$1", job_id, len(entries))
            for n, info in enumerate(entries, 1):
                if info.file_size > self.storage.max_bytes:
                    c.skipped += 1
                    continue

                def stage(info=info):
                    with zf.open(info) as src:
                        return self.storage.stage(src)

                await self._one(c, stage, PurePosixPath(info.filename).name, "zip", f"{zip_name}:{info.filename}")
                if n % 20 == 0:
                    await self._progress(job_id, c)
        finally:
            zf.close()

    async def _dir(self, job_id: int, root: Path, c: Counters) -> None:
        files = await asyncio.to_thread(lambda: sorted(p for p in root.rglob("*") if p.is_file() and not p.name.startswith(".")))
        await self.pool.execute("UPDATE import_jobs SET total=$2 WHERE id=$1", job_id, len(files))
        for n, path in enumerate(files, 1):
            if path.suffix.lower() not in MEDIA_SUFFIXES:
                c.skipped += 1
                continue

            def stage(path=path):
                with open(path, "rb") as src:
                    return self.storage.stage(src)

            ok = await self._one(c, stage, path.name, "inbox", str(path.relative_to(root)))
            if ok:
                path.unlink(missing_ok=True)
            if n % 20 == 0:
                await self._progress(job_id, c)
        for d in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            try:
                os.rmdir(d)
            except OSError:
                pass
