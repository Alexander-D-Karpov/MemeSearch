import asyncio
import io
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from PIL import Image, ImageDraw

from memesearch.channels import ChannelImporter, has_preview, parse_duration, parse_page
from memesearch.config import Settings
from memesearch.media import content_hash, dhash, hamming, poster_time, thumb_hash, video_times
from memesearch.storage import Storage

PAGE = """
<html><head><meta property="og:title" content="Best Memes"></head><body>
<div class="tgme_channel_info"></div>
<div class="tgme_widget_message_wrap js-widget_message_wrap">
 <div class="tgme_widget_message js-widget_message" data-post="bestmemes/101">
  <a class="tgme_widget_message_photo_wrap 1 blured js-message_photo"
     style="width:800px;background-image:url('https://cdn4.telesco.pe/file/a.jpg')" href="https://t.me/bestmemes/101"></a>
  <div class="tgme_widget_message_text js-message_text" dir="auto">first line<br>second line</div>
  <div class="tgme_widget_message_footer"><a class="tgme_widget_message_date" href="https://t.me/bestmemes/101">
   <time datetime="2025-03-01T10:00:00+00:00" class="time">10:00</time></a></div>
 </div>
</div>
<div class="tgme_widget_message_wrap js-widget_message_wrap">
 <div class="tgme_widget_message js-widget_message" data-post="bestmemes/102">
  <div class="tgme_widget_message_grouped_wrap js-message_grouped_wrap"><div class="tgme_widget_message_grouped">
   <a class="tgme_widget_message_photo_wrap grouped_media_wrap blured js-message_photo"
      style="left:0px;top:0px;background-image:url(&#39;https://cdn4.telesco.pe/file/b.jpg&#39;)"
      data-single-url="https://t.me/bestmemes/102?single" href="https://t.me/bestmemes/102?single"></a>
   <a class="tgme_widget_message_video_player grouped_media_wrap blured js-message_video_player"
      data-single-url="https://t.me/bestmemes/103?single" href="https://t.me/bestmemes/103?single">
    <i class="tgme_widget_message_video_thumb" style="background-image:url('https://cdn4.telesco.pe/file/t.jpg')"></i>
    <div class="tgme_widget_message_video_wrap"><video class="tgme_widget_message_video js-message_video" src="https://cdn4.telesco.pe/file/v.mp4"></video></div>
    <time class="message_video_duration js-message_video_duration">0:14</time>
   </a>
  </div></div>
 </div>
</div>
<div class="tgme_widget_message_wrap js-widget_message_wrap">
 <div class="tgme_widget_message js-widget_message" data-post="bestmemes/104">
  <a class="tgme_widget_message_video_player not_supported js-message_video_player" href="https://t.me/bestmemes/104">
   <i class="tgme_widget_message_video_thumb" style="background-image:url('https://cdn4.telesco.pe/file/t2.jpg')"></i>
   <div class="message_media_not_supported"><div class="message_media_not_supported_label">Media is too big</div></div>
   <time class="message_video_duration js-message_video_duration">1:02:03</time>
  </a>
 </div>
</div>
<div class="tgme_widget_message_wrap js-widget_message_wrap">
 <div class="tgme_widget_message js-widget_message" data-post="otherchannel/999">
  <a class="tgme_widget_message_photo_wrap" style="background-image:url('https://cdn4.telesco.pe/file/x.jpg')" href="https://t.me/otherchannel/999"></a>
 </div>
</div>
<a class="tme_messages_more js-messages_more" data-before="101" href="/s/bestmemes?before=101"></a>
</body></html>
"""


def test_parse_page():
    page = parse_page(PAGE, "BestMemes")
    assert page.title == "Best Memes"
    assert page.before == 101
    assert [p.id for p in page.posts] == [101, 102, 104]

    first = page.posts[0]
    assert first.text == "first line\nsecond line"
    assert first.date is not None and first.date.year == 2025
    assert [(m.post_id, m.kind, m.url) for m in first.media] == [(101, "photo", "https://cdn4.telesco.pe/file/a.jpg")]

    album = page.posts[1]
    assert [(m.post_id, m.kind, m.url, m.duration) for m in album.media] == [
        (102, "photo", "https://cdn4.telesco.pe/file/b.jpg", None),
        (103, "video", "https://cdn4.telesco.pe/file/v.mp4", 14),
    ]

    big = page.posts[2]
    assert [(m.kind, m.duration) for m in big.media] == [("too_big", 3723)]


def test_parse_page_without_preview():
    html = "<html><head><meta property='og:title' content='Telegram'></head><body>private</body></html>"
    page = parse_page(html, "secret")
    assert page.posts == [] and page.before is None
    assert not has_preview(html)
    assert has_preview(PAGE)


@pytest.mark.parametrize(("raw", "want"), [("0:14", 14), ("2:05", 125), ("1:00:00", 3600), ("", None), ("live", None)])
def test_parse_duration(raw, want):
    assert parse_duration(raw) == want


def _meme(text: str, size=(640, 480)) -> Image.Image:
    img = Image.new("RGB", size, (240, 240, 240))
    d = ImageDraw.Draw(img)
    d.rectangle((40, 60, 400, 380), fill=(30, 90, 200))
    d.ellipse((300, 120, 600, 420), fill=(220, 60, 40))
    d.text((20, 10), text, fill=(0, 0, 0))
    return img


def test_dhash_matches_rescaled_copy_and_differs_for_other_image():
    a = dhash(_meme("hello"))
    b = dhash(_meme("hello").resize((320, 240)))
    other = Image.new("RGB", (640, 480), (255, 255, 255))
    ImageDraw.Draw(other).rectangle((300, 0, 640, 200), fill=(0, 0, 0))
    assert len(a) == 256
    assert hamming(a, b) <= 12
    assert hamming(a, dhash(other)) > 40


def test_content_hash_equals_thumb_hash(tmp_path: Path):
    src = tmp_path / "m.png"
    _meme("same").save(src)
    h = content_hash("image", src, tmp_path / "w", max_video_frames=8, frame_max_side=1024, thumb_size=480)
    thumb = tmp_path / "t.webp"
    t = Image.open(src).convert("RGB")
    t.thumbnail((480, 1440))
    t.save(thumb, "WEBP", quality=80)
    assert hamming(h, thumb_hash(thumb)) <= 6


def test_poster_time_matches_video_frames():
    assert video_times(0, 8) == [0.0]
    assert poster_time(2.0, 8) == video_times(2.0, 8)[0]
    times = video_times(30.0, 8)
    assert len(times) == 8 and poster_time(30.0, 8) == times[1]


def test_download_retries_transient_cdn_errors(tmp_path: Path):
    buf = io.BytesIO()
    _meme("x").save(buf, "JPEG")
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if len(calls) < 3:
            return httpx.Response(500)
        return httpx.Response(200, content=buf.getvalue())

    async def run():
        s = Settings(database_url="postgresql://x", internal_token="x", upload_dir=tmp_path)
        imp = ChannelImporter.__new__(ChannelImporter)
        imp.s, imp.max_bytes = s, 10 << 20
        imp.storage = Storage(tmp_path, None, None, 10 << 20)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch("memesearch.channels.asyncio.sleep", new=AsyncMock()):
                return await imp._download_url(client, "https://cdn.example/a.jpg")

    staged = asyncio.run(run())
    assert len(calls) == 3
    assert staged.media.ext == "jpg" and staged.size == len(buf.getvalue())
