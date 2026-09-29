from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

STRONG_AD = [
    r"#\s*реклам",
    r"\bреклама\b",
    r"\bна правах рекламы\b",
    r"\berid\b",
    r"\bинн\s*:?\s*\d{10}",
    r"\bпромокод",
    r"\bpromo\s*code\b",
    r"#\s*ad\b",
    r"#\s*sponsored\b",
    r"\bsponsored\b",
    r"\bпартн[её]рск\w* (пост|материал|публикац)",
    r"\bказино\b",
    r"\bбукмекер",
    r"\bcasino\b",
]

WEAK_AD = [
    r"\bрозыгрыш",
    r"\bgiveaway\b",
    r"\bскидк\w*\s+(до\s+)?\d+\s*%",
    r"\b\d+\s*%\s*скидк",
    r"\bкурс\w*\b",
    r"\bвебинар",
    r"\bзаработ\w*",
    r"\bпассивн\w+ доход",
    r"\bинвест\w*",
    r"\bкрипт\w*",
    r"\bставк[иау]\b",
    r"\bбесплатно\b",
    r"\bжми\b",
    r"\bпереходи(те)?\b",
    r"\bуспей\w*\b",
    r"\bзаказ\w*\b",
    r"\bкупи(ть|те)?\b",
    r"\bbuy now\b",
    r"\bdiscount\b",
    r"\blimited offer\b",
]

INVITE = re.compile(r"(t\.me|telegram\.me)/(\+|joinchat/)", re.I)
BOT_START = re.compile(r"(t\.me|telegram\.me)/\w+\?start=", re.I)
TG_LINK = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z]\w{3,31})", re.I)
MENTION = re.compile(r"(?<![\w/])@([A-Za-z]\w{3,31})")
URL = re.compile(r"https?://\S+|(?:t\.me|telegram\.me)/\S+", re.I)


@dataclass
class PostFilter:
    max_text_chars: int = 700
    extra_words: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        words = [re.escape(w.strip().lower()) for w in self.extra_words if w.strip()]
        self._strong = [re.compile(p, re.I) for p in STRONG_AD] + [re.compile(w, re.I) for w in words]
        self._weak = [re.compile(p, re.I) for p in WEAK_AD]

    def check(self, text: str, links: list[str], buttons: int, channel: str) -> str:
        body = text or ""
        low = body.lower()
        for rx in self._strong:
            if m := rx.search(low):
                return f"advertisement: {m.group(0).strip()}"
        all_links = {u.rstrip("/.,)").lower() for u in links + URL.findall(body)}
        own = channel.lower()
        foreign = {m.group(1).lower() for u in all_links if (m := TG_LINK.search(u))}
        foreign |= {m.lower() for m in MENTION.findall(body)}
        foreign.discard(own)
        score = sum(1 for rx in self._weak if rx.search(low))
        score += 2 * sum(1 for u in all_links if INVITE.search(u) or BOT_START.search(u))
        score += len(foreign)
        score += 1 if buttons else 0
        score += 1 if len(all_links) >= 3 else 0
        if score >= 3:
            return "advertisement (links and promo wording)"
        if self.max_text_chars and len(URL.sub("", body).strip()) > self.max_text_chars:
            return "not a meme (long text post)"
        return ""


def analysis_verdict(fields: dict[str, Any]) -> str:
    if fields.get("is_ad"):
        return "advertisement (analysis)"
    if fields.get("is_meme") is False:
        return "not a meme (analysis)"
    return ""
