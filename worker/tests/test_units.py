from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memesearch.codex import classify, cooldown_from_usage, pause_reason, usage_pause_until
from memesearch.config import Settings
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
