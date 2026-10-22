import asyncio

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import (
    InlineQueryResultCachedGif,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedVideo,
    InlineQueryResultGif,
    InlineQueryResultPhoto,
    InlineQueryResultVideo,
    Message,
)

from memesearch import bot
from memesearch.bot import inline_result


def meme(**kw):
    base = {
        "id": 5,
        "kind": "image",
        "ext": "jpg",
        "mime": "image/jpeg",
        "url": "/media/originals/aa/bb/x.jpg",
        "thumb_url": "/media/thumbs/aa/bb/x.webp",
        "size_bytes": 1000,
        "width": 800,
        "height": 600,
        "title": "Cat",
        "text": "hello\nworld",
    }
    return {**base, **kw}


def test_jpeg_uses_original():
    r = inline_result(meme())
    assert isinstance(r, InlineQueryResultPhoto)
    assert r.photo_url == "https://ms.example/media/originals/aa/bb/x.jpg"
    assert r.thumbnail_url == "https://ms.example/m/5/og.jpg"
    assert r.description == "hello world"


def test_png_and_big_jpeg_use_converted_photo():
    assert inline_result(meme(ext="png", mime="image/png")).photo_url == "https://ms.example/m/5/photo.jpg"
    assert inline_result(meme(size_bytes=9 << 20)).photo_url == "https://ms.example/m/5/photo.jpg"


def test_gif_and_video():
    g = inline_result(meme(kind="gif", ext="gif", mime="image/gif", duration_ms=2400))
    assert isinstance(g, InlineQueryResultGif) and g.gif_duration == 2 and g.thumbnail_mime_type == "image/jpeg"
    v = inline_result(meme(kind="video", ext="mp4", mime="video/mp4", duration_ms=15000, url="https://cdn.example/v.mp4"))
    assert isinstance(v, InlineQueryResultVideo) and v.video_url == "https://cdn.example/v.mp4" and v.video_duration == 15


def test_unsendable_videos_are_skipped():
    assert inline_result(meme(kind="video", ext="webm", mime="video/webm")) is None
    assert inline_result(meme(kind="video", ext="mp4", mime="video/mp4", size_bytes=50 << 20)) is None
    assert inline_result(meme(kind="video", ext="mp4", mime="video/mp4", thumb_url="")) is None


def test_cached_file_ids_win_over_links():
    assert isinstance(inline_result(meme(ext="png"), "AgAC", "photo"), InlineQueryResultCachedPhoto)
    assert isinstance(inline_result(meme(kind="gif"), "CgAC", "gif"), InlineQueryResultCachedGif)
    v = inline_result(meme(kind="video", mime="video/webm", size_bytes=40 << 20), "BAAC", "video")
    assert isinstance(v, InlineQueryResultCachedVideo) and v.video_file_id == "BAAC"


def test_every_result_links_to_the_meme_page():
    results = [
        inline_result(meme()),
        inline_result(meme(kind="gif", ext="gif", mime="image/gif")),
        inline_result(meme(kind="video", ext="mp4", mime="video/mp4")),
        inline_result(meme(), "AgAC", "photo"),
        inline_result(meme(kind="video"), "BAAC", "video"),
    ]
    for r in results:
        button = r.reply_markup.inline_keyboard[0][0]
        assert button.url == "https://ms.example/m/5" and button.text == "ms.example/m/5"


class CachePool:
    def __init__(self):
        self.calls = []

    async def execute(self, sql, *args):
        self.calls.append((sql.split()[0], args))


class FlakyBot:
    def __init__(self, reachable):
        self.reachable = reachable

    async def get_me(self):
        if not self.reachable:
            raise TelegramNetworkError(method=None, message="down")
        return object()


def test_network_error_on_one_file_marks_it_failed(monkeypatch):
    pool = CachePool()
    monkeypatch.setattr(bot.ctx, "pool", pool, raising=False)

    async def boom(*a):
        raise TelegramNetworkError(method=None, message="Request timeout error")

    monkeypatch.setattr(bot, "upload", boom)
    asyncio.run(bot.cache_one(FlakyBot(True), 1, {"id": 7}))
    assert pool.calls and pool.calls[0][1][0] == 7
    assert "Request timeout error" in pool.calls[0][1][1]


def test_network_outage_does_not_mark_memes(monkeypatch):
    pool = CachePool()
    monkeypatch.setattr(bot.ctx, "pool", pool, raising=False)

    async def boom(*a):
        raise TelegramNetworkError(method=None, message="Request timeout error")

    monkeypatch.setattr(bot, "upload", boom)
    with pytest.raises(TelegramNetworkError):
        asyncio.run(bot.cache_one(FlakyBot(False), 1, {"id": 7}))
    assert pool.calls == []


def message_from(user_id, chat_id, chat_type):
    return Message.model_validate(
        {
            "message_id": 1,
            "date": 0,
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": user_id, "is_bot": False, "first_name": "x"},
        }
    )


def test_only_admins_in_private_chat_count_as_admin(monkeypatch):
    monkeypatch.setattr(bot.settings, "telegram_admin_ids", "42")
    assert bot.is_admin(message_from(42, 42, "private"))
    assert not bot.is_admin(message_from(42, -100123, "supergroup"))
    assert not bot.is_admin(message_from(42, -100456, "group"))
    assert not bot.is_admin(message_from(7, 7, "private"))


def test_upload_timeout_grows_with_file_size(monkeypatch, tmp_path):
    seen = {}

    class Sent:
        message_id = 1
        video = type("V", (), {"file_id": "vid"})()
        animation = None

    class FakeBot:
        async def send_video(self, chat_id, file, **kw):
            seen.update(kw)
            return Sent()

        async def delete_message(self, *a):
            pass

    monkeypatch.setattr(bot.settings, "upload_dir", tmp_path)
    (tmp_path / "v.mp4").write_bytes(b"x")
    m = {"kind": "video", "file_path": "v.mp4", "thumb_path": "", "size_bytes": 14 << 20, "ext": "mp4"}
    m |= {"width": 0, "height": 0, "duration_ms": 0}
    assert asyncio.run(bot.upload(FakeBot(), 1, m)) == ("vid", "video")
    assert seen["request_timeout"] >= 280
    m["size_bytes"] = 1 << 20
    asyncio.run(bot.upload(FakeBot(), 1, m))
    assert seen["request_timeout"] == bot.settings.telegram_upload_timeout
