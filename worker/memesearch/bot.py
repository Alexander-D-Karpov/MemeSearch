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
from aiogram.filters import Command, CommandObject
from aiogram.types import (
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
from .storage import Storage, TooLarge, Unsupported

log = logging.getLogger("memesearch.bot")
settings = get_settings()
router = Router()

NOTIFY_TTL = 6 * 3600


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
    await message.answer(
        "Forward or send memes (photos, GIFs, videos, image/video files) and they will be added and analyzed.\n"
        "The caption is stored as extra context.\n\n"
        "/search &lt;query&gt; — search\n/stats — counts\n/reprocess &lt;id&gt; — analyze again\n"
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
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        notify.cancel()
        await ctx.http.aclose()
        await ctx.pool.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
