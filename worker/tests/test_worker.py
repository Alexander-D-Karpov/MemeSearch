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
