from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

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


def _save_thumb(img: Image.Image, path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    t = img.copy()
    t.thumbnail((size, size * 3), Image.LANCZOS)
    tmp = path.with_suffix(".tmp.webp")
    t.save(tmp, "WEBP", quality=80, method=4)
    tmp.replace(path)
    path.chmod(0o644)
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
    p.thumb = _save_thumb(img, thumb_path, thumb_size)
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
                p.thumb = _save_thumb(img, thumb_path, thumb_size)
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

    if duration <= 0:
        times = [0.0]
    else:
        count = max(1, min(max_frames, int(duration / 1.5) + 1))
        margin = min(0.3, duration * 0.05)
        span = max(0.0, duration - 2 * margin)
        times = [margin + span * i / max(1, count - 1) for i in range(count)] if count > 1 else [duration / 2]
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
        p.thumb = _save_thumb(im.convert("RGB"), thumb_path, thumb_size)
    return p


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
