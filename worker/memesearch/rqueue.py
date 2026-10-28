from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ResponseError

STREAM_HIGH = "ms:q:high"
STREAM_MID = "ms:q:mid"
STREAM_LOW = "ms:q:low"
STREAMS = (STREAM_HIGH, STREAM_MID, STREAM_LOW)
DELAYED = "ms:q:delayed"
DELAYED_MID = "ms:q:delayed:mid"
DELAYED_HIGH = "ms:q:delayed:high"
WAIT_CODEX = "ms:q:wait:codex"
WAIT_CODEX_MID = "ms:q:wait:codex:mid"
INDEX_VERSION = "ms:index:ver"
EVENTS = "ms:events"
GROUP = "workers"

_PROMOTE = """
local items = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, 200)
for _, item in ipairs(items) do
  redis.call('ZREM', KEYS[1], item)
  redis.call('XADD', KEYS[2], '*', 'job', item)
end
return #items
"""


def is_mid(job: dict[str, Any]) -> bool:
    return job.get("lane") == "mid"


@dataclass
class Message:
    stream: str
    msg_id: str
    job: dict[str, Any]


class JobQueue:
    def __init__(self, redis: Redis) -> None:
        self.r = redis
        self._promote = self.r.register_script(_PROMOTE)

    async def ensure_groups(self) -> None:
        for stream in STREAMS:
            try:
                await self.r.xgroup_create(stream, GROUP, id="0", mkstream=True)
            except ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise

    async def push(self, stream: str, *jobs: dict[str, Any]) -> None:
        pipe = self.r.pipeline(transaction=False)
        now = time.time()
        for job in jobs:
            if stream == STREAM_MID:
                job = {**job, "lane": "mid"}
            pipe.xadd(stream, {"job": json.dumps({"at": now, **job})})
        await pipe.execute()

    async def process(self, meme_id: int, stream: str = STREAM_HIGH, analyze: bool = True) -> None:
        await self.push(stream, {"type": "process", "id": meme_id, "analyze": analyze})

    async def delay(self, job: dict[str, Any], seconds: float, high: bool = False) -> None:
        job = {**job, "nonce": time.time_ns()}
        key = DELAYED_HIGH if high else DELAYED_MID if is_mid(job) else DELAYED
        await self.r.zadd(key, {json.dumps(job): time.time() + seconds})

    async def wait_codex(self, job: dict[str, Any], seconds: float) -> None:
        job = {**job, "nonce": time.time_ns()}
        await self.r.zadd(WAIT_CODEX_MID if is_mid(job) else WAIT_CODEX, {json.dumps(job): time.time() + seconds})

    async def promote_due(self) -> int:
        now = time.time()
        n = 0
        for src, dst in ((DELAYED_HIGH, STREAM_HIGH), (DELAYED_MID, STREAM_MID), (WAIT_CODEX_MID, STREAM_MID)):
            n += int(await self._promote(keys=[src, dst], args=[now]))
        n += int(await self._promote(keys=[DELAYED, STREAM_LOW], args=[now]))
        return n + int(await self._promote(keys=[WAIT_CODEX, STREAM_LOW], args=[now]))

    async def backlog(self) -> int:
        pipe = self.r.pipeline(transaction=False)
        for key in STREAMS:
            pipe.xlen(key)
        for key in (DELAYED, DELAYED_MID, DELAYED_HIGH, WAIT_CODEX, WAIT_CODEX_MID):
            pipe.zcard(key)
        return sum(int(n or 0) for n in await pipe.execute())

    async def codex_waiting(self) -> int:
        return int(await self.r.zcard(WAIT_CODEX)) + int(await self.r.zcard(WAIT_CODEX_MID))

    async def release_codex_waiters(self) -> int:
        total = 0
        for src, dst in ((WAIT_CODEX_MID, STREAM_MID), (WAIT_CODEX, STREAM_LOW)):
            while n := int(await self._promote(keys=[src, dst], args=["+inf"])):
                total += n
        return total

    async def move_to_mid(self, wanted) -> int:
        moved = 0
        pending = await self.r.xpending_range(STREAM_LOW, GROUP, min="-", max="+", count=10000)
        busy = {p["message_id"] for p in pending}
        start = "-"
        while entries := await self.r.xrange(STREAM_LOW, min=start, max="+", count=2000):
            start = "(" + entries[-1][0]
            batch = [m for m in self._parse(STREAM_LOW, entries) if m.msg_id not in busy and m.job.get("type") == "process"]
            ids = await wanted([int(m.job["id"]) for m in batch])
            pipe = self.r.pipeline(transaction=False)
            for m in batch:
                if int(m.job["id"]) in ids:
                    pipe.xadd(STREAM_MID, {"job": json.dumps({**m.job, "lane": "mid"})})
                    pipe.xdel(STREAM_LOW, m.msg_id)
                    moved += 1
            await pipe.execute()
        for src, dst in ((DELAYED, DELAYED_MID), (WAIT_CODEX, WAIT_CODEX_MID)):
            items = await self.r.zrange(src, 0, -1, withscores=True)
            for i in range(0, len(items), 2000):
                chunk = []
                for raw, score in items[i : i + 2000]:
                    try:
                        job = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    if job.get("type") == "process":
                        chunk.append((raw, score, job))
                ids = await wanted([int(j["id"]) for _, _, j in chunk])
                pipe = self.r.pipeline(transaction=False)
                for raw, score, job in chunk:
                    if int(job["id"]) in ids:
                        pipe.zrem(src, raw)
                        pipe.zadd(dst, {json.dumps({**job, "lane": "mid"}): score})
                        moved += 1
                await pipe.execute()
        return moved

    @staticmethod
    def _parse(stream: str, entries: list) -> list[Message]:
        out = []
        for msg_id, fields in entries:
            if not fields:
                continue
            raw = fields.get("job") or fields.get(b"job")
            try:
                job = json.loads(raw)
            except (TypeError, ValueError):
                job = {"type": "invalid"}
            out.append(Message(stream, msg_id, job))
        return out

    async def read(self, consumer: str, count: int, block_ms: int = 5000) -> list[Message]:
        res = await self.r.xreadgroup(GROUP, consumer, {STREAM_HIGH: ">"}, count=count)
        if not res:
            res = await self.r.xreadgroup(GROUP, consumer, {STREAM_MID: ">"}, count=count)
        if not res:
            res = await self.r.xreadgroup(GROUP, consumer, dict.fromkeys(STREAMS, ">"), count=count, block=block_ms)
        out: list[Message] = []
        for stream, entries in res or []:
            out.extend(self._parse(stream, entries))
        return out

    async def reclaim(self, consumer: str, min_idle_ms: int, count: int = 50) -> list[Message]:
        out: list[Message] = []
        for stream in STREAMS:
            res = await self.r.xautoclaim(stream, GROUP, consumer, min_idle_time=min_idle_ms, start_id="0-0", count=count)
            entries = res[1] if res else []
            out.extend(self._parse(stream, entries))
        return out

    async def ack(self, msg: Message) -> None:
        pipe = self.r.pipeline(transaction=False)
        pipe.xack(msg.stream, GROUP, msg.msg_id)
        pipe.xdel(msg.stream, msg.msg_id)
        await pipe.execute()

    async def event(self, payload: dict[str, Any]) -> None:
        await self.r.publish(EVENTS, json.dumps(payload))

    async def bump(self) -> None:
        await self.r.incr(INDEX_VERSION)
