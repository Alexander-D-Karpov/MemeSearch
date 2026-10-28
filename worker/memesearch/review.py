from __future__ import annotations

import asyncio
import logging
import shutil
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import asyncpg
from PIL import Image
from redis.asyncio import Redis

from .codex import CodexPool, CodexUnavailable
from .config import Settings
from .db import SettingsCache
from .dedupe import merge
from .prompt import COMPARE_SCHEMA, COMPARE_SYSTEM, compare_prompt, parse_compare
from .storage import safe_join

log = logging.getLogger(__name__)

REVIEW = "ms:dedupe:review"
REVIEW_LOCK = "ms:dedupe:review:lock"
REVIEW_STOP = "ms:dedupe:review:stop"
MIN_CONFIDENCE = 0.8
BATCH = 8
PHOTO_EXTS = {"jpg", "jpeg", "png", "webp"}

MEME_SQL = """SELECT id, kind, ext, file_path, thumb_path, locked, hidden, status, width, height, size_bytes
FROM memes WHERE id = ANY($1)"""
SEND_SIDE = 1536


def choose(first: Any, second: Any, better: int) -> tuple[Any, Any]:
    pick, other = (first, second) if better == 1 else (second, first)
    if (other["locked"] and not pick["locked"]) or (pick["hidden"] and not other["hidden"]):
        pick, other = other, pick
    return pick, other


def picture(upload_dir: Path, m: Any, dst: Path) -> Path:
    rel = m["file_path"] if m["kind"] == "image" and m["ext"] in PHOTO_EXTS else m["thumb_path"]
    if not rel:
        raise ValueError(f"meme {m['id']} has no picture")
    with Image.open(safe_join(upload_dir, rel)) as im:
        im = im.convert("RGB")
        im.thumbnail((SEND_SIDE, SEND_SIDE))
        im.save(dst, "JPEG", quality=88)
    return dst


class Reviewer:
    def __init__(
        self,
        settings: Settings,
        pool: asyncpg.Pool,
        redis: Redis,
        codex: CodexPool,
        app_settings: SettingsCache,
        bump: Callable[[], Awaitable[None]],
    ) -> None:
        self.s = settings
        self.pool = pool
        self.redis = redis
        self.codex = codex
        self.app_settings = app_settings
        self.bump = bump

    async def run(self) -> None:
        if not await self.redis.set(REVIEW_LOCK, "1", nx=True, ex=900):
            return
        counts: Counter[str] = Counter()
        state = "done"
        try:
            await self.redis.delete(REVIEW_STOP)
            await self.pool.execute("UPDATE dedupe_candidates SET verdict = '' WHERE verdict = 'error'")
            total = await self.pool.fetchval("SELECT count(*) FROM dedupe_candidates WHERE verdict = ''")
            pipe = self.redis.pipeline()
            pipe.delete(REVIEW)
            pipe.hset(REVIEW, mapping={"state": "running", "total": total, "started_at": int(time.time())})
            await pipe.execute()
            while True:
                if await self.redis.exists(REVIEW_STOP):
                    state = "stopped"
                    break
                app = await self.app_settings.get()
                if not app.codex_enabled:
                    state = "paused: Codex is turned off in the analysis settings"
                    break
                await self.redis.expire(REVIEW_LOCK, 900)
                rows = await self.pool.fetch(
                    "SELECT a, b FROM dedupe_candidates WHERE verdict = '' ORDER BY sim DESC LIMIT $1", BATCH
                )
                if not rows:
                    break
                results = await asyncio.gather(*(self.check(r["a"], r["b"], app) for r in rows), return_exceptions=True)
                paused = ""
                for res in results:
                    if isinstance(res, CodexUnavailable):
                        paused = str(res)
                    elif isinstance(res, BaseException):
                        log.warning("duplicate review crashed: %s", res)
                        counts["errors"] += 1
                    else:
                        counts[res] += 1
                await self.redis.hset(REVIEW, mapping=dict(counts))
                if paused:
                    state = f"paused: {paused}"
                    break
        except asyncio.CancelledError:
            state = "stopped: the worker restarted"
            raise
        except Exception as exc:
            log.exception("duplicate review failed")
            state = f"failed: {exc}"[:300]
        finally:
            await self.redis.hset(REVIEW, mapping={"state": state, "finished_at": int(time.time()), **counts})
            await self.redis.delete(REVIEW_LOCK)
            if counts["merged"]:
                await self.bump()
            log.info("duplicate review %s: %s", state, dict(counts))

    async def verdict(self, a: int, b: int, verdict: str, reason: str) -> None:
        await self.pool.execute(
            "UPDATE dedupe_candidates SET verdict = $3, reason = $4, checked_at = now() WHERE a = $1 AND b = $2",
            a,
            b,
            verdict,
            reason[:500],
        )

    async def check(self, a: int, b: int, app: Any) -> str:
        memes = {r["id"]: r for r in await self.pool.fetch(MEME_SQL, [a, b])}
        if len(memes) < 2:
            await self.pool.execute("DELETE FROM dedupe_candidates WHERE a = $1 AND b = $2", a, b)
            return "gone"
        first, second = memes[a], memes[b]
        workdir = self.s.work_dir / f"review-{a}-{b}-{uuid.uuid4().hex[:8]}"
        workdir.mkdir(parents=True, exist_ok=True)
        try:
            images = await asyncio.to_thread(
                lambda: [
                    picture(self.s.upload_dir, first, workdir / "1.jpg"),
                    picture(self.s.upload_dir, second, workdir / "2.jpg"),
                ]
            )
            res = await self.codex.analyze(
                compare_prompt(dict(first), dict(second)),
                images,
                workdir,
                app.codex_model or (self.s.codex_model or ""),
                app.codex_effort,
                schema=COMPARE_SCHEMA,
                system=COMPARE_SYSTEM,
                parse=parse_compare,
            )
        except CodexUnavailable:
            raise
        except Exception as exc:
            await self.verdict(a, b, "error", str(exc))
            return "errors"
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        f = res.fields
        same = f["same"] and f["confidence"] >= MIN_CONFIDENCE
        keep, dup = choose(first, second, f["better"])
        note = f" · keeps #{keep['id']}" if same else ""
        await self.verdict(a, b, "same" if same else "different", f"{f['confidence']:.2f} · {f['reason']}{note}")
        if not same:
            return "different"
        if dup["locked"] or await self.redis.exists(f"ms:lock:meme:{dup['id']}"):
            return "same"
        if await merge(self.pool, self.s.upload_dir, keep["id"], dup["id"]):
            log.info("codex: meme %s is the same as %s, merged", dup["id"], keep["id"])
            return "merged"
        return "same"
