import asyncio

import pytest

from memesearch.codex import CodexRuntime, classify
from memesearch.config import Settings
from memesearch.rqueue import Message
from memesearch.worker import Worker


class FakeQueue:
    def __init__(self):
        self.pushed = []
        self.acked = []

    async def push(self, stream, *jobs):
        self.pushed.append((stream, jobs))

    async def ack(self, msg):
        self.acked.append(msg.msg_id)


class SlowPipeline:
    async def handle(self, job):
        await asyncio.sleep(60)


def test_cancelled_job_is_requeued_and_acked():
    async def run():
        w = Worker()
        w.queue = FakeQueue()
        w.pipeline = SlowPipeline()
        w.changed = asyncio.Condition()
        w.active = 1
        msg = Message("ms:q:high", "1-0", {"type": "process", "id": 7, "analyze": True})
        task = asyncio.create_task(w.handle(msg))
        await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return w

    w = asyncio.run(run())
    assert w.queue.pushed == [("ms:q:high", ({"type": "process", "id": 7, "analyze": True},))]
    assert w.queue.acked == ["1-0"]
    assert w.active == 0


def test_codex_session_closes_process_when_startup_fails(monkeypatch):
    closed = []

    class Broken:
        async def __aenter__(self):
            raise BlockingIOError(11, "Resource temporarily unavailable")

        async def close(self):
            closed.append(True)

    runtime = CodexRuntime(Settings(database_url="postgresql://x", internal_token="x"))
    monkeypatch.setattr(runtime, "client", lambda sid: Broken())

    async def run():
        with pytest.raises(BlockingIOError):
            async with runtime.session(1):
                pass

    asyncio.run(run())
    assert closed == [True]
    assert classify(BlockingIOError(11, "Resource temporarily unavailable")) == "transient"


def test_background_jobs_never_take_every_slot():
    class Recorder(FakeQueue):
        def __init__(self):
            super().__init__()
            self.delayed = []

        async def delay(self, job, seconds):
            self.delayed.append((job["id"], seconds))

    class SlowChannels:
        def __init__(self):
            self.running = 0

        async def run(self, cid, at):
            self.running += 1
            await asyncio.sleep(0.05)

    async def run():
        w = Worker()
        w.s.worker_concurrency, w.s.background_concurrency = 4, 1
        w.queue, w.channels = Recorder(), SlowChannels()
        w.changed, w.active, w.background = asyncio.Condition(), 2, 0
        a = asyncio.create_task(w.handle(Message("ms:q:high", "1-0", {"type": "channel", "id": 1})))
        await asyncio.sleep(0.01)
        await w.handle(Message("ms:q:high", "2-0", {"type": "channel", "id": 2}))
        await a
        return w

    w = asyncio.run(run())
    assert w.channels.running == 1
    assert w.queue.delayed == [(2, 30)]
    assert w.queue.acked == ["2-0", "1-0"] and w.background == 0
