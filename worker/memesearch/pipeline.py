from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import numpy as np
from redis.asyncio import Redis

from .codex import CodexFailed, CodexPool, CodexUnavailable
from .config import Settings
from .db import SettingsCache, fetch_meme
from .fallback import FallbackAnalyzer
from .media import Prepared, extract_audio, prepare
from .prompt import build_prompt, embedding_text
from .rqueue import JobQueue
from .storage import rel_thumb, safe_join
from .transcribe import Transcriber

log = logging.getLogger(__name__)


class RetryLater(Exception):
    def __init__(self, message: str, delay: float, count_attempt: bool) -> None:
        super().__init__(message)
        self.delay = delay
        self.count_attempt = count_attempt


class EmbedPending(Exception):
    pass


TEXT_COLUMNS = (
    "title",
    "description",
    "ocr_text",
    "transcript",
    "objects",
    "tags",
    "people",
    "template",
    "lang",
    "mood",
    "nsfw",
)


class MLClient:
    def __init__(self, settings: Settings) -> None:
        self.http = httpx.AsyncClient(
            base_url=settings.ml_url,
            headers={"X-Internal-Token": settings.internal_token},
            timeout=httpx.Timeout(300, connect=10),
        )

    async def embed_meme(self, image_paths: list[Path], text: str) -> tuple[np.ndarray | None, np.ndarray | None]:
        resp = await self.http.post("/embed/meme", json={"image_paths": [str(p) for p in image_paths], "text": text})
        if resp.status_code == 503:
            raise RetryLater("ml service is still loading models", 30, False)
        resp.raise_for_status()
        data = resp.json()
        clip = np.asarray(data["clip"], dtype=np.float32) if data.get("clip") else None
        vec = np.asarray(data["text"], dtype=np.float32) if data.get("text") else None
        return clip, vec


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        pool: asyncpg.Pool,
        redis: Redis,
        queue: JobQueue,
        codex: CodexPool,
        fallback: FallbackAnalyzer,
        transcriber: Transcriber | None,
        ml: MLClient,
    ) -> None:
        self.s = settings
        self.pool = pool
        self.redis = redis
        self.queue = queue
        self.codex = codex
        self.fallback = fallback
        self.transcriber = transcriber
        self.ml = ml
        self.app_settings = SettingsCache(pool)

    async def handle(self, job: dict[str, Any]) -> None:
        meme_id = int(job["id"])
        lock = f"ms:lock:meme:{meme_id}"
        if not await self.redis.set(lock, "1", nx=True, ex=self.s.job_timeout_seconds + 120):
            log.info("meme %s is busy, retrying the job later", meme_id)
            await self.queue.delay(job, 60)
            return
        try:
            await self._process(meme_id, bool(job.get("analyze", True)), job)
        finally:
            await self.redis.delete(lock)

    async def _process(self, meme_id: int, analyze: bool, job: dict[str, Any]) -> None:
        meme = await fetch_meme(self.pool, meme_id)
        if meme is None:
            return
        full = analyze or meme["analyzed_at"] is None
        requested_at = job.get("at")
        if full and requested_at and meme["analyzed_at"] and meme["analyzed_at"].timestamp() >= float(requested_at):
            log.info("meme %s was analyzed after this job was queued, embedding only", meme_id)
            full = False
        if meme["locked"]:
            full = False
        if full:
            await self.pool.execute(
                """UPDATE memes SET status=CASE WHEN status='done' THEN 'done' ELSE 'processing' END,
                attempts=attempts+1, updated_at=now() WHERE id=$1""",
                meme_id,
            )
            await self.queue.event({"type": "meme", "id": meme_id, "status": "processing"})

        workdir = self.s.work_dir / f"{meme_id}-{uuid.uuid4().hex[:8]}"
        try:
            async with asyncio.timeout(self.s.job_timeout_seconds):
                await self._run(meme, full, workdir)
        except EmbedPending as exc:
            await self._fail_or_retry(meme_id, {**job, "analyze": False}, str(exc), 30, False, full)
        except TimeoutError:
            await self._fail_or_retry(meme_id, job, "processing timed out", 120, full, full)
        except RetryLater as exc:
            await self._fail_or_retry(meme_id, job, str(exc), exc.delay, exc.count_attempt and full, full)
        except (CodexFailed, httpx.HTTPError, OSError, ValueError, RuntimeError) as exc:
            log.warning("meme %s failed: %s", meme_id, exc)
            await self._fail_or_retry(meme_id, job, str(exc), None, full, full)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    async def _run(self, meme: dict[str, Any], full: bool, workdir: Path) -> None:
        meme_id = meme["id"]
        src = safe_join(self.s.upload_dir, meme["file_path"])
        if not src.exists():
            await self._mark_failed(meme_id, f"file missing: {meme['file_path']}")
            return
        thumb_rel = rel_thumb(meme["sha256"])
        prep: Prepared = await asyncio.to_thread(
            prepare,
            meme["kind"],
            src,
            workdir,
            safe_join(self.s.upload_dir, thumb_rel),
            max_video_frames=self.s.max_video_frames,
            max_gif_frames=self.s.max_gif_frames,
            analysis_max_side=self.s.analysis_max_side,
            frame_max_side=self.s.frame_max_side,
            thumb_size=self.s.thumb_size,
        )
        fields: dict[str, Any] = {}
        provider, model = meme["provider"], meme["model"]
        if full:
            app = await self.app_settings.get()
            transcript = meme["transcript"]
            if meme["kind"] == "video" and prep.has_audio and app.transcribe and self.transcriber:
                transcript = await self._transcribe(src, workdir) or ""
            prompt = build_prompt(
                kind=meme["kind"],
                frame_count=len(prep.frames),
                frame_times=prep.frame_times,
                duration_ms=prep.duration_ms,
                transcript=transcript,
                caption=meme["caption"],
                filename=meme["original_name"],
                extra=app.prompt_extra,
            )
            fields, provider, model = await self._analyze(prompt, prep.frames, workdir, app)
            fields["transcript"] = transcript
        merged = {
            **{k: meme[k] for k in ("title", "ocr_text", "description", "template", "people", "objects", "tags", "transcript")},
            **fields,
        }
        try:
            clip, text_vec = await self.ml.embed_meme(prep.frames, embedding_text(merged))
        except (httpx.HTTPError, RetryLater) as exc:
            if fields:
                await self._save_fields(meme_id, fields, provider, model)
                raise EmbedPending(f"analysis saved, embedding failed: {exc}") from exc
            raise
        await self._save(meme_id, prep, thumb_rel, fields, provider, model, clip, text_vec)
        await self.queue.bump()
        await self.queue.event({"type": "meme", "id": meme_id, "status": "done", "title": merged.get("title", "")})
        log.info("meme %s done via %s", meme_id, provider if full else "embed-only")

    async def _transcribe(self, src: Path, workdir: Path) -> str | None:
        wav = workdir / "audio.wav"
        ok = await asyncio.to_thread(extract_audio, src, wav, self.s.max_transcribe_seconds)
        if not ok:
            return None
        try:
            text, _ = await asyncio.to_thread(self.transcriber.transcribe, wav)
            return text
        except Exception as exc:
            log.warning("transcription failed: %s", exc)
            return None

    async def _analyze(self, prompt: str, frames: list[Path], workdir: Path, app) -> tuple[dict[str, Any], str, str]:
        reasons: list[str] = []
        retry_at: datetime | None = None
        if app.codex_enabled:
            try:
                res = await self.codex.analyze(
                    prompt, frames, workdir, app.codex_model or (self.s.codex_model or ""), app.codex_effort
                )
                return res.fields, "codex", res.model
            except CodexUnavailable as exc:
                reasons.append(f"codex unavailable: {exc}")
                retry_at = exc.retry_at
            except CodexFailed as exc:
                reasons.append(f"codex failed: {exc}")
        if app.fallback_enabled and self.fallback.configured:
            try:
                fields, model = await self.fallback.analyze(prompt, frames, app.fallback_model)
                return fields, "fallback", model
            except Exception as exc:
                reasons.append(f"fallback failed: {exc}")
                raise RetryLater("; ".join(reasons)[:2000], 300, True) from exc
        if not app.codex_enabled and not (app.fallback_enabled and self.fallback.configured):
            raise RetryLater("no analysis provider enabled", 1800, False)
        if retry_at is not None:
            delay = max(60.0, (retry_at - datetime.now(timezone.utc)).total_seconds() + 30)
            raise RetryLater("; ".join(reasons)[:2000], delay, False)
        if reasons and reasons[0].startswith("codex unavailable"):
            raise RetryLater("; ".join(reasons)[:2000], 900, False)
        raise CodexFailed("; ".join(reasons)[:2000])

    async def _save(
        self,
        meme_id: int,
        prep: Prepared,
        thumb_rel: str,
        fields: dict[str, Any],
        provider: str,
        model: str,
        clip: np.ndarray | None,
        text_vec: np.ndarray | None,
    ) -> None:
        sets = [
            "status='done'",
            "error=''",
            "processed_at=now()",
            "updated_at=now()",
            "thumb_path=$2",
            "width=$3",
            "height=$4",
            "duration_ms=$5",
            "clip_vec=$6",
            "text_vec=$7",
            "provider=$8",
            "model=$9",
            "phash=COALESCE($10::text::bit(256), phash)",
        ]
        args: list[Any] = [
            meme_id,
            thumb_rel if prep.thumb else "",
            prep.width,
            prep.height,
            prep.duration_ms,
            clip,
            text_vec,
            provider or "",
            model or "",
            prep.phash,
        ]
        sets += self._text_sets(fields, args)
        await self.pool.execute(f"UPDATE memes SET {', '.join(sets)} WHERE id=$1", *args)

    @staticmethod
    def _text_sets(fields: dict[str, Any], args: list[Any]) -> list[str]:
        sets = []
        for col in TEXT_COLUMNS:
            if col in fields:
                args.append(fields[col])
                sets.append(f"{col}=CASE WHEN locked THEN {col} ELSE ${len(args)} END")
        if fields:
            sets.append("analyzed_at=now()")
        return sets

    async def _save_fields(self, meme_id: int, fields: dict[str, Any], provider: str, model: str) -> None:
        sets = ["provider=$2", "model=$3", "updated_at=now()"]
        args: list[Any] = [meme_id, provider or "", model or ""]
        sets += self._text_sets(fields, args)
        await self.pool.execute(f"UPDATE memes SET {', '.join(sets)} WHERE id=$1", *args)

    async def _mark_failed(self, meme_id: int, error: str) -> None:
        await self.pool.execute(
            """UPDATE memes SET status=CASE WHEN status='done' THEN 'done' ELSE 'failed' END,
            error=$2, updated_at=now() WHERE id=$1""",
            meme_id,
            error[:2000],
        )
        await self.queue.event({"type": "meme", "id": meme_id, "status": "failed", "error": error[:300]})

    async def _fail_or_retry(
        self,
        meme_id: int,
        job: dict[str, Any],
        error: str,
        delay: float | None,
        count_attempt: bool,
        incremented: bool,
    ) -> None:
        row = await self.pool.fetchrow("SELECT attempts, status FROM memes WHERE id=$1", meme_id)
        if row is None:
            return
        attempts = row["attempts"]
        if incremented and not count_attempt:
            await self.pool.execute("UPDATE memes SET attempts=GREATEST(0, attempts-1) WHERE id=$1", meme_id)
            attempts -= 1
        if count_attempt and attempts >= self.s.max_attempts:
            await self._mark_failed(meme_id, error)
            return
        if delay is None:
            delay = 60 * (2 ** max(0, attempts - 1))
        new_status = "pending" if row["status"] in ("processing", "pending") else row["status"]
        await self.pool.execute(
            "UPDATE memes SET status=$2, error=$3, updated_at=now() WHERE id=$1", meme_id, new_status, error[:2000]
        )
        await self.queue.delay(job, delay)
        log.info("meme %s retry in %.0fs: %s", meme_id, delay, error[:200])
