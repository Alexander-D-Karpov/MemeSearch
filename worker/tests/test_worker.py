import asyncio

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
