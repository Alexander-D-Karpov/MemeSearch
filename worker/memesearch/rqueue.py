from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ResponseError

STREAM_HIGH = "ms:q:high"
STREAM_LOW = "ms:q:low"
DELAYED = "ms:q:delayed"
DELAYED_HIGH = "ms:q:delayed:high"
WAIT_CODEX = "ms:q:wait:codex"
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
        for stream in (STREAM_HIGH, STREAM_LOW):
            try:
                await self.r.xgroup_create(stream, GROUP, id="0", mkstream=True)
            except ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise

    async def push(self, stream: str, *jobs: dict[str, Any]) -> None:
        pipe = self.r.pipeline(transaction=False)
        now = time.time()
        for job in jobs:
            pipe.xadd(stream, {"job": json.dumps({"at": now, **job})})
        await pipe.execute()

    async def process(self, meme_id: int, stream: str = STREAM_HIGH, analyze: bool = True) -> None:
        await self.push(stream, {"type": "process", "id": meme_id, "analyze": analyze})

    async def delay(self, job: dict[str, Any], seconds: float, high: bool = False) -> None:
        job = {**job, "nonce": time.time_ns()}
        await self.r.zadd(DELAYED_HIGH if high else DELAYED, {json.dumps(job): time.time() + seconds})

    async def wait_codex(self, job: dict[str, Any], seconds: float) -> None:
        job = {**job, "nonce": time.time_ns()}
        await self.r.zadd(WAIT_CODEX, {json.dumps(job): time.time() + seconds})

    async def promote_due(self) -> int:
        now = time.time()
        n = int(await self._promote(keys=[DELAYED_HIGH, STREAM_HIGH], args=[now]))
        n += int(await self._promote(keys=[DELAYED, STREAM_LOW], args=[now]))
        return n + int(await self._promote(keys=[WAIT_CODEX, STREAM_LOW], args=[now]))

    async def backlog(self) -> int:
        pipe = self.r.pipeline(transaction=False)
        for key in (STREAM_HIGH, STREAM_LOW):
            pipe.xlen(key)
        for key in (DELAYED, DELAYED_HIGH, WAIT_CODEX):
            pipe.zcard(key)
        return sum(int(n or 0) for n in await pipe.execute())

    async def codex_waiting(self) -> int:
        return int(await self.r.zcard(WAIT_CODEX))

    async def release_codex_waiters(self) -> int:
        total = 0
        while n := int(await self._promote(keys=[WAIT_CODEX, STREAM_LOW], args=["+inf"])):
            total += n
        return total

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
            res = await self.r.xreadgroup(GROUP, consumer, {STREAM_HIGH: ">", STREAM_LOW: ">"}, count=count, block=block_ms)
        out: list[Message] = []
        for stream, entries in res or []:
            out.extend(self._parse(stream, entries))
        return out

    async def reclaim(self, consumer: str, min_idle_ms: int, count: int = 50) -> list[Message]:
        out: list[Message] = []
        for stream in (STREAM_HIGH, STREAM_LOW):
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
