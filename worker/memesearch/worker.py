from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket

from redis.asyncio import Redis

from .channels import ChannelImporter, claim_due
from .codex import TRANSIENT_SQL, CodexPool, CodexRuntime
from .config import get_settings
from .db import connect
from .fallback import FallbackAnalyzer
from .importer import Importer
from .media import thumb_hash
from .pipeline import MLClient, Pipeline
from .rqueue import STREAM_HIGH, STREAM_LOW, JobQueue, Message
from .storage import Storage, safe_join
from .transcribe import Transcriber

log = logging.getLogger("memesearch.worker")


class Worker:
    def __init__(self) -> None:
        self.s = get_settings()
        self.consumer = f"{socket.gethostname()}-{os.getpid()}"
        self.stop = asyncio.Event()
        self.tasks: set[asyncio.Task] = set()

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
        self.phash_cursor = 0
        self.changed = asyncio.Condition()

    async def handle(self, msg: Message) -> None:
        requeue = False
        try:
            kind = msg.job.get("type")
            if kind == "process":
                await self.pipeline.handle(msg.job)
            elif kind == "import":
                await self.importer.run(int(msg.job["job_id"]))
            elif kind == "channel":
                await self.channels.run(int(msg.job["id"]), float(msg.job.get("at") or 0))
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
        stale = await self.pool.fetch(
            """SELECT id FROM memes WHERE
            (status='processing' AND ($2 OR updated_at < now() - make_interval(secs => $1)))
            OR (status='pending' AND updated_at < now() - interval '6 hours')""",
            self.s.job_timeout_seconds * 2,
            startup,
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
        await self.pool.close()
        await self.redis.aclose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(Worker().run())


if __name__ == "__main__":
    main()
