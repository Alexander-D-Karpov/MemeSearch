import asyncio
import subprocess

import pytest

from memesearch import pipeline
from memesearch.codex import CodexRuntime, classify
from memesearch.config import Settings
from memesearch.pipeline import Pipeline
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


class FakePool:
    def __init__(self, row):
        self.row = row
        self.calls = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        if "status=CASE WHEN status='done' THEN 'done' ELSE 'failed'" in sql:
            self.row["status"], self.row["error"] = "failed", args[1]
        elif "SET status=$2, error=$3" in sql:
            self.row["status"], self.row["error"] = args[1], args[2]

    async def fetchrow(self, sql, *args):
        return dict(self.row)


class EventQueue:
    def __init__(self):
        self.events = []
        self.delayed = []

    async def event(self, data):
        self.events.append(data)

    async def delay(self, job, seconds):
        self.delayed.append((job, seconds))


def make_pipeline(tmp_path, row):
    p = Pipeline.__new__(Pipeline)
    p.s = Settings(upload_dir=tmp_path, work_dir=tmp_path / "work")
    p.pool = FakePool(row)
    p.queue = EventQueue()
    return p


def meme_row(**kw):
    row = {
        "id": 5,
        "kind": "video",
        "file_path": "broken.mp4",
        "sha256": "ab" * 32,
        "status": "processing",
        "attempts": 1,
        "analyzed_at": None,
        "locked": False,
        "error": None,
    }
    row.update(kw)
    return row


def test_unreadable_media_fails_the_meme_without_retry(tmp_path, monkeypatch):
    (tmp_path / "broken.mp4").write_bytes(b"not a video")
    row = meme_row()
    p = make_pipeline(tmp_path, row)

    def broken(*a, **kw):
        raise subprocess.CalledProcessError(
            1, ["ffprobe", "/m/x.mp4"], stderr=b"moov atom not found\n/m/x.mp4: Invalid data found\n"
        )

    monkeypatch.setattr(pipeline, "prepare", broken)
    asyncio.run(p._run(row, True, tmp_path / "work"))
    assert row["status"] == "failed"
    assert row["error"] == "could not read the file: ffprobe exited with 1: Invalid data found"
    assert p.queue.delayed == []


def test_broken_image_fails_the_meme(tmp_path):
    (tmp_path / "broken.png").write_bytes(b"\x89PNG\r\n\x1a\nnope")
    row = meme_row(kind="image", file_path="broken.png")
    p = make_pipeline(tmp_path, row)
    asyncio.run(p._run(row, True, tmp_path / "work"))
    assert row["status"] == "failed"
    assert row["error"].startswith("could not read the file:")


def test_unexpected_error_is_retried_not_stuck(tmp_path, monkeypatch):
    row = meme_row(attempts=0, status="pending")
    p = make_pipeline(tmp_path, row)

    async def fetch(pool, meme_id):
        return dict(row)

    async def boom(meme, full, workdir):
        raise KeyError("surprise")

    monkeypatch.setattr(pipeline, "fetch_meme", fetch)
    p._run = boom
    asyncio.run(p._process(5, True, {"type": "process", "id": 5}))
    assert row["status"] == "pending"
    assert row["error"] == "unexpected error: KeyError: 'surprise'"
    assert len(p.queue.delayed) == 1
