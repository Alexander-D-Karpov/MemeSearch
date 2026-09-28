from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import asyncpg
import httpx
from bs4 import BeautifulSoup, Tag
from redis.asyncio import Redis

from .config import Settings
from .media import content_hash, video_duration
from .rqueue import STREAM_LOW
from .storage import Staged, Storage, TooLarge, Unsupported

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
LOCK_TTL = 900
BG_URL = re.compile(r"background-image:\s*url\(['\"]?([^'\")]+)['\"]?\)")
POST_ID = re.compile(r"/(\d+)(?:\?|$)")

_EXTEND = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class ChannelUnavailable(Exception):
    pass


@dataclass
class MediaItem:
    post_id: int
    kind: str
    url: str
    duration: int | None = None


@dataclass
class Post:
    id: int
    text: str = ""
    date: datetime | None = None
    media: list[MediaItem] = field(default_factory=list)


@dataclass
class ChannelPage:
    title: str
    posts: list[Post]
    before: int | None


def parse_duration(s: str) -> int | None:
    parts = s.strip().split(":")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    total = 0
    for p in parts:
        total = total * 60 + int(p)
    return total


def _post_id(href: str | None, default: int) -> int:
    m = POST_ID.search(href or "")
    return int(m.group(1)) if m else default


def _media(node: Tag, post_id: int) -> list[MediaItem]:
    items: list[MediaItem] = []
    for el in node.select(
        "a.tgme_widget_message_photo_wrap, a.tgme_widget_message_video_player, .tgme_widget_message_roundvideo_player"
    ):
        classes = el.get("class") or []
        item_id = _post_id(el.get("href") or el.get("data-single-url"), post_id)
        if "tgme_widget_message_photo_wrap" in classes:
            m = BG_URL.search(el.get("style") or "")
            if m:
                items.append(MediaItem(item_id, "photo", m.group(1)))
            continue
        video = el.select_one("video[src]")
        dur = el.select_one("time.message_video_duration, time.tgme_widget_message_roundvideo_duration")
        duration = parse_duration(dur.get_text()) if dur else None
        if video is None:
            items.append(MediaItem(item_id, "too_big", "", duration))
            continue
        items.append(MediaItem(item_id, "video", video["src"], duration))
    return items


def parse_page(html: str, username: str) -> ChannelPage:
    soup = BeautifulSoup(html, "html.parser")
    og = soup.select_one('meta[property="og:title"]')
    title = (og.get("content") or "").strip() if og else ""
    posts: list[Post] = []
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        chan, _, pid = (msg.get("data-post") or "").partition("/")
        if chan.lower() != username.lower() or not pid.isdigit():
            continue
        post = Post(int(pid))
        text = msg.select_one(".tgme_widget_message_text")
        if text:
            for br in text.find_all("br"):
                br.replace_with("\n")
            post.text = text.get_text().strip()
        ts = msg.select_one(".tgme_widget_message_date time[datetime], time[datetime]")
        if ts:
            try:
                post.date = datetime.fromisoformat(ts["datetime"])
            except ValueError:
                pass
        post.media = _media(msg, post.id)
        posts.append(post)
    more = soup.select_one("a.tme_messages_more[data-before]")
    before = int(more["data-before"]) if more and str(more["data-before"]).isdigit() else None
    if before is None and posts:
        before = min(p.id for p in posts)
        if before <= 1:
            before = None
    return ChannelPage(title, posts, before)


def has_preview(html: str) -> bool:
    return "tgme_channel_info" in html or "tgme_widget_message" in html


@dataclass
class Counters:
    added: int = 0
    duplicates: int = 0
    skipped: int = 0
    failed: int = 0


class ChannelImporter:
    def __init__(self, s: Settings, pool: asyncpg.Pool, storage: Storage, redis: Redis) -> None:
        self.s = s
        self.pool = pool
        self.storage = storage
        self.redis = redis
        self.max_bytes = s.channel_max_file_mb << 20
        self.base = s.telegram_web_url.rstrip("/")
        self._extend = redis.register_script(_EXTEND)
        self._release = redis.register_script(_RELEASE)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            proxy=self.s.web_proxy,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
            timeout=httpx.Timeout(60, connect=15),
            follow_redirects=True,
        )

    async def _heartbeat(self, key: str, token: str) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await self._extend(keys=[key], args=[token, LOCK_TTL])
            except Exception as exc:
                log.warning("channel lock heartbeat failed: %s", exc)

    async def run(self, channel_id: int, queued_at: float = 0) -> None:
        key = f"ms:lock:channel:{channel_id}"
        token = uuid.uuid4().hex
        if not await self.redis.set(key, token, nx=True, ex=LOCK_TTL):
            return
        beat = asyncio.create_task(self._heartbeat(key, token))
        try:
            ch = await self.pool.fetchrow("SELECT * FROM channels WHERE id=$1", channel_id)
            if ch is None:
                return
            if ch["last_polled_at"] and queued_at and ch["last_polled_at"].timestamp() > queued_at:
                return
            await self._poll(dict(ch))
        finally:
            beat.cancel()
            await self._release(keys=[key], args=[token])

    async def _poll(self, ch: dict) -> None:
        cid = ch["id"]
        await self.pool.execute("UPDATE channels SET status='running', error='' WHERE id=$1", cid)
        c = Counters()
        try:
            async with self._client() as client:
                posts = await self._collect(client, ch)
                for post in posts:
                    for item in post.media:
                        await self._item(client, ch, post, item, c)
                    await self._progress(cid, c, post.id)
            await self.pool.execute(
                """UPDATE channels SET status='idle', last_polled_at=now(),
                next_poll_at=now() + make_interval(mins => $2) WHERE id=$1""",
                cid,
                self.s.channel_poll_minutes,
            )
            log.info("channel @%s: +%d, %d duplicates, %d skipped", ch["username"], c.added, c.duplicates, c.skipped)
        except Exception as exc:
            log.warning("channel @%s failed: %s", ch["username"], exc)
            await self.pool.execute(
                """UPDATE channels SET status='failed', error=$2, last_polled_at=now(),
                next_poll_at=now() + make_interval(mins => $3) WHERE id=$1""",
                cid,
                (str(exc) or type(exc).__name__)[:2000],
                self.s.channel_poll_minutes,
            )

    async def _progress(self, cid: int, c: Counters, last_post: int) -> None:
        await self.pool.execute(
            """UPDATE channels SET added=added+$2, duplicates=duplicates+$3, skipped=skipped+$4, failed=failed+$5,
            last_post_id=GREATEST(last_post_id, $6) WHERE id=$1""",
            cid,
            c.added,
            c.duplicates,
            c.skipped,
            c.failed,
            last_post,
        )
        c.added = c.duplicates = c.skipped = c.failed = 0

    async def _fetch(self, client: httpx.AsyncClient, url: str, params: dict | None = None) -> str:
        for attempt in range(4):
            try:
                r = await client.get(url, params=params)
            except httpx.TransportError as exc:
                if attempt == 2:
                    raise ChannelUnavailable(
                        f"cannot reach {self.base} ({type(exc).__name__}); set TELEGRAM_WEB_PROXY in .env and recreate the worker"
                    ) from exc
                await asyncio.sleep(5 * (attempt + 1))
                continue
            if r.status_code == 429:
                await asyncio.sleep(float(r.headers.get("Retry-After") or 10 * (attempt + 1)))
                continue
            r.raise_for_status()
            return r.text
        raise RuntimeError("t.me keeps rate limiting, try later")

    async def _collect(self, client: httpx.AsyncClient, ch: dict) -> list[Post]:
        username = ch["username"]
        last = ch["last_post_id"]
        limit = ch["backfill_limit"] if last == 0 else 0
        found: dict[int, Post] = {}
        before: int | None = None
        first = True
        while True:
            html = await self._fetch(client, f"{self.base}/s/{username}", {"before": before} if before else None)
            page = parse_page(html, username)
            if first:
                if not page.posts and not has_preview(html):
                    raise ChannelUnavailable("channel is private, does not exist or has web preview disabled")
                if page.title and page.title != ch["title"]:
                    await self.pool.execute("UPDATE channels SET title=$2 WHERE id=$1", ch["id"], page.title[:300])
                first = False
            fresh = [p for p in page.posts if p.id > last]
            for p in fresh:
                found[p.id] = p
            if not page.posts or len(fresh) < len(page.posts) or page.before is None:
                break
            if limit and len(found) >= limit:
                break
            if before is not None and page.before >= before:
                break
            before = page.before
            await asyncio.sleep(self.s.channel_page_delay)
        posts = sorted(found.values(), key=lambda p: p.id)
        if limit:
            posts = posts[-limit:]
        return posts

    async def _item(self, client: httpx.AsyncClient, ch: dict, post: Post, item: MediaItem, c: Counters) -> None:
        url = f"https://t.me/{ch['username']}/{item.post_id}"
        if await self.pool.fetchval("SELECT 1 FROM meme_sources WHERE url=$1", url):
            return
        max_seconds = self.s.channel_max_video_seconds
        if item.kind == "too_big" or (item.duration is not None and max_seconds and item.duration > max_seconds):
            c.skipped += 1
            return
        staged: Staged | None = None
        try:
            staged = await self._download(client, ch["username"], item)
            existing = await self.pool.fetchval("SELECT id FROM memes WHERE sha256=$1", staged.sha)
            if existing:
                await self._link(existing, url, ch["id"], post)
                c.duplicates += 1
                return
            workdir = self.s.work_dir / f"ch-{uuid.uuid4().hex}"
            try:
                phash = await asyncio.to_thread(self._hash, staged, workdir)
            finally:
                await asyncio.to_thread(shutil.rmtree, workdir, True)
            if phash is False:
                c.skipped += 1
                return
            if phash:
                near = await self.pool.fetchval(
                    """SELECT id FROM memes WHERE phash IS NOT NULL AND (kind = 'video') = $2
                    AND bit_count(phash # $1::text::bit(256)) <= $3
                    ORDER BY bit_count(phash # $1::text::bit(256)), id LIMIT 1""",
                    phash,
                    staged.media.kind == "video",
                    self.s.channel_dedup_distance,
                )
                if near:
                    await self._link(near, url, ch["id"], post)
                    c.duplicates += 1
                    return
            res = await self.storage.commit(
                staged,
                name=f"{ch['username']}_{item.post_id}.{staged.media.ext}",
                source="telegram",
                source_ref=url,
                caption=post.text,
                stream=STREAM_LOW,
                phash=phash or None,
            )
            staged = None
            await self._link(res.meme_id, url, ch["id"], post)
            if res.duplicate:
                c.duplicates += 1
            else:
                c.added += 1
        except (Unsupported, TooLarge) as exc:
            log.info("skip %s: %s", url, exc)
            c.skipped += 1
        except Exception as exc:
            log.warning("channel item %s failed: %s", url, exc)
            c.failed += 1
        finally:
            if staged is not None:
                staged.tmp.unlink(missing_ok=True)

    def _hash(self, staged: Staged, workdir: Path) -> str | bool | None:
        try:
            if staged.media.kind == "video":
                seconds = video_duration(staged.tmp)
                if self.s.channel_max_video_seconds and seconds > self.s.channel_max_video_seconds:
                    return False
            return content_hash(
                staged.media.kind,
                staged.tmp,
                workdir,
                max_video_frames=self.s.max_video_frames,
                frame_max_side=self.s.frame_max_side,
                thumb_size=self.s.thumb_size,
            )
        except Exception as exc:
            log.info("no content hash for %s: %s", staged.tmp.name, exc)
            return None

    async def _download(self, client: httpx.AsyncClient, username: str, item: MediaItem) -> Staged:
        try:
            return await self._download_url(client, item.url)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in (403, 404, 410):
                raise
        html = await self._fetch(client, f"{self.base}/{username}/{item.post_id}", {"embed": "1", "mode": "tme", "single": "1"})
        for post in parse_page(html, username).posts:
            for fresh in post.media:
                if fresh.post_id == item.post_id and fresh.url:
                    return await self._download_url(client, fresh.url)
        raise RuntimeError("media link expired and could not be refreshed")

    async def _download_url(self, client: httpx.AsyncClient, url: str) -> Staged:
        self.storage.tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="dl-", dir=self.storage.tmp_dir)
        tmp = Path(name)
        try:
            with os.fdopen(fd, "wb") as out:
                async with client.stream("GET", url) as r:
                    r.raise_for_status()
                    size = 0
                    async for chunk in r.aiter_bytes(1 << 20):
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise TooLarge(f"file is larger than {self.max_bytes >> 20} MB")
                        out.write(chunk)

            def stage() -> Staged:
                with open(tmp, "rb") as f:
                    return self.storage.stage(f)

            return await asyncio.to_thread(stage)
        finally:
            tmp.unlink(missing_ok=True)

    async def _link(self, meme_id: int, url: str, channel_id: int, post: Post) -> None:
        await self.pool.execute(
            """INSERT INTO meme_sources (meme_id, url, source, channel_id, post_id, caption, posted_at)
            VALUES ($1, $2, 'telegram', $3, $4, $5, $6) ON CONFLICT (url) DO NOTHING""",
            meme_id,
            url,
            channel_id,
            post.id,
            post.text[:4000],
            post.date,
        )


async def claim_due(pool: asyncpg.Pool, minutes: int) -> list[int]:
    rows = await pool.fetch(
        """UPDATE channels SET next_poll_at = now() + make_interval(mins => $1)
        WHERE id IN (SELECT id FROM channels WHERE enabled AND next_poll_at <= now() ORDER BY next_poll_at LIMIT 20)
        RETURNING id""",
        minutes,
    )
    return [r["id"] for r in rows]
