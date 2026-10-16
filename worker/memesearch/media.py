from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:
    pass

log = logging.getLogger(__name__)


@dataclass
class Prepared:
    width: int = 0
    height: int = 0
    duration_ms: int = 0
    has_audio: bool = False
    frames: list[Path] = field(default_factory=list)
    frame_times: list[float] = field(default_factory=list)
    thumb: Path | None = None
    phash: str | None = None
    blurhash: str = ""


def _flatten(img: Image.Image) -> Image.Image:
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.getchannel("A"))
        return bg
    return img.convert("RGB")


def _fit(img: Image.Image, max_side: int) -> Image.Image:
    if max(img.size) > max_side:
        img = img.copy()
        img.thumbnail((max_side, max_side), Image.LANCZOS)
    return img


def _save_jpeg(img: Image.Image, path: Path, max_side: int) -> Path:
    _fit(img, max_side).save(path, "JPEG", quality=90, optimize=True)
    return path


def _thumb(img: Image.Image, size: int) -> Image.Image:
    t = img.copy()
    t.thumbnail((size, size * 3), Image.LANCZOS)
    return t


def dhash(img: Image.Image, side: int = 16) -> str:
    g = img.convert("L").resize((side + 1, side), Image.LANCZOS)
    px = g.tobytes()
    w = side + 1
    return "".join("1" if px[y * w + x] > px[y * w + x + 1] else "0" for y in range(side) for x in range(side))


B83 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz#$%*+,-.:;=?@[]^_{|}~"


def _b83(value: int, length: int) -> str:
    return "".join(B83[(value // 83 ** (length - 1 - i)) % 83] for i in range(length))


def _to_srgb(v: float) -> int:
    v = min(max(v, 0.0), 1.0)
    return int(v * 12.92 * 255 + 0.5) if v <= 0.0031308 else int((1.055 * v ** (1 / 2.4) - 0.055) * 255 + 0.5)


def blurhash(img: Image.Image, cx: int = 4, cy: int = 3) -> str:
    small = img.convert("RGB")
    small.thumbnail((32, 32), Image.BILINEAR)
    rgb = np.asarray(small, dtype=np.float64) / 255.0
    lin = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    h, w = lin.shape[:2]
    xs, ys = np.arange(w), np.arange(h)
    factors = []
    for j in range(cy):
        for i in range(cx):
            basis = np.outer(np.cos(np.pi * j * ys / h), np.cos(np.pi * i * xs / w))
            norm = 1.0 if i == 0 and j == 0 else 2.0
            factors.append(norm * (lin * basis[..., None]).sum(axis=(0, 1)) / (w * h))
    dc, ac = factors[0], factors[1:]
    out = _b83((cx - 1) + (cy - 1) * 9, 1)
    if ac:
        qmax = max(0, min(82, int(np.floor(max(np.abs(np.array(ac)).max(), 0) * 166 - 0.5))))
        maximum = (qmax + 1) / 166
    else:
        qmax, maximum = 0, 1.0
    out += _b83(qmax, 1)
    out += _b83((_to_srgb(dc[0]) << 16) + (_to_srgb(dc[1]) << 8) + _to_srgb(dc[2]), 4)
    for f in ac:
        q = [max(0, min(18, int(np.floor(np.sign(v / maximum) * abs(v / maximum) ** 0.5 * 9 + 9.5)))) for v in f]
        out += _b83(q[0] * 361 + q[1] * 19 + q[2], 2)
    return out


def thumb_blurhash(path: Path) -> str:
    with Image.open(path) as im:
        return blurhash(im)


def hamming(a: str, b: str) -> int:
    return sum(x != y for x, y in zip(a, b, strict=True))


def _save_thumb(img: Image.Image, path: Path, size: int, p: Prepared) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    t = _thumb(img, size)
    tmp = path.with_suffix(".tmp.webp")
    t.save(tmp, "WEBP", quality=80, method=4)
    tmp.replace(path)
    path.chmod(0o644)
    p.phash = dhash(t)
    p.blurhash = blurhash(t)
    return path


def ffprobe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True,
        check=True,
        timeout=60,
    )
    return json.loads(out.stdout)


def _video_frame(src: Path, at: float, dst: Path, max_side: int) -> bool:
    scale = f"scale='if(gt(iw,ih),min({max_side},iw),-2)':'if(gt(iw,ih),-2,min({max_side},ih))'"
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{at:.3f}",
        "-i",
        str(src),
        "-frames:v",
        "1",
        "-vf",
        scale,
        "-q:v",
        "3",
        str(dst),
    ]
    res = subprocess.run(cmd, capture_output=True, timeout=120)
    return res.returncode == 0 and dst.exists() and dst.stat().st_size > 0


def prepare_image(src: Path, workdir: Path, thumb_path: Path, max_side: int, thumb_size: int) -> Prepared:
    with Image.open(src) as im:
        img = _flatten(im)
    p = Prepared(width=img.width, height=img.height)
    p.frames.append(_save_jpeg(img, workdir / "frame_00.jpg", max_side))
    p.thumb = _save_thumb(img, thumb_path, thumb_size, p)
    return p


def prepare_gif(src: Path, workdir: Path, thumb_path: Path, max_frames: int, max_side: int, thumb_size: int) -> Prepared:
    with Image.open(src) as im:
        p = Prepared(width=im.width, height=im.height)
        total = min(getattr(im, "n_frames", 1), 5000)
        durations = []
        for i in range(total):
            im.seek(i)
            durations.append(im.info.get("duration", 100) or 100)
        count = min(max_frames, total)
        idx = sorted({round(i * (total - 1) / max(1, count - 1)) for i in range(count)})
        starts = []
        t = 0
        for d in durations:
            starts.append(t / 1000)
            t += d
        for n, i in enumerate(idx):
            im.seek(i)
            img = _flatten(im.copy())
            p.frames.append(_save_jpeg(img, workdir / f"frame_{n:02d}.jpg", max_side))
            p.frame_times.append(starts[i])
            if n == 0:
                p.thumb = _save_thumb(img, thumb_path, thumb_size, p)
    p.duration_ms = int(sum(durations)) if total > 1 else 0
    if not p.frames:
        raise ValueError("gif has no frames")
    return p


def prepare_video(src: Path, workdir: Path, thumb_path: Path, max_frames: int, max_side: int, thumb_size: int) -> Prepared:
    info = ffprobe(src)
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        raise ValueError("no video stream")
    p = Prepared()
    p.width = int(video.get("width") or 0)
    p.height = int(video.get("height") or 0)
    rotation = 0
    for sd in video.get("side_data_list", []) or []:
        if "rotation" in sd:
            rotation = abs(int(sd["rotation"]))
    if rotation in (90, 270):
        p.width, p.height = p.height, p.width
    duration = float(info.get("format", {}).get("duration") or video.get("duration") or 0)
    p.duration_ms = int(duration * 1000)
    p.has_audio = any(s.get("codec_type") == "audio" for s in info.get("streams", []))

    times = video_times(duration, max_frames)
    for n, at in enumerate(times):
        dst = workdir / f"frame_{n:02d}.jpg"
        if _video_frame(src, at, dst, max_side):
            p.frames.append(dst)
            p.frame_times.append(at)
    if not p.frames and _video_frame(src, 0, workdir / "frame_00.jpg", max_side):
        p.frames.append(workdir / "frame_00.jpg")
        p.frame_times.append(0.0)
    if not p.frames:
        raise ValueError("could not extract any frame")
    poster = p.frames[min(1, len(p.frames) - 1)] if duration > 3 else p.frames[0]
    with Image.open(poster) as im:
        p.thumb = _save_thumb(im.convert("RGB"), thumb_path, thumb_size, p)
    return p


FASTSTART_EXTS = {".mp4": "mp4", ".m4v": "mp4", ".mov": "mov"}


def needs_faststart(path: Path) -> bool:
    if path.suffix.lower() not in FASTSTART_EXTS:
        return False
    size = path.stat().st_size
    with path.open("rb") as f:
        pos = 0
        while pos + 8 <= size:
            f.seek(pos)
            head = f.read(16)
            if len(head) < 8:
                return False
            box_size = int.from_bytes(head[:4], "big")
            kind = head[4:8]
            if box_size == 1 and len(head) >= 16:
                box_size = int.from_bytes(head[8:16], "big")
            elif box_size == 0:
                box_size = size - pos
            if kind == b"moov":
                return False
            if kind == b"mdat":
                return True
            if box_size < 8:
                return False
            pos += box_size
    return False


def faststart(path: Path) -> bool:
    if not needs_faststart(path):
        return False
    tmp = path.with_name(path.name + ".faststart.tmp")
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-i",
                str(path),
                "-map",
                "0",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                "-f",
                FASTSTART_EXTS[path.suffix.lower()],
                str(tmp),
            ],
            capture_output=True,
            check=True,
            timeout=600,
        )
        if needs_faststart(tmp) or tmp.stat().st_size < path.stat().st_size // 2:
            raise ValueError("remuxed file looks wrong")
        tmp.chmod(0o644)
        tmp.replace(path)
        return True
    finally:
        tmp.unlink(missing_ok=True)


def video_times(duration: float, max_frames: int) -> list[float]:
    if duration <= 0:
        return [0.0]
    count = max(1, min(max_frames, int(duration / 1.5) + 1))
    if count == 1:
        return [duration / 2]
    margin = min(0.3, duration * 0.05)
    span = max(0.0, duration - 2 * margin)
    return [margin + span * i / (count - 1) for i in range(count)]


def poster_time(duration: float, max_frames: int) -> float:
    times = video_times(duration, max_frames)
    return times[min(1, len(times) - 1)] if duration > 3 else times[0]


def video_duration(src: Path) -> float:
    info = ffprobe(src)
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        raise ValueError("no video stream")
    return float(info.get("format", {}).get("duration") or video.get("duration") or 0)


def content_hash(kind: str, src: Path, workdir: Path, *, max_video_frames: int, frame_max_side: int, thumb_size: int) -> str:
    if kind == "video":
        workdir.mkdir(parents=True, exist_ok=True)
        dst = workdir / "poster.jpg"
        at = poster_time(video_duration(src), max_video_frames)
        if not _video_frame(src, at, dst, frame_max_side) and not _video_frame(src, 0, dst, frame_max_side):
            raise ValueError("could not extract a frame")
        with Image.open(dst) as im:
            return dhash(_thumb(im.convert("RGB"), thumb_size))
    with Image.open(src) as im:
        im.seek(0)
        return dhash(_thumb(_flatten(im.copy() if kind == "gif" else im), thumb_size))


def thumb_hash(path: Path) -> str:
    with Image.open(path) as im:
        return dhash(im.convert("RGB"))


def prepare(
    kind: str,
    src: Path,
    workdir: Path,
    thumb_path: Path,
    *,
    max_video_frames: int,
    max_gif_frames: int,
    analysis_max_side: int,
    frame_max_side: int,
    thumb_size: int,
) -> Prepared:
    workdir.mkdir(parents=True, exist_ok=True)
    if kind == "image":
        return prepare_image(src, workdir, thumb_path, analysis_max_side, thumb_size)
    if kind == "gif":
        return prepare_gif(src, workdir, thumb_path, max_gif_frames, frame_max_side, thumb_size)
    return prepare_video(src, workdir, thumb_path, max_video_frames, frame_max_side, thumb_size)


def extract_audio(src: Path, dst: Path, max_seconds: int) -> bool:
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-t",
        str(max_seconds),
        "-f",
        "wav",
        str(dst),
    ]
    res = subprocess.run(cmd, capture_output=True, timeout=600)
    return res.returncode == 0 and dst.exists() and dst.stat().st_size > 1024


@dataclass
class MediaFix:
    blurhash: str = ""
    phash: str | None = None
    regenerated: bool = False
    remuxed: bool = False


def regenerate_thumb(kind: str, src: Path, thumb_path: Path, size: int, max_frames: int) -> Prepared:
    p = Prepared()
    if kind == "video":
        with tempfile.TemporaryDirectory() as d:
            frame = Path(d) / "poster.jpg"
            at = poster_time(video_duration(src), max_frames)
            if not _video_frame(src, at, frame, 1024) and not _video_frame(src, 0, frame, 1024):
                raise ValueError("could not extract a frame from the video")
            with Image.open(frame) as im:
                _save_thumb(im.convert("RGB"), thumb_path, size, p)
        return p
    with Image.open(src) as im:
        img = _flatten(im)
    _save_thumb(img, thumb_path, size, p)
    return p


def repair_media(kind: str, src: Path, thumb_path: Path, size: int, max_frames: int, need_blurhash: bool) -> MediaFix:
    fix = MediaFix()
    if not thumb_path.is_file():
        if not src.is_file():
            raise FileNotFoundError(f"the original file is missing ({src.name})")
        p = regenerate_thumb(kind, src, thumb_path, size, max_frames)
        fix.regenerated, fix.blurhash, fix.phash = True, p.blurhash, p.phash
    elif need_blurhash:
        fix.blurhash = thumb_blurhash(thumb_path)
    if kind == "video" and src.is_file():
        fix.remuxed = faststart(src)
    return fix
