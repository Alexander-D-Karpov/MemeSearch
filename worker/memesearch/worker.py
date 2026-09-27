from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket

from redis.asyncio import Redis

from .codex import CodexPool, CodexRuntime
from .config import get_settings
from .db import connect
from .fallback import FallbackAnalyzer
from .importer import Importer
from .pipeline import MLClient, Pipeline
from .rqueue import STREAM_HIGH, STREAM_LOW, JobQueue, Message
from .storage import Storage
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
        self.active = 0
        self.changed = asyncio.Condition()

    async def handle(self, msg: Message) -> None:
        try:
            kind = msg.job.get("type")
            if kind == "process":
                await self.pipeline.handle(msg.job)
            elif kind == "import":
                await self.importer.run(int(msg.job["job_id"]))
            else:
                log.warning("unknown job %s", msg.job)
        except Exception:
            log.exception("job %s crashed", msg.job)
            if msg.job.get("type") == "process":
                await self.queue.delay(msg.job, 300)
        finally:
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
                if tick % 60 == 0:
                    await self.recover()
            except Exception as exc:
                log.warning("maintenance: %s", exc)
            tick += 1
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

    async def recover(self) -> None:
        idle_ms = (self.s.job_timeout_seconds + 300) * 1000
        claimed = await self.queue.reclaim(self.consumer, idle_ms)
        for msg in claimed:
            self.spawn(msg)
        stale = await self.pool.fetch(
            """SELECT id FROM memes WHERE
            (status='processing' AND updated_at < now() - make_interval(secs => $1))
            OR (status='pending' AND updated_at < now() - interval '6 hours')""",
            self.s.job_timeout_seconds * 2,
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
        maint = asyncio.create_task(self.maintenance())
        consumer = asyncio.create_task(self.consume())
        await self.stop.wait()
        consumer.cancel()
        log.info("draining %d jobs", len(self.tasks))
        if self.tasks:
            await asyncio.wait(self.tasks, timeout=60)
        maint.cancel()
        await self.pool.close()
        await self.redis.aclose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(Worker().run())


if __name__ == "__main__":
    main()
