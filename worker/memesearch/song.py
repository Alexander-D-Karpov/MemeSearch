from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from shazamio import Shazam

log = logging.getLogger(__name__)


def song_name(result: dict[str, Any] | None) -> str:
    track = (result or {}).get("track") or {}
    title = str(track.get("title") or "").strip()
    artist = str(track.get("subtitle") or "").strip()
    if not title:
        return ""
    return f"{artist} — {title}" if artist else title


class SongRecognizer:
    def __init__(self, proxy: str | None, timeout: float = 45) -> None:
        self.proxy = proxy
        self.timeout = timeout
        self.shazam = Shazam(language="ru-RU", endpoint_country="RU")

    async def recognize(self, wav: Path) -> str:
        try:
            result = await asyncio.wait_for(self.shazam.recognize(str(wav), proxy=self.proxy), self.timeout)
            return song_name(result)
        except Exception as exc:
            log.info("song recognition failed: %s", exc)
            return ""
