from aiogram.types import (
    InlineQueryResultCachedGif,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedVideo,
    InlineQueryResultGif,
    InlineQueryResultPhoto,
    InlineQueryResultVideo,
)

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
