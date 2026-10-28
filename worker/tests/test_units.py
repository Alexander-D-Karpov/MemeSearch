from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from memesearch.codex import classify, cooldown_from_usage, pause_reason, usage_pause_until
from memesearch.config import Settings
from memesearch.dedupe import Pair, Verdict, Visual, groups, usable
from memesearch.media import blurhash, faststart, needs_faststart, repair_media
from memesearch.prompt import SCHEMA, build_prompt, embedding_text, parse_response
from memesearch.storage import rel_original, rel_thumb, safe_join, sniff


@pytest.mark.parametrize(
    ("head", "kind", "ext"),
    [
        (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00", "image", "jpg"),
        (b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR", "image", "png"),
        (b"GIF89a\x01\x00\x01\x00", "gif", "gif"),
        (b"RIFF\x24\x00\x00\x00WEBPVP8 ", "image", "webp"),
        (b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00", "video", "mp4"),
        (b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00", "video", "mov"),
        (b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00", "image", "heic"),
        (b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01webm", "video", "webm"),
    ],
)
def test_sniff_matches_go(head, kind, ext):
    media = sniff(head)
    assert media is not None
    assert (media.kind, media.ext) == (kind, ext)


def test_sniff_rejects_text():
    assert sniff(b"just some text") is None


def test_paths():
    sha = "cd57fbedcca7b1b947ad3ac670b6fb3bdffd56c255ca44973fe7c09ac2bd5c05"
    assert rel_original(sha, "jpg") == f"originals/cd/57/{sha}.jpg"
    assert rel_thumb(sha) == f"thumbs/cd/57/{sha}.webp"


def test_safe_join_blocks_escape(tmp_path: Path):
    assert safe_join(tmp_path, "originals/a/b.jpg") == (tmp_path / "originals/a/b.jpg").resolve()
    with pytest.raises(ValueError):
        safe_join(tmp_path, "../etc/passwd")


def test_parse_response_normalizes():
    raw = """```json
{"title": " Кот ", "text": "КОГДА УВИДЕЛ ОГУРЕЦ", "description": "d",
 "objects": ["cat / кот", "cat / кот", ""], "people": [], "template": "",
 "tags": ["Cat", "#мем", "cat"], "mood": "funny", "language": "NONE", "nsfw": false}
```"""
    out = parse_response(raw)
    assert out["title"] == "Кот"
    assert out["ocr_text"] == "КОГДА УВИДЕЛ ОГУРЕЦ"
    assert out["objects"] == ["cat / кот"]
    assert out["tags"] == ["cat", "мем"]
    assert out["lang"] == ""
    assert out["nsfw"] is False


def test_parse_response_rejects_non_json():
    with pytest.raises(ValueError):
        parse_response("sorry, I can't help")
    with pytest.raises(ValueError):
        parse_response("")


def test_schema_is_strict():
    assert SCHEMA["additionalProperties"] is False
    assert set(SCHEMA["required"]) == set(SCHEMA["properties"])


def test_build_prompt_for_video_mentions_frames_and_transcript():
    prompt = build_prompt(
        kind="video",
        frame_count=3,
        frame_times=[0.5, 2.0, 4.0],
        duration_ms=5000,
        transcript="привет",
        caption="",
        filename="IMG_20240101_123456.mp4",
        extra="Name the movie.",
    )
    assert "3 attached images" in prompt
    assert "привет" in prompt
    assert "Name the movie." in prompt
    assert "IMG_20240101" not in prompt


def test_embedding_text_joins_fields():
    text = embedding_text({"title": "t", "ocr_text": "o", "tags": ["a", "b"], "objects": [], "people": []})
    assert text.splitlines() == ["t", "o", "a, b"]


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("You've hit your usage limit. Try again at 5pm.", "limit"),
        ("429 Too Many Requests", "limit"),
        ("401 Unauthorized: refresh token expired", "auth"),
        ("stream disconnected before completion", "transient"),
        ("[Errno 11] Resource temporarily unavailable", "transient"),
        ("model returned invalid JSON", "other"),
    ],
)
def test_classify(message, kind):
    assert classify(RuntimeError(message)) == kind


def test_cooldown_uses_reset_time_of_exhausted_window():
    reset = datetime.now(timezone.utc) + timedelta(hours=3)
    usage = {"rateLimits": {"primary": {"usedPercent": 100, "resetsAt": int(reset.timestamp())}}}
    until = cooldown_from_usage(usage, 30)
    assert abs((until - reset).total_seconds()) < 2


def test_cooldown_default_without_usage():
    until = cooldown_from_usage(None, 30)
    delta = until - datetime.now(timezone.utc)
    assert timedelta(minutes=29) < delta < timedelta(minutes=31)


def test_pause_at_configured_usage_percent():
    reset = datetime.now(timezone.utc) + timedelta(hours=2)
    usage = {"rateLimits": {"primary": {"usedPercent": 91, "resetsAt": int(reset.timestamp())}, "secondary": {"usedPercent": 20}}}
    assert usage_pause_until(usage, 100) is None
    until = usage_pause_until(usage, 90)
    assert until is not None and abs((until - reset).total_seconds()) < 2
    assert pause_reason(usage, 90).startswith("paused at 91% of the short-term limit")
    assert pause_reason(usage, 100) == ""


def test_weekly_threshold_is_separate():
    soon = datetime.now(timezone.utc) + timedelta(hours=2)
    week = datetime.now(timezone.utc) + timedelta(days=4)
    usage = {
        "rateLimits": {
            "primary": {"usedPercent": 10, "resetsAt": int(soon.timestamp())},
            "secondary": {"usedPercent": 92, "resetsAt": int(week.timestamp())},
        }
    }
    assert usage_pause_until(usage, 90) is not None
    assert usage_pause_until(usage, 90, 100) is None
    assert pause_reason(usage, 90, 100) == ""
    assert pause_reason(usage, 90) == "paused at 92% of the weekly limit (CODEX_MAX_USAGE_PERCENT=90)"
    assert pause_reason(usage, 100, 90) == "paused at 92% of the weekly limit (CODEX_MAX_WEEKLY_PERCENT=90)"
    usage["rateLimits"]["primary"]["usedPercent"] = 95
    until = usage_pause_until(usage, 90, 100)
    assert until is not None and abs((until - soon).total_seconds()) < 2
    assert pause_reason(usage, 90, 100).startswith("paused at 95% of the short-term limit")


def test_weekly_setting_defaults_to_general_percent():
    assert Settings(codex_max_usage_percent=90).codex_limits == (90, 90)
    assert Settings(codex_max_usage_percent=90, codex_max_weekly_percent=100).codex_limits == (90, 100)
    assert Settings(codex_max_usage_percent=90, codex_max_weekly_percent="").codex_limits == (90, 90)


def test_blurhash_matches_reference_encoder():
    a = np.zeros((24, 32, 3), dtype="uint8")
    a[..., 0] = np.linspace(0, 255, 32, dtype="uint8")[None, :]
    a[..., 2] = np.linspace(255, 0, 24, dtype="uint8")[:, None]
    assert blurhash(Image.fromarray(a)) == "L:HZ1m2GwxWroAWrjtfSfWfRfQfR"


def box(kind: bytes, payload: int) -> bytes:
    return (8 + payload).to_bytes(4, "big") + kind + b"\0" * payload


def test_needs_faststart_reads_top_level_boxes(tmp_path: Path):
    late = tmp_path / "late.mp4"
    late.write_bytes(box(b"ftyp", 16) + box(b"mdat", 1000) + box(b"moov", 50))
    early = tmp_path / "early.mp4"
    early.write_bytes(box(b"ftyp", 16) + box(b"moov", 50) + box(b"mdat", 1000))
    other = tmp_path / "clip.webm"
    other.write_bytes(box(b"ftyp", 16) + box(b"mdat", 1000) + box(b"moov", 50))
    assert needs_faststart(late)
    assert not needs_faststart(early)
    assert not needs_faststart(other)
    assert not faststart(early)


def test_repair_media_regenerates_missing_thumb_and_reports_missing_original(tmp_path: Path):
    src = tmp_path / "orig.png"
    Image.new("RGB", (640, 480), (200, 40, 40)).save(src)
    thumb = tmp_path / "thumbs" / "t.webp"
    fix = repair_media("image", src, thumb, 480, 6, need_blurhash=True)
    assert fix.regenerated and thumb.is_file() and len(fix.blurhash) == 28 and fix.phash
    again = repair_media("image", src, thumb, 480, 6, need_blurhash=False)
    assert not again.regenerated and again.blurhash == ""
    with pytest.raises(FileNotFoundError, match="original file is missing"):
        repair_media("image", tmp_path / "gone.png", tmp_path / "thumbs" / "x.webp", 480, 6, need_blurhash=True)


def _hash(seed: int, flips: int = 0) -> str:
    bits = list(np.random.default_rng(seed).integers(0, 2, 256))
    for i in range(flips):
        bits[i * 7] ^= 1
    return "".join(str(b) for b in bits)


def _row(id_: int, phash: str, **kw) -> dict:
    return {"id": id_, "video": False, "phash": phash, "locked": False, "hidden": False, "status": "done"} | kw


def test_dedupe_groups_near_copies():
    rows = [
        _row(5, _hash(1, 3)),
        _row(2, _hash(1)),
        _row(9, _hash(1, 10)),
        _row(3, _hash(2)),
        _row(4, _hash(1), video=True),
    ]
    out = groups(rows, 12)
    assert out == [{"keeper": 2, "dups": [(5, "3 bits apart", 0.25), (9, "10 bits apart", 10 / 12)]}]


def test_dedupe_groups_prefers_locked_and_skips_degenerate():
    flat = "0" * 250 + "1" * 6
    rows = [_row(1, _hash(1)), _row(7, _hash(1, 2), locked=True), _row(8, flat), _row(9, flat)]
    assert not usable(flat)
    assert groups(rows, 12) == [{"keeper": 7, "dups": [(1, "2 bits apart", 2 / 12)]}]


def test_dedupe_groups_visual_links_only_direct_sure_pairs():
    rows = [_row(1, _hash(1)), _row(2, _hash(2)), _row(3, _hash(3)), _row(4, None), _row(5, _hash(5))]
    edges = [
        Pair(2, 1, 0.98, Verdict(True, "looks 98% alike", 0.5)),
        Pair(3, 2, 0.97, Verdict(True, "looks 97% alike", 0.9)),
        Pair(4, 1, 0.99, Verdict(True, "looks 99% alike", 0.3)),
        Pair(5, 1, 0.93, Verdict(False, "looks 93% alike", 2.0)),
    ]
    assert groups(rows, 12, edges) == [{"keeper": 1, "dups": [(2, "looks 98% alike", 0.5), (4, "looks 99% alike", 0.3)]}]


JUST = "why is everything a disorder or syndrome maybe im just"


@pytest.mark.parametrize(
    ("sim", "a", "b", "want"),
    [
        (0.98, JUST + " sexy", JUST + " cool", "ask"),
        (0.92, JUST + " sexy", JUST + " cool", "ask"),
        (0.97, JUST + " sexy", JUST + " sexy", "sure"),
        (0.95, JUST + " sexy", JUST + " sexy", "ask"),
        (0.98, "скоро буду! да, уже в машине", "Скоро буду!\nДа, уже в машине", "sure"),
        (0.98, "Elon Musk @elonmusk\nhello world this is a tweet about cats", "hello world this is a tweet about cats", "sure"),
        (0.95, "совсем другой текст про котов", "a totally different caption", None),
        (0.89, "same text here", "same text here", None),
        (0.95, "", "", "ask"),
        (0.92, "", "", None),
        (0.98, "", "", "sure"),
        (0.99, "some text", "", "ask"),
        (0.95, "some text", "", None),
    ],
)
def test_visual_judge_merges_only_close_pictures_with_the_same_words(sim, a, b, want):
    v = Visual().judge(sim, a, b)
    assert (None if v is None else "sure" if v.sure else "ask") == want
