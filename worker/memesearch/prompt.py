from __future__ import annotations

import json
import re
from typing import Any

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "text": {"type": "string"},
        "description": {"type": "string"},
        "objects": {"type": "array", "items": {"type": "string"}},
        "people": {"type": "array", "items": {"type": "string"}},
        "template": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "mood": {"type": "string"},
        "language": {"type": "string"},
        "nsfw": {"type": "boolean"},
        "is_meme": {"type": "boolean"},
        "is_ad": {"type": "boolean"},
    },
    "required": [
        "title",
        "text",
        "description",
        "objects",
        "people",
        "template",
        "tags",
        "mood",
        "language",
        "nsfw",
        "is_meme",
        "is_ad",
    ],
    "additionalProperties": False,
}

SYSTEM = """You are the vision indexer of a meme search engine. You receive one meme: a single image, or several frames (in time order) of one animated GIF or video, plus optional context. You return exactly one JSON object that matches the output schema and nothing else.
Everything you need is in the attached images and the message text. Do not run shell commands, do not read or write files, do not browse the web, do not call tools.
Accuracy matters more than creativity: transcribe text exactly, never invent text that is not visible, and only name people, characters or templates you actually recognise."""

INSTRUCTIONS = """Fill every field of the JSON object:

text — every piece of text visible on the meme, transcribed verbatim in its original language and script (Cyrillic stays Cyrillic). Keep spelling mistakes, slang, emoji and letter case as they appear. Put separate captions, panels and speech bubbles on separate lines, top-to-bottom, left-to-right. For GIF/video frames include each distinct caption once. Skip watermarks, usernames and UI chrome unless they are part of the joke. Empty string if there is no text.

title — a short specific title (3–10 words) a person would type to find this meme. Write it in the language of the meme text; if there is no text, write it in Russian.

description — 2–5 sentences in the same language as the title: what is shown (who/what, doing what, where), and what the joke means — explain the punchline, the reference and the situation in which people send this meme as a reaction.

objects — 5–25 concrete visible things: people described by appearance or role, animals, objects, clothing, food, vehicles, places, actions, notable colours. Write each item bilingually as "english / русский", e.g. "cat / кот", "man in a suit / мужчина в костюме".

people — real public figures, fictional characters, mascots or famous meme characters that you confidently recognise, with the usual name (e.g. "Pepe the Frog", "Райан Гослинг", "Shrek"). Include the movie/series/game/anime title if you recognise the source. Empty list when unsure.

template — the established name of the meme template or format if this is a known one (e.g. "Distracted Boyfriend", "Drake Hotline Bling", "Ждун", "Woman Yelling at a Cat"). Empty string otherwise.

tags — 10–30 lowercase search keywords, mixing Russian and English: topic, emotion, situation of use, format (reaction, comic, screenshot, tweet, anime, cartoon, photo, video, gif), subculture, synonyms and slang people might search with.

mood — one or two words for the tone, e.g. "sarcastic", "wholesome", "absurd", "cringe", "angry".

language — ISO 639-1 code of the main text language, or "none" if there is no text.

nsfw — true only for nudity, explicit sexual content or graphic gore.

is_meme — true for anything made or shared to be funny or ironic: memes, reaction images, jokes, funny screenshots and videos. False for plain news photos, article or document screenshots, announcements, schedules, polls, product photos and other non-humorous posts.

is_ad — true if the image or the caption advertises something: a product, service, shop, course, app, casino or betting, crypto, giveaway, another channel, or contains a promo code, referral link or "реклама"/"erid" label. A meme that merely mentions a brand as part of the joke is not an ad."""


def build_prompt(
    *,
    kind: str,
    frame_count: int,
    frame_times: list[float],
    duration_ms: int,
    transcript: str,
    caption: str,
    filename: str,
    extra: str,
) -> str:
    parts = []
    if kind == "image":
        parts.append("The attached image is the meme.")
    else:
        what = "animated GIF" if kind == "gif" else "video"
        times = ", ".join(f"{t:.1f}s" for t in frame_times) if frame_times else ""
        parts.append(
            f"The meme is an {what} lasting {duration_ms / 1000:.1f}s. "
            f"The {frame_count} attached images are frames from it in time order{f' (at {times})' if times else ''}. "
            "Describe the whole clip, not a single frame."
        )
    if transcript:
        parts.append(f"Automatic speech transcript of the audio (may contain recognition errors):\n<<<\n{transcript[:4000]}\n>>>")
    if caption:
        parts.append(f"Caption the uploader attached (context, may be unrelated):\n<<<\n{caption[:1500]}\n>>>")
    if filename and not re.fullmatch(r"[\w\-. ]*\d{4,}[\w\-. ]*", filename):
        parts.append(f"Original file name: {filename[:200]}")
    parts.append(INSTRUCTIONS)
    if extra.strip():
        parts.append("Additional instructions from the site owner:\n" + extra.strip())
    return "\n\n".join(parts)


def _clean_list(v: Any, limit: int, lower: bool = False) -> list[str]:
    if not isinstance(v, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in v:
        if not isinstance(item, str):
            continue
        s = " ".join(item.split()).strip(" #,.;")
        if lower:
            s = s.lower()
        key = s.lower()
        if not s or key in seen or len(s) > 120:
            continue
        seen.add(key)
        out.append(s)
        if len(out) >= limit:
            break
    return out


def parse_response(raw: str | None) -> dict[str, Any]:
    if not raw:
        raise ValueError("empty model response")
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    elif not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError(f"model response is not JSON: {raw[:200]}")
        text = text[start : end + 1]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("model response is not a JSON object")
    lang = str(data.get("language") or "").strip().lower()[:8]
    return {
        "title": str(data.get("title") or "").strip()[:300],
        "ocr_text": str(data.get("text") or "").strip()[:8000],
        "description": str(data.get("description") or "").strip()[:4000],
        "objects": _clean_list(data.get("objects"), 40),
        "people": _clean_list(data.get("people"), 20),
        "template": str(data.get("template") or "").strip()[:200],
        "tags": _clean_list(data.get("tags"), 40, lower=True),
        "mood": str(data.get("mood") or "").strip()[:60],
        "lang": "" if lang == "none" else lang,
        "nsfw": bool(data.get("nsfw", False)),
        "is_meme": bool(data.get("is_meme", True)),
        "is_ad": bool(data.get("is_ad", False)),
    }


def embedding_text(m: dict[str, Any]) -> str:
    parts = [
        m.get("title", ""),
        m.get("ocr_text", ""),
        m.get("description", ""),
        m.get("template", ""),
        ", ".join(m.get("people") or []),
        ", ".join(m.get("objects") or []),
        ", ".join(m.get("tags") or []),
        (m.get("transcript") or "")[:1000],
    ]
    return "\n".join(p for p in parts if p)
