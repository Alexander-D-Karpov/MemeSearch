from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

import asyncpg

from .rqueue import STREAM_HIGH, JobQueue


class Unsupported(Exception):
    pass


class TooLarge(Exception):
    pass


@dataclass
class MediaType:
    kind: str
    mime: str
    ext: str


def sniff(head: bytes) -> MediaType | None:
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand == b"qt  ":
            return MediaType("video", "video/quicktime", "mov")
        if brand in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"):
            return MediaType("image", "image/heic", "heic")
        if brand in (b"avif", b"avis"):
            return MediaType("image", "image/avif", "avif")
        return MediaType("video", "video/mp4", "mp4")
    if head[:4] == b"\x1a\x45\xdf\xa3":
        if b"webm" in head:
            return MediaType("video", "video/webm", "webm")
        return MediaType("video", "video/x-matroska", "mkv")
    if head[:3] == b"\xff\xd8\xff":
        return MediaType("image", "image/jpeg", "jpg")
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return MediaType("image", "image/png", "png")
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return MediaType("gif", "image/gif", "gif")
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return MediaType("image", "image/webp", "webp")
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return MediaType("video", "video/x-msvideo", "avi")
    if head[:2] == b"BM":
        return MediaType("image", "image/bmp", "bmp")
    return None


def rel_original(sha: str, ext: str) -> str:
    return str(PurePosixPath("originals", sha[:2], sha[2:4], f"{sha}.{ext}"))


def rel_thumb(sha: str) -> str:
    return str(PurePosixPath("thumbs", sha[:2], sha[2:4], f"{sha}.webp"))


def safe_join(root: Path, rel: str) -> Path:
    p = (root / rel).resolve()
    if not p.is_relative_to(root.resolve()):
        raise ValueError("path escapes upload dir")
    return p


@dataclass
class Staged:
    tmp: Path
    sha: str
    size: int
    media: MediaType


@dataclass
class IngestResult:
    meme_id: int
    duplicate: bool
    kind: str


class Storage:
    def __init__(self, upload_dir: Path, pool: asyncpg.Pool, queue: JobQueue, max_bytes: int) -> None:
        self.upload_dir = upload_dir
        self.pool = pool
        self.queue = queue
        self.max_bytes = max_bytes
        self.tmp_dir = upload_dir / ".tmp"

    def stage(self, src: BinaryIO) -> Staged:
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        head = src.read(512)
        media = sniff(head)
        if media is None:
            raise Unsupported("unsupported media type")
        fd, name = tempfile.mkstemp(prefix="in-", dir=self.tmp_dir)
        tmp = Path(name)
        h = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(fd, "wb") as out:
                chunk = head
                while chunk:
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise TooLarge(f"file is larger than {self.max_bytes >> 20} MB")
                    h.update(chunk)
                    out.write(chunk)
                    chunk = src.read(1 << 20)
            os.chmod(tmp, 0o644)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return Staged(tmp, h.hexdigest(), size, media)

    async def commit(
        self,
        staged: Staged,
        *,
        name: str,
        source: str,
        source_ref: str = "",
        caption: str = "",
        stream: str = STREAM_HIGH,
    ) -> IngestResult:
        try:
            existing = await self.pool.fetchval("SELECT id FROM memes WHERE sha256=$1", staged.sha)
            if existing:
                return IngestResult(existing, True, staged.media.kind)
            rel = rel_original(staged.sha, staged.media.ext)
            dst = safe_join(self.upload_dir, rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.move, staged.tmp, dst)
            meme_id = await self.pool.fetchval(
                """INSERT INTO memes (sha256, kind, mime, ext, file_path, size_bytes, original_name, source, source_ref, caption)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT (sha256) DO NOTHING RETURNING id""",
                staged.sha,
                staged.media.kind,
                staged.media.mime,
                staged.media.ext,
                rel,
                staged.size,
                Path(name).name[:255],
                source,
                source_ref[:1000],
                caption[:4000],
            )
            if meme_id is None:
                existing = await self.pool.fetchval("SELECT id FROM memes WHERE sha256=$1", staged.sha)
                return IngestResult(existing, True, staged.media.kind)
            await self.queue.process(meme_id, stream=stream)
            return IngestResult(meme_id, False, staged.media.kind)
        finally:
            staged.tmp.unlink(missing_ok=True)

    async def ingest_path(self, path: Path, **meta) -> IngestResult:
        def _stage() -> Staged:
            with open(path, "rb") as f:
                return self.stage(f)

        staged = await asyncio.to_thread(_stage)
        meta.setdefault("name", path.name)
        return await self.commit(staged, **meta)
