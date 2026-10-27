from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import tempfile
import time
from collections import deque
from pathlib import Path

import asyncpg
from redis.asyncio import Redis

from .channels import ChannelImporter, claim_due
from .codex import TRANSIENT_SQL, CodexPool, CodexRuntime
from .config import get_settings
from .db import connect
from .dedupe import groups, merge
from .fallback import FallbackAnalyzer
from .importer import Importer
from .media import extract_audio, repair_media, thumb_hash
from .pipeline import UNREADABLE, MLClient, Pipeline, media_error
from .rqueue import STREAM_HIGH, STREAM_LOW, JobQueue, Message
from .storage import Storage, safe_join
from .transcribe import Transcriber

log = logging.getLogger("memesearch.worker")

MEDIA_STATS = "ms:media:stats"
MEDIA_BATCH = 20
MEDIA_ATTEMPTS = 3
MEDIA_IDLE_SECONDS = 30
SPEECH_STATS = "ms:speech:stats"
DEDUPE_REPORT = "ms:dedupe:report"
DEDUPE_LOCK = "ms:dedupe:lock"
SPEECH_IDLE_SECONDS = 60
SPEECH_ERROR_SECONDS = 600


class Worker:
    def __init__(self) -> None:
        self.s = get_settings()
        self.consumer = f"{socket.gethostname()}-{os.getpid()}"
        self.stop = asyncio.Event()
        self.tasks: set[asyncio.Task] = set()
        self.imports = 0

    async def setup(self) -> None:
        self.s.ensure_dirs()
        self.pool = await connect(self.s.database_url, max_size=self.s.worker_concurrency + 6)
        self.redis = Redis.from_url(self.s.redis_url, decode_responses=True, socket_timeout=30, socket_connect_timeout=10)
        self.queue = JobQueue(self.redis)
        await self.queue.ensure_groups()
        runtime = CodexRuntime(self.s)
        transcriber = None
        if self.s.whisper_model:
            transcriber = Transcriber(
                self.s.whisper_model, self.s.whisper_compute_type, self.s.whisper_threads, self.s.data_dir / "models" / "whisper"
            )
        self.pipeline = Pipeline(
            self.s,
            self.pool,
            self.redis,
            self.queue,
            CodexPool(self.s, self.pool, runtime),
            FallbackAnalyzer(self.s),
            transcriber,
            MLClient(self.s),
        )
        storage = Storage(self.s.upload_dir, self.pool, self.queue, self.s.max_file_mb << 20)
        self.importer = Importer(self.pool, storage, self.redis)
        self.channels = ChannelImporter(self.s, self.pool, storage, self.redis)
        self.active = 0
        self.background = 0
        self.phash_cursor = 0
        self.media_retry_at = 0.0
        self.media_done: deque[float] = deque()
        self.changed = asyncio.Condition()

    async def handle(self, msg: Message) -> None:
        requeue = False
        try:
            kind = msg.job.get("type")
            if kind == "process":
                await self.pipeline.handle(msg.job)
            elif kind == "dedupe":
                await self.dedupe(bool(msg.job.get("apply")))
            elif kind == "import":
                if self.imports >= 1 or self.background + self.imports >= max(1, self.s.worker_concurrency - 1):
                    await self.pool.execute(
                        "UPDATE import_jobs SET stage='waiting for a free worker slot', updated_at=now() "
                        "WHERE id=$1 AND status='pending'",
                        int(msg.job["job_id"]),
                    )
                    await self.queue.delay(msg.job, 10, high=True)
                    return
                self.imports += 1
                try:
                    await self.importer.run(int(msg.job["job_id"]))
                finally:
                    self.imports -= 1
            elif kind == "channel":
                cap = max(1, min(self.s.background_concurrency, self.s.worker_concurrency - 1 - self.imports))
                if self.background >= cap:
                    await self.queue.delay(msg.job, 30, high=True)
                    return
                self.background += 1
                try:
                    await self.channels.run(int(msg.job["id"]), float(msg.job.get("at") or 0))
                finally:
                    self.background -= 1
            else:
                log.warning("unknown job %s", msg.job)
        except asyncio.CancelledError:
            requeue = True
            raise
        except Exception:
            log.exception("job %s crashed", msg.job)
            if msg.job.get("type") == "process":
                await self.queue.delay(msg.job, 300)
        finally:
            if requeue:
                await self.queue.push(msg.stream, msg.job)
            await self.queue.ack(msg)
            async with self.changed:
                self.active -= 1
                self.changed.notify_all()

    def spawn(self, msg: Message) -> None:
        self.active += 1
        task = asyncio.create_task(self.handle(msg))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def consume(self) -> None:
        limit = self.s.worker_concurrency
        while not self.stop.is_set():
            async with self.changed:
                await self.changed.wait_for(lambda: self.active < limit)
                free = limit - self.active
            try:
                messages = await self.queue.read(self.consumer, count=free, block_ms=5000)
            except Exception as exc:
                log.warning("queue read failed: %s", exc)
                await asyncio.sleep(2)
                continue
            for msg in messages:
                self.spawn(msg)

    async def maintenance(self) -> None:
        tick = 0
        while not self.stop.is_set():
            try:
                await self.queue.promote_due()
                if tick % 60 == 59:
                    await self.recover()
                if tick % 12 == 0:
                    await self.poll_channels()
                    await self.wake_codex_waiters()
                if tick % 60 == 30:
                    await self.backfill_phash()
                if tick % 120 == 90:
                    await self.auto_retry_failed()
                if tick % 60 == 45:
                    await self.pipeline.codex.refresh_idle()
                if tick % 120 == 60:
                    await self.refresh_tag_stats()
            except Exception as exc:
                log.warning("maintenance: %s", exc)
            tick += 1
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

    async def recover(self, startup: bool = False) -> None:
        idle_ms = (self.s.job_timeout_seconds + 300) * 1000
        claimed = await self.queue.reclaim(self.consumer, idle_ms)
        for msg in claimed:
            self.spawn(msg)
        drained = await self.queue.backlog() <= self.s.worker_concurrency + len(claimed)
        stale = await self.pool.fetch(
            """SELECT id FROM memes WHERE
            (status='processing' AND ($2 OR updated_at < now() - make_interval(secs => $1)))
            OR ($3 AND status='pending' AND updated_at < now() - interval '30 minutes')
            ORDER BY id LIMIT 5000""",
            self.s.job_timeout_seconds * 2,
            startup,
            drained,
        )
        orphans = await self._unlocked([r["id"] for r in stale], "ms:lock:meme:")
        if orphans:
            log.info("requeueing %d stale memes", len(orphans))
            await self.pool.execute(
                "UPDATE memes SET status='pending', updated_at=now() WHERE id = ANY($1) AND status IN ('processing','pending')",
                orphans,
            )
            await self.queue.push(STREAM_LOW, *({"type": "process", "id": i, "analyze": True} for i in orphans))
        jobs = await self.pool.fetch(
            "SELECT id FROM import_jobs WHERE status='running' AND started_at < now() - interval '30 minutes'"
        )
        for job_id in await self._unlocked([j["id"] for j in jobs], "ms:lock:import:"):
            log.info("resuming orphaned import %s", job_id)
            await self.queue.push(STREAM_HIGH, {"type": "import", "job_id": job_id})

    async def refresh_tag_stats(self) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("DELETE FROM tag_stats")
            await conn.execute(
                """INSERT INTO tag_stats (tag, n)
                SELECT t, count(*) FROM memes, unnest(tags) t WHERE status = 'done' AND NOT hidden GROUP BY t"""
            )

    async def auto_retry_failed(self) -> None:
        rows = await self.pool.fetch(
            """UPDATE memes SET status='pending', attempts=0, auto_retries=auto_retries+1, updated_at=now()
            WHERE id IN (
                SELECT id FROM memes WHERE status='failed' AND auto_retries < $1 AND error ~* $2
                AND updated_at < now() - make_interval(mins => 15 * (auto_retries + 1))
                ORDER BY id LIMIT 500
            ) RETURNING id""",
            self.s.auto_retry_failed,
            TRANSIENT_SQL,
        )
        if rows:
            log.info("retrying %d memes that failed on temporary errors", len(rows))
            await self.queue.push(STREAM_LOW, *({"type": "process", "id": r["id"], "analyze": True} for r in rows))

    async def wake_codex_waiters(self) -> None:
        if not await self.queue.codex_waiting():
            return
        app = await self.pipeline.app_settings.get()
        fallback = app.fallback_enabled and self.pipeline.fallback.configured
        if fallback or (app.codex_enabled and await self.pipeline.codex.available()):
            n = await self.queue.release_codex_waiters()
            if n:
                log.info("an analysis provider is available again, resumed %d memes", n)

    async def poll_channels(self) -> None:
        ids = await claim_due(self.pool, self.s.channel_poll_minutes)
        if ids:
            await self.queue.push(STREAM_HIGH, *({"type": "channel", "id": i} for i in ids))

    async def backfill_phash(self) -> None:
        rows = await self.pool.fetch(
            "SELECT id, thumb_path FROM memes WHERE phash IS NULL AND thumb_path <> '' AND id > $1 ORDER BY id LIMIT 500",
            self.phash_cursor,
        )
        for r in rows:
            self.phash_cursor = r["id"]
            try:
                h = await asyncio.to_thread(thumb_hash, safe_join(self.s.upload_dir, r["thumb_path"]))
            except Exception as exc:
                log.debug("phash %s: %s", r["id"], exc)
                continue
            await self.pool.execute("UPDATE memes SET phash=$2::text::bit(256) WHERE id=$1 AND phash IS NULL", r["id"], h)
        if not rows:
            self.phash_cursor = 0

    async def media_loop(self) -> None:
        while not self.stop.is_set():
            try:
                done = await self.repair_media_batch()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("media backfill: %s", exc)
                done = 0
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=1 if done else MEDIA_IDLE_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def repair_media_batch(self) -> int:
        now = time.monotonic()
        if now - self.media_retry_at > 60:
            self.media_retry_at = now
            n = await self.pool.fetchval(
                """WITH r AS (UPDATE memes SET media_error = '' WHERE media_error <> '' AND media_attempts < $1
                AND media_failed_at < now() - interval '10 minutes' * power(3, media_attempts - 1) RETURNING 1)
                SELECT count(*) FROM r""",
                MEDIA_ATTEMPTS,
            )
            if n:
                log.info("retrying media repair for %d memes", n)
        rows = await self.pool.fetch(
            """SELECT id, kind, file_path, thumb_path, blurhash FROM memes
            WHERE NOT media_ok AND media_error = '' AND thumb_path <> '' ORDER BY id DESC LIMIT $1""",
            MEDIA_BATCH,
        )
        await self.redis.hset(MEDIA_STATS, mapping={"seen_at": int(time.time())})
        done = 0
        for r in rows:
            if self.stop.is_set():
                break
            if await self.redis.exists(f"ms:lock:meme:{r['id']}"):
                continue
            await self.repair_one(r)
            done += 1
        return done

    async def repair_one(self, r: asyncpg.Record) -> None:
        try:
            fix = await asyncio.to_thread(
                repair_media,
                r["kind"],
                safe_join(self.s.upload_dir, r["file_path"]),
                safe_join(self.s.upload_dir, r["thumb_path"]),
                self.s.thumb_size,
                self.s.max_video_frames,
                not r["blurhash"],
            )
        except Exception as exc:
            error = media_error(exc) if isinstance(exc, UNREADABLE) else (str(exc) or type(exc).__name__)
            await self.pool.execute(
                """UPDATE memes SET media_attempts = media_attempts + 1, media_error = $2, media_failed_at = now()
                WHERE id = $1""",
                r["id"],
                error[:500],
            )
            await self.redis.hincrby(MEDIA_STATS, "failed", 1)
            await self.redis.hset(MEDIA_STATS, mapping={"last_error": f"#{r['id']}: {error[:300]}"})
            log.info("media repair for meme %s failed: %s", r["id"], error)
            return
        await self.pool.execute(
            """UPDATE memes SET media_ok = true, media_error = '', media_attempts = 0,
            blurhash = CASE WHEN $2 <> '' THEN $2 ELSE blurhash END,
            phash = COALESCE($3::text::bit(256), phash) WHERE id = $1""",
            r["id"],
            fix.blurhash,
            fix.phash,
        )
        self.media_done.append(time.time())
        while self.media_done and self.media_done[0] < time.time() - 600:
            self.media_done.popleft()
        pipe = self.redis.pipeline(transaction=False)
        pipe.hincrby(MEDIA_STATS, "done", 1)
        if fix.regenerated:
            pipe.hincrby(MEDIA_STATS, "regenerated", 1)
        if fix.remuxed:
            pipe.hincrby(MEDIA_STATS, "remuxed", 1)
        pipe.hset(MEDIA_STATS, mapping={"rate": round(len(self.media_done) / 10, 1), "seen_at": int(time.time())})
        await pipe.execute()

    async def speech_loop(self) -> None:
        while not self.stop.is_set():
            pause = SPEECH_IDLE_SECONDS
            try:
                app = await self.pipeline.app_settings.get()
                if app.transcribe and self.pipeline.transcriber:
                    pause = await self.transcribe_batch()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("speech backfill: %s", exc)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=pause)
            except asyncio.TimeoutError:
                pass

    async def transcribe_batch(self) -> float:
        rows = await self.pool.fetch(
            """SELECT id, file_path FROM memes WHERE kind = 'video' AND status = 'done' AND transcript = '' AND NOT speech_checked
            ORDER BY id DESC LIMIT 10"""
        )
        await self.redis.hset(SPEECH_STATS, mapping={"seen_at": int(time.time())})
        if not rows:
            return SPEECH_IDLE_SECONDS
        for r in rows:
            if self.stop.is_set():
                break
            if await self.redis.exists(f"ms:lock:meme:{r['id']}"):
                continue
            with tempfile.TemporaryDirectory(dir=self.s.work_dir) as d:
                wav = Path(d) / "audio.wav"
                try:
                    extracted = await asyncio.to_thread(
                        extract_audio, safe_join(self.s.upload_dir, r["file_path"]), wav, self.s.max_transcribe_seconds
                    )
                except Exception as exc:
                    log.info("speech backfill: meme %s has no readable audio: %s", r["id"], exc)
                    extracted = False
                if not extracted:
                    await self.pool.execute("UPDATE memes SET speech_checked = true WHERE id = $1", r["id"])
                    continue
                try:
                    text, _ = await asyncio.to_thread(self.pipeline.transcriber.transcribe, wav)
                except Exception as exc:
                    log.warning("speech backfill paused, transcription failed: %s", exc)
                    await self.redis.hset(SPEECH_STATS, mapping={"last_error": str(exc)[:300]})
                    return SPEECH_ERROR_SECONDS
            await self.pool.execute(
                "UPDATE memes SET transcript = CASE WHEN $2 <> '' THEN $2 ELSE transcript END, speech_checked = true "
                "WHERE id = $1",
                r["id"],
                text,
            )
            if text:
                await self.queue.push(STREAM_LOW, {"type": "process", "id": r["id"], "analyze": False})
            pipe = self.redis.pipeline(transaction=False)
            pipe.hincrby(SPEECH_STATS, "done", 1)
            if text:
                pipe.hincrby(SPEECH_STATS, "with_speech", 1)
            pipe.hset(SPEECH_STATS, mapping={"seen_at": int(time.time()), "last_error": ""})
            await pipe.execute()
        return 0.5

    async def dedupe(self, apply: bool) -> None:
        if not await self.redis.set(DEDUPE_LOCK, "1", nx=True, ex=3600):
            return
        started = time.time()
        try:
            pipe = self.redis.pipeline()
            pipe.delete(DEDUPE_REPORT)
            pipe.hset(DEDUPE_REPORT, mapping={"state": "scanning", "apply": int(apply), "started_at": int(started)})
            await pipe.execute()
            rows = await self.pool.fetch(
                """SELECT id, kind = 'video' AS video, phash::text AS phash, locked, hidden, status
                FROM memes WHERE phash IS NOT NULL AND status <> 'failed'"""
            )
            found = await asyncio.to_thread(groups, [dict(r) for r in rows], self.s.channel_dedup_distance)
            pairs = [(g["keeper"], d, dist) for g in found for d, dist in g["dups"]]
            hist: dict[int, int] = {}
            for _, _, dist in pairs:
                hist[dist] = hist.get(dist, 0) + 1
            by_dist = sorted(pairs, key=lambda p: -p[2])
            samples = by_dist[:20] + pairs[:20]
            report = {
                "state": "merging" if apply else "preview",
                "scanned": len(rows),
                "groups": len(found),
                "to_remove": len(pairs),
                "distance": self.s.channel_dedup_distance,
                "hist": json.dumps(hist),
                "samples": json.dumps(list(dict.fromkeys(samples))),
                "merged": 0,
            }
            await self.redis.hset(DEDUPE_REPORT, mapping=report)
            if apply:
                merged = 0
                for n, (keeper, dup, _) in enumerate(pairs, 1):
                    if self.stop.is_set():
                        break
                    if await self.redis.exists(f"ms:lock:meme:{dup}"):
                        continue
                    try:
                        merged += await merge(self.pool, self.s.upload_dir, keeper, dup)
                    except Exception as exc:
                        log.warning("dedupe: could not merge %s into %s: %s", dup, keeper, exc)
                    if n % 50 == 0:
                        await self.redis.hset(DEDUPE_REPORT, "merged", merged)
                await self.redis.hset(DEDUPE_REPORT, mapping={"merged": merged, "state": "merged"})
                if merged:
                    await self.queue.bump()
                log.info("dedupe: merged %d duplicates into %d memes", merged, len(found))
            await self.redis.hset(DEDUPE_REPORT, mapping={"finished_at": int(time.time())})
        except Exception as exc:
            log.exception("dedupe failed")
            await self.redis.hset(DEDUPE_REPORT, mapping={"state": "failed", "error": str(exc)[:300]})
        finally:
            await self.redis.delete(DEDUPE_LOCK)

    async def _unlocked(self, ids: list[int], prefix: str) -> list[int]:
        if not ids:
            return []
        held = await self.redis.mget([f"{prefix}{i}" for i in ids])
        return [i for i, v in zip(ids, held, strict=True) if v is None]

    async def run(self) -> None:
        await self.setup()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop.set)
        log.info("worker %s started, concurrency=%d", self.consumer, self.s.worker_concurrency)
        try:
            await self.recover(startup=True)
        except Exception as exc:
            log.warning("startup recovery: %s", exc)
        maint = asyncio.create_task(self.maintenance())
        media = asyncio.create_task(self.media_loop())
        speech = asyncio.create_task(self.speech_loop())
        consumer = asyncio.create_task(self.consume())
        await self.stop.wait()
        consumer.cancel()
        log.info("draining %d jobs", len(self.tasks))
        if self.tasks:
            await asyncio.wait(self.tasks, timeout=self.s.shutdown_drain_seconds)
        if self.tasks:
            log.info("requeueing %d unfinished jobs", len(self.tasks))
            for task in self.tasks:
                task.cancel()
            await asyncio.wait(self.tasks, timeout=10)
        maint.cancel()
        media.cancel()
        speech.cancel()
        await self.pool.close()
        await self.redis.aclose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(Worker().run())


if __name__ == "__main__":
    main()
