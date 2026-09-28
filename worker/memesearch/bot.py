from __future__ import annotations

import asyncio
import html
import json
import logging
import tempfile
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    FSInputFile,
    InlineQuery,
    InlineQueryResultCachedGif,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedVideo,
    InlineQueryResultGif,
    InlineQueryResultPhoto,
    InlineQueryResultVideo,
    Message,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
)
from redis.asyncio import Redis

from .config import get_settings
from .db import connect
from .rqueue import EVENTS, STREAM_HIGH, JobQueue
from .storage import Storage, TooLarge, Unsupported, safe_join

log = logging.getLogger("memesearch.bot")
settings = get_settings()
router = Router()

NOTIFY_TTL = 6 * 3600
INLINE_PAGE = 30
PHOTO_URL_MAX = 5 << 20
FILE_URL_MAX = 20 << 20
PHOTO_UPLOAD_MAX = 10 << 20
UPLOAD_MAX = 50 << 20
PHOTO_EXTS = {"jpg", "jpeg", "png", "webp"}
CACHE_PRIO = "ms:tg:cache:prio"


class Ctx:
    storage: Storage
    queue: JobQueue
    redis: Redis
    pool: object
    http: httpx.AsyncClient


ctx = Ctx()


def is_admin(message: Message) -> bool:
    return message.from_user is not None and message.from_user.id in settings.admin_ids


def meme_url(meme_id: int) -> str:
    return f"{settings.public_url.rstrip('/')}/m/{meme_id}"


def origin_of(message: Message) -> str:
    o = message.forward_origin
    if isinstance(o, MessageOriginChannel):
        if o.chat.username:
            return f"https://t.me/{o.chat.username}/{o.message_id}"
        return f"channel:{o.chat.title}"
    if isinstance(o, MessageOriginChat):
        return f"chat:{o.sender_chat.title}"
    if isinstance(o, MessageOriginUser):
        return f"user:{o.sender_user.full_name}"
    if isinstance(o, MessageOriginHiddenUser):
        return f"user:{o.sender_user_name}"
    return ""


def pick_file(message: Message) -> tuple[str, int, str] | None:
    if message.photo:
        p = message.photo[-1]
        return p.file_id, p.file_size or 0, f"photo_{p.file_unique_id}.jpg"
    if message.animation:
        a = message.animation
        return a.file_id, a.file_size or 0, a.file_name or f"animation_{a.file_unique_id}.mp4"
    if message.video:
        v = message.video
        return v.file_id, v.file_size or 0, v.file_name or f"video_{v.file_unique_id}.mp4"
    if message.video_note:
        v = message.video_note
        return v.file_id, v.file_size or 0, f"videonote_{v.file_unique_id}.mp4"
    if message.sticker and not message.sticker.is_animated:
        s = message.sticker
        return s.file_id, s.file_size or 0, f"sticker_{s.file_unique_id}.{'webm' if s.is_video else 'webp'}"
    if message.document:
        d = message.document
        mime = d.mime_type or ""
        if mime.startswith(("image/", "video/")) or (d.file_name or "").lower().endswith(
            (".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".mov", ".webm", ".mkv", ".heic", ".avif")
        ):
            return d.file_id, d.file_size or 0, d.file_name or f"doc_{d.file_unique_id}"
    return None


@router.message(Command("start", "help"))
async def cmd_help(message: Message) -> None:
    if not is_admin(message):
        await message.answer(f"Your id is <code>{message.from_user.id}</code>. This bot is private.")
        return
    me = await message.bot.me()
    await message.answer(
        "Forward or send memes (photos, GIFs, videos, image/video files) and they will be added and analyzed.\n"
        "The caption is stored as extra context.\n\n"
        "/search &lt;query&gt; — search\n/stats — counts\n/reprocess &lt;id&gt; — analyze again\n"
        f"In any chat type @{html.escape(me.username or 'this_bot')} and a query to send a meme.\n"
        f"Web: {html.escape(settings.public_url)}"
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not is_admin(message):
        return
    rows = await ctx.pool.fetch("SELECT status, count(*) AS n FROM memes GROUP BY status ORDER BY status")
    total = sum(r["n"] for r in rows)
    lines = [f"Total: <b>{total}</b>"] + [f"{r['status']}: {r['n']}" for r in rows]
    await message.answer("\n".join(lines))


@router.message(Command("search"))
async def cmd_search(message: Message, command: CommandObject) -> None:
    if not is_admin(message):
        return
    q = (command.args or "").strip()
    if not q:
        await message.answer("Usage: /search cat with a knife")
        return
    try:
        resp = await ctx.http.get("/api/v1/search", params={"q": q, "limit": 8})
        resp.raise_for_status()
        memes = resp.json().get("memes", [])
    except httpx.HTTPError as exc:
        await message.answer(f"Search failed: {html.escape(str(exc))}")
        return
    if not memes:
        await message.answer("Nothing found.")
        return
    lines = [f'<a href="{meme_url(m["id"])}">#{m["id"]}</a> {html.escape(m["title"] or "")}' for m in memes]
    await message.answer("\n".join(lines), disable_web_page_preview=True)


def cached_result(m: dict, file_id: str, file_type: str):
    rid = str(m["id"])
    title = (m.get("title") or "").strip() or f"Meme #{m['id']}"
    desc = ((m.get("text") or m.get("description") or "").strip().replace("\n", " "))[:200]
    if file_type == "photo":
        return InlineQueryResultCachedPhoto(id=rid, photo_file_id=file_id, title=title, description=desc)
    if file_type == "gif":
        return InlineQueryResultCachedGif(id=rid, gif_file_id=file_id, title=title)
    if file_type == "video":
        return InlineQueryResultCachedVideo(id=rid, video_file_id=file_id, title=title, description=desc)
    return None


def inline_result(m: dict, file_id: str = "", file_type: str = ""):
    if file_id:
        cached = cached_result(m, file_id, file_type)
        if cached is not None:
            return cached
    base = settings.public_url.rstrip("/")
    page = f"{base}/m/{m['id']}"

    def absolute(u: str) -> str:
        return base + u if u.startswith("/") else u

    url = absolute(m.get("url") or "")
    thumb = f"{page}/og.jpg" if m.get("thumb_url") and m.get("thumb_url") != m.get("url") else ""
    title = (m.get("title") or "").strip() or f"Meme #{m['id']}"
    desc = ((m.get("text") or m.get("description") or "").strip().replace("\n", " "))[:200]
    size = m.get("size_bytes") or 0
    w, h = m.get("width") or None, m.get("height") or None
    seconds = max(1, round((m.get("duration_ms") or 0) / 1000)) if m.get("duration_ms") else None
    rid = str(m["id"])
    if m["kind"] == "image":
        direct = m.get("ext") in ("jpg", "jpeg") and 0 < size <= PHOTO_URL_MAX
        photo = url if direct else f"{page}/photo.jpg"
        return InlineQueryResultPhoto(
            id=rid,
            photo_url=photo,
            thumbnail_url=thumb or photo,
            photo_width=w,
            photo_height=h,
            title=title,
            description=desc,
        )
    if m["kind"] == "gif" and 0 < size <= FILE_URL_MAX and thumb:
        return InlineQueryResultGif(
            id=rid,
            gif_url=url,
            gif_width=w,
            gif_height=h,
            gif_duration=seconds,
            thumbnail_url=thumb,
            thumbnail_mime_type="image/jpeg",
            title=title,
        )
    if m["kind"] == "video" and m.get("mime") == "video/mp4" and 0 < size <= FILE_URL_MAX and thumb:
        return InlineQueryResultVideo(
            id=rid,
            video_url=url,
            mime_type="video/mp4",
            thumbnail_url=thumb,
            title=title,
            description=desc,
            video_width=w,
            video_height=h,
            video_duration=seconds,
        )
    return None


async def file_cache(ids: list[int]) -> dict[int, tuple[str, str]]:
    if not ids:
        return {}
    rows = await ctx.pool.fetch("SELECT id, tg_file_id, tg_file_type FROM memes WHERE id = ANY($1) AND tg_file_id <> ''", ids)
    return {r["id"]: (r["tg_file_id"], r["tg_file_type"]) for r in rows}


async def upload(bot: Bot, chat_id: int, m: dict) -> tuple[str, str]:
    src = safe_join(settings.upload_dir, m["file_path"])
    thumb = safe_join(settings.upload_dir, m["thumb_path"]) if m["thumb_path"] else None
    limit = 2000 << 20 if settings.telegram_api_local else UPLOAD_MAX
    common = {"disable_notification": True}
    if m["kind"] == "image":
        path = src if m["ext"] in PHOTO_EXTS and m["size_bytes"] <= PHOTO_UPLOAD_MAX else thumb
        if path is None:
            raise ValueError("no photo-compatible file")
        msg = await bot.send_photo(chat_id, FSInputFile(path), **common)
        file_id, file_type = msg.photo[-1].file_id, "photo"
    elif m["size_bytes"] > limit:
        raise ValueError(f"file is larger than {limit >> 20} MB")
    elif m["kind"] == "gif":
        msg = await bot.send_animation(chat_id, FSInputFile(src), **common)
        file_id, file_type = (msg.animation or msg.document).file_id, "gif"
    else:
        msg = await bot.send_video(
            chat_id,
            FSInputFile(src),
            width=m["width"] or None,
            height=m["height"] or None,
            duration=round(m["duration_ms"] / 1000) or None,
            thumbnail=FSInputFile(thumb) if thumb else None,
            supports_streaming=True,
            **common,
        )
        if msg.video is not None:
            file_id, file_type = msg.video.file_id, "video"
        elif msg.animation is not None:
            file_id, file_type = msg.animation.file_id, "gif"
        else:
            raise ValueError("telegram did not accept the file as a video")
    try:
        await bot.delete_message(chat_id, msg.message_id)
    except TelegramAPIError:
        pass
    return file_id, file_type


async def cache_one(bot: Bot, chat_id: int, m: dict) -> None:
    for _ in range(3):
        try:
            file_id, file_type = await upload(bot, chat_id, m)
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
            continue
        except (TelegramForbiddenError, TelegramNetworkError):
            raise
        except (TelegramAPIError, ValueError, OSError) as exc:
            if "chat not found" in str(exc).lower():
                raise CacheChatUnavailable(str(exc)) from exc
            log.info("cannot cache meme %s in telegram: %s", m["id"], exc)
            await ctx.pool.execute("UPDATE memes SET tg_cache_error=$2 WHERE id=$1", m["id"], str(exc)[:500] or "error")
            return
        await ctx.pool.execute(
            "UPDATE memes SET tg_file_id=$2, tg_file_type=$3, tg_cache_error='' WHERE id=$1", m["id"], file_id, file_type
        )
        return


class CacheChatUnavailable(Exception):
    pass


CACHE_COLS = "id, kind, ext, file_path, thumb_path, size_bytes, width, height, duration_ms"
CACHE_WHERE = "tg_file_id = '' AND tg_cache_error = '' AND status = 'done' AND NOT hidden"


async def cacher(bot: Bot) -> None:
    chat_id = settings.cache_chat_id
    if not chat_id:
        log.warning("no TELEGRAM_CACHE_CHAT_ID or admin id: inline results use media links only")
        return
    while True:
        try:
            prio = [int(i) for i in await ctx.redis.spop(CACHE_PRIO, 30) or []]
            rows = []
            if prio:
                rows = await ctx.pool.fetch(f"SELECT {CACHE_COLS} FROM memes WHERE id = ANY($1) AND {CACHE_WHERE}", prio)
            if not rows:
                rows = await ctx.pool.fetch(f"SELECT {CACHE_COLS} FROM memes WHERE {CACHE_WHERE} ORDER BY id DESC LIMIT 30")
            if not rows:
                await asyncio.sleep(30)
                continue
            for m in rows:
                await cache_one(bot, chat_id, dict(m))
                await asyncio.sleep(settings.telegram_cache_interval)
        except asyncio.CancelledError:
            raise
        except (TelegramForbiddenError, CacheChatUnavailable) as exc:
            log.warning("cannot post to cache chat %s (%s); send /start to the bot or add it to the channel", chat_id, exc)
            await asyncio.sleep(300)
        except Exception as exc:
            log.warning("telegram cache: %s", exc)
            await asyncio.sleep(15)


@router.inline_query()
async def inline_search(query: InlineQuery) -> None:
    if not settings.telegram_inline_public and query.from_user.id not in settings.admin_ids:
        await query.answer([], cache_time=300, is_personal=True)
        return
    offset = int(query.offset) if query.offset.isdigit() else 0
    q = query.query.strip()
    params: dict = {"limit": INLINE_PAGE, "offset": offset}
    if q:
        params["q"] = q
    try:
        resp = await ctx.http.get("/api/v1/search", params=params)
        resp.raise_for_status()
        memes = resp.json().get("memes") or []
    except httpx.HTTPError as exc:
        log.warning("inline search %r failed: %s", q, exc)
        await query.answer([], cache_time=5, is_personal=True)
        return
    cache = await file_cache([m["id"] for m in memes])
    missing = [m["id"] for m in memes if m["id"] not in cache]
    if missing:
        await ctx.redis.sadd(CACHE_PRIO, *missing)
    results = [r for m in memes if (r := inline_result(m, *cache.get(m["id"], ("", "")))) is not None]
    await query.answer(
        results,
        cache_time=30 if q else 60,
        is_personal=False,
        next_offset=str(offset + INLINE_PAGE) if len(memes) == INLINE_PAGE else "",
    )


@router.message(Command("reprocess"))
async def cmd_reprocess(message: Message, command: CommandObject) -> None:
    if not is_admin(message):
        return
    try:
        meme_id = int((command.args or "").strip().lstrip("#"))
    except ValueError:
        await message.answer("Usage: /reprocess 123")
        return
    n = await ctx.pool.execute("UPDATE memes SET status='pending', error='', attempts=0, updated_at=now() WHERE id=$1", meme_id)
    if n.endswith(" 0"):
        await message.answer("Not found.")
        return
    await ctx.queue.process(meme_id, STREAM_HIGH, analyze=True)
    reply = await message.answer(f"#{meme_id} queued for analysis…")
    await remember(meme_id, reply)


async def remember(meme_id: int, reply: Message) -> None:
    await ctx.redis.set(f"ms:bot:notify:{meme_id}", json.dumps({"chat": reply.chat.id, "msg": reply.message_id}), ex=NOTIFY_TTL)


@router.message(F.photo | F.animation | F.video | F.video_note | F.document | F.sticker)
async def on_media(message: Message, bot: Bot) -> None:
    if not is_admin(message):
        return
    picked = pick_file(message)
    if picked is None:
        await message.reply("Unsupported file. Send an image, GIF or video.")
        return
    file_id, size, name = picked
    limit = settings.bot_max_file_mb << 20
    if size and size > limit:
        await message.reply(
            f"File is {size >> 20} MB; the bot can download up to {settings.bot_max_file_mb} MB. Use the web upload."
        )
        return
    tmp_dir = settings.upload_dir / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=tmp_dir) as d:
        path = Path(d) / "download"
        try:
            await bot.download(file_id, destination=path)
        except Exception as exc:
            await message.reply(f"Download failed: {html.escape(str(exc))}")
            return
        try:
            res = await ctx.storage.ingest_path(
                path,
                name=name,
                source="telegram",
                source_ref=origin_of(message),
                caption=message.caption or "",
            )
        except (Unsupported, TooLarge) as exc:
            await message.reply(html.escape(str(exc)))
            return
    if res.duplicate:
        await message.reply(f"Already in the collection: {meme_url(res.meme_id)}", disable_web_page_preview=True)
        return
    reply = await message.reply(
        f'Added <a href="{meme_url(res.meme_id)}">#{res.meme_id}</a>, analyzing…', disable_web_page_preview=True
    )
    await remember(res.meme_id, reply)


async def notifier(bot: Bot) -> None:
    while True:
        pubsub = ctx.redis.pubsub()
        try:
            await pubsub.subscribe(EVENTS)
            async for item in pubsub.listen():
                if item.get("type") != "message":
                    continue
                try:
                    ev = json.loads(item["data"])
                except (TypeError, ValueError):
                    continue
                if ev.get("type") != "meme" or ev.get("status") not in ("done", "failed"):
                    continue
                key = f"ms:bot:notify:{ev['id']}"
                raw = await ctx.redis.getdel(key)
                if not raw:
                    continue
                target = json.loads(raw)
                meme = await ctx.pool.fetchrow("SELECT title, ocr_text, tags, error FROM memes WHERE id=$1", ev["id"])
                if meme is None:
                    continue
                if ev["status"] == "done":
                    text = f'✅ <a href="{meme_url(ev["id"])}">#{ev["id"]}</a> <b>{html.escape(meme["title"] or "")}</b>'
                    if meme["ocr_text"]:
                        text += f"\n<i>{html.escape(meme['ocr_text'][:300])}</i>"
                    if meme["tags"]:
                        text += "\n" + " ".join(f"#{html.escape(t.replace(' ', '_'))}" for t in meme["tags"][:8])
                else:
                    text = f"❌ #{ev['id']} failed: {html.escape((meme['error'] or '')[:300])}"
                try:
                    await bot.edit_message_text(
                        text, chat_id=target["chat"], message_id=target["msg"], disable_web_page_preview=True
                    )
                except Exception as exc:
                    log.debug("edit failed: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("notifier reconnecting: %s", exc)
            await asyncio.sleep(3)
        finally:
            await pubsub.aclose()


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not settings.telegram_bot_token:
        log.warning("TELEGRAM_BOT_TOKEN is empty, bot disabled")
        await asyncio.Event().wait()
    if not settings.admin_ids:
        log.warning("TELEGRAM_ADMIN_IDS is empty: nobody can add memes (send /start to learn your id)")
    ctx.pool = await connect(settings.database_url, max_size=4)
    ctx.redis = Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=30, socket_connect_timeout=10)
    ctx.queue = JobQueue(ctx.redis)
    ctx.storage = Storage(settings.upload_dir, ctx.pool, ctx.queue, settings.max_file_mb << 20)
    ctx.http = httpx.AsyncClient(base_url=settings.web_url, timeout=20)

    session_kwargs = {}
    if settings.telegram_proxy:
        session_kwargs["proxy"] = settings.telegram_proxy
    if settings.telegram_api_url:
        session_kwargs["api"] = TelegramAPIServer.from_base(settings.telegram_api_url, is_local=settings.telegram_api_local)
    bot = Bot(
        settings.telegram_bot_token,
        session=AiohttpSession(**session_kwargs),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)
    notify = asyncio.create_task(notifier(bot))
    uploader = asyncio.create_task(cacher(bot))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        notify.cancel()
        uploader.cancel()
        await ctx.http.aclose()
        await ctx.pool.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
