from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI, BadRequestError

from .config import Settings
from .prompt import SCHEMA, SYSTEM, parse_response

log = logging.getLogger(__name__)


class FallbackAnalyzer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.sem = asyncio.Semaphore(max(1, settings.fallback_concurrency))
        self.client = (
            AsyncOpenAI(
                base_url=settings.fallback_base_url,
                api_key=settings.fallback_api_key,
                timeout=settings.fallback_timeout_seconds,
                max_retries=3,
            )
            if settings.fallback_configured
            else None
        )
        self._strict_ok = True

    @property
    def configured(self) -> bool:
        return self.client is not None

    @staticmethod
    def _image(path: Path, detail: str) -> dict[str, Any]:
        data = base64.b64encode(path.read_bytes()).decode()
        return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}", "detail": detail}}

    async def analyze(self, prompt: str, images: list[Path], model: str) -> tuple[dict[str, Any], str]:
        if self.client is None:
            raise RuntimeError("fallback API is not configured")
        model = model or self.settings.fallback_model
        detail = self.settings.fallback_image_detail if len(images) <= 2 else "low"
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        content += [self._image(p, detail) for p in images]
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}]
        async with self.sem:
            if self._strict_ok:
                try:
                    resp = await self.client.chat.completions.create(
                        model=model,
                        messages=messages,
                        response_format={
                            "type": "json_schema",
                            "json_schema": {"name": "meme_index", "schema": SCHEMA, "strict": True},
                        },
                    )
                    return parse_response(resp.choices[0].message.content), model
                except BadRequestError as exc:
                    if "response_format" not in str(exc) and "json_schema" not in str(exc):
                        raise
                    log.warning("fallback provider rejected json_schema, switching to json_object: %s", exc)
                    self._strict_ok = False
            messages[0] = {
                "role": "system",
                "content": SYSTEM + "\nReturn only a JSON object matching this JSON schema:\n" + json.dumps(SCHEMA),
            }
            resp = await self.client.chat.completions.create(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
            )
            return parse_response(resp.choices[0].message.content), model
