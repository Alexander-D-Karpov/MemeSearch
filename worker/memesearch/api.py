from __future__ import annotations

import asyncio
import hmac
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncpg
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from openai_codex import AsyncCodex
from pydantic import BaseModel, Field

from .codex import CodexRuntime, cooldown_from_usage, pause_reason, usage_pause_until
from .config import get_settings
from .db import connect
from .embeddings import AudioTagger, Embedder

log = logging.getLogger("memesearch.api")
settings = get_settings()


@dataclass
class LoginState:
    client: AsyncCodex | None
    handle: Any
    login_id: str
    verification_url: str
    user_code: str
    state: str = "pending"
    error: str = ""
    started: float = field(default_factory=time.monotonic)
    task: asyncio.Task | None = None


class State:
    pool: asyncpg.Pool
    runtime: CodexRuntime
    embedder: Embedder | None = None
    embed_error: str = ""
    tagger: AudioTagger | None = None
    logins: dict[int, LoginState]


state = State()


async def load_embedder() -> None:
    try:
        state.embedder = await run_in_threadpool(
            Embedder, settings.clip_model, settings.text_model, settings.embed_threads, settings.clip_dim, settings.text_dim
        )
        log.info("embedding models ready")
    except Exception as exc:
        state.embed_error = str(exc)
        log.exception("failed to load embedding models")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings.ensure_dirs()
    state.pool = await connect(settings.database_url, max_size=5)
    state.runtime = CodexRuntime(settings)
    state.logins = {}
    if settings.audio_tag_model:
        state.tagger = AudioTagger(settings.audio_tag_model, settings.embed_threads, settings.audio_tag_seconds)
    loader = asyncio.create_task(load_embedder())
    yield
    loader.cancel()
    for login in state.logins.values():
        await _close_login(login)
    await state.pool.close()


app = FastAPI(title="memesearch-ml", lifespan=lifespan, docs_url=None, redoc_url=None)


def require_token(x_internal_token: str = Header(default="")) -> None:
    if not hmac.compare_digest(x_internal_token, settings.internal_token):
        raise HTTPException(status_code=401, detail="bad internal token")


def need_embedder() -> Embedder:
    if state.embedder is None:
        raise HTTPException(status_code=503, detail=state.embed_error or "models are loading")
    return state.embedder


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "models": state.embedder is not None, "error": state.embed_error}


class QueryIn(BaseModel):
    q: str = Field(max_length=500)


class MemeIn(BaseModel):
    image_paths: list[str] = Field(max_length=32)
    text: str = Field(default="", max_length=20000)


def _allowed(path: str) -> Path:
    p = Path(path).resolve()
    for root in (settings.work_dir.resolve(), settings.upload_dir.resolve()):
        if p.is_relative_to(root) and p.is_file():
            return p
    raise HTTPException(status_code=400, detail=f"path not allowed: {path}")


@app.post("/embed/query", dependencies=[Depends(require_token)])
async def embed_query(body: QueryIn) -> dict[str, list[float]]:
    emb = need_embedder()
    q = " ".join(body.q.split())
    clip, text = await asyncio.gather(run_in_threadpool(emb.clip_text, q), run_in_threadpool(emb.text_query, q))
    return {"clip": clip.tolist(), "text": text.tolist()}


@app.post("/embed/meme", dependencies=[Depends(require_token)])
async def embed_meme(body: MemeIn) -> dict[str, list[float] | None]:
    emb = need_embedder()
    paths = [_allowed(p) for p in body.image_paths]
    clip = await run_in_threadpool(emb.clip_images, paths) if paths else None
    text = await run_in_threadpool(emb.text_passage, body.text) if body.text.strip() else None
    return {"clip": clip.tolist() if clip is not None else None, "text": text.tolist() if text is not None else None}


MAX_IMAGE_BYTES = 20 << 20


@app.post("/embed/image", dependencies=[Depends(require_token)])
async def embed_image(request: Request) -> dict[str, list[float]]:
    emb = need_embedder()
    data = await request.body()
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413 if data else 400, detail="send an image up to 20 MB as the request body")
    try:
        clip = await run_in_threadpool(emb.clip_image_bytes, data)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=415, detail=f"not a readable image: {exc}") from exc
    return {"clip": clip.tolist()}


class AudioIn(BaseModel):
    path: str


@app.post("/audio/tags", dependencies=[Depends(require_token)])
async def audio_tags(body: AudioIn) -> dict[str, Any]:
    if state.tagger is None:
        return {"tags": [], "enabled": False}
    path = _allowed(body.path)
    try:
        tags = await run_in_threadpool(state.tagger.tag, path)
    except Exception as exc:
        log.warning("audio tagging failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"audio tagging failed: {exc}") from exc
    return {"tags": tags, "enabled": True}


class SessionIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)


async def _session(sid: int) -> asyncpg.Record:
    row = await state.pool.fetchrow("SELECT * FROM codex_sessions WHERE id=$1", sid)
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    return row


async def _close_login(login: LoginState) -> None:
    if login.task and not login.task.done() and login.task is not asyncio.current_task():
        login.task.cancel()
    if login.client is not None:
        try:
            await login.client.close()
        except Exception:
            pass
        login.client = None


async def _refresh(sid: int) -> dict[str, Any]:
    try:
        info = await state.runtime.account_info(sid)
    except Exception as exc:
        await state.pool.execute(
            "UPDATE codex_sessions SET status='error', last_error=$2, last_check_at=now(), updated_at=now() WHERE id=$1",
            sid,
            str(exc)[:1000],
        )
        raise HTTPException(status_code=502, detail=f"codex check failed: {exc}") from exc
    if not info["logged_in"]:
        await state.pool.execute(
            "UPDATE codex_sessions SET status='logged_out', last_check_at=now(), updated_at=now() WHERE id=$1", sid
        )
        return {"status": "logged_out"}
    usage = info["usage"] or {}
    threshold = settings.codex_max_usage_percent
    if usage_pause_until(usage, threshold):
        until = cooldown_from_usage(usage, settings.codex_default_cooldown_minutes, threshold)
        await state.pool.execute(
            """UPDATE codex_sessions SET status='limited', cooldown_until=$5, email=$2, plan=$3, usage=$4,
            last_error=$6, last_check_at=now(), updated_at=now() WHERE id=$1""",
            sid,
            info["email"],
            str(info["plan"]),
            usage,
            until,
            pause_reason(usage, threshold),
        )
        status = "limited"
    else:
        await state.pool.execute(
            """UPDATE codex_sessions SET status='ok', last_error='', cooldown_until=NULL, email=$2, plan=$3, usage=$4,
            last_check_at=now(), updated_at=now() WHERE id=$1""",
            sid,
            info["email"],
            str(info["plan"]),
            usage,
        )
        status = "ok"
    return {"status": status, "email": info["email"], "plan": str(info["plan"]), "usage": usage}


@app.post("/codex/sessions", dependencies=[Depends(require_token)])
async def create_session(body: SessionIn) -> dict[str, Any]:
    sid = await state.pool.fetchval("INSERT INTO codex_sessions(name) VALUES ($1) RETURNING id", body.name.strip())
    state.runtime.write_config(sid)
    return {"id": sid}


@app.delete("/codex/sessions/{sid}", dependencies=[Depends(require_token)])
async def delete_session(sid: int) -> dict[str, Any]:
    await _session(sid)
    login = state.logins.pop(sid, None)
    if login:
        await _close_login(login)
    if state.runtime.has_auth(sid):
        try:
            async with state.runtime.session(sid) as codex:
                await asyncio.wait_for(codex.logout(), timeout=30)
        except Exception as exc:
            log.warning("logout during delete failed: %s", exc)
    state.runtime.remove_home(sid)
    await state.pool.execute("DELETE FROM codex_sessions WHERE id=$1", sid)
    return {"deleted": sid}


async def _wait_login(sid: int, login: LoginState) -> None:
    try:
        done = await asyncio.wait_for(login.handle.wait(), timeout=settings.codex_login_timeout_seconds)
        if not getattr(done, "success", False):
            login.state = "failed"
            login.error = getattr(done, "error", None) or "login was not completed"
            await state.pool.execute(
                "UPDATE codex_sessions SET status='error', last_error=$2, updated_at=now() WHERE id=$1", sid, login.error
            )
            return
        await _close_login(login)
        await _refresh(sid)
        login.state = "done"
    except asyncio.TimeoutError:
        login.state = "failed"
        login.error = "device code expired"
        await state.pool.execute(
            "UPDATE codex_sessions SET status=CASE WHEN status='login_pending' THEN 'new' ELSE status END, updated_at=now() WHERE id=$1",
            sid,
        )
    except asyncio.CancelledError:
        login.state = "failed"
        login.error = "cancelled"
        raise
    except Exception as exc:
        login.state = "failed"
        login.error = str(exc)
        log.exception("login %s failed", sid)
    finally:
        await _close_login(login)


@app.post("/codex/sessions/{sid}/login", dependencies=[Depends(require_token)])
async def start_login(sid: int) -> dict[str, Any]:
    await _session(sid)
    existing = state.logins.get(sid)
    if existing and existing.state == "pending":
        return _login_view(existing)
    if existing:
        await _close_login(existing)
    client = state.runtime.client(sid)
    try:
        await client.__aenter__()
        handle = await client.login_chatgpt_device_code()
    except Exception as exc:
        await client.close()
        detail = f"could not start device login: {exc}"
        if "403" in str(exc) and not (settings.openai_proxy or settings.codex_https_proxy or settings.codex_all_proxy):
            detail += ". OpenAI refused the request, usually because the server's country is blocked. Set OPENAI_PROXY in .env to a proxy outside the blocked region and restart ml and worker."
        raise HTTPException(status_code=502, detail=detail) from exc
    login = LoginState(
        client=client,
        handle=handle,
        login_id=handle.login_id,
        verification_url=handle.verification_url,
        user_code=handle.user_code,
    )
    state.logins[sid] = login
    await state.pool.execute("UPDATE codex_sessions SET status='login_pending', updated_at=now() WHERE id=$1", sid)
    login.task = asyncio.create_task(_wait_login(sid, login))
    return _login_view(login)


def _login_view(login: LoginState | None) -> dict[str, Any]:
    if login is None:
        return {"state": "none"}
    return {
        "state": login.state,
        "verification_url": login.verification_url,
        "user_code": login.user_code,
        "error": login.error,
        "age_seconds": int(time.monotonic() - login.started),
    }


@app.get("/codex/sessions/{sid}/login", dependencies=[Depends(require_token)])
async def login_status(sid: int) -> dict[str, Any]:
    return _login_view(state.logins.get(sid))


@app.delete("/codex/sessions/{sid}/login", dependencies=[Depends(require_token)])
async def cancel_login(sid: int) -> dict[str, Any]:
    login = state.logins.pop(sid, None)
    if login:
        if login.client is not None and login.state == "pending":
            try:
                await login.handle.cancel()
            except Exception:
                pass
        await _close_login(login)
        await state.pool.execute(
            "UPDATE codex_sessions SET status=CASE WHEN status='login_pending' THEN 'new' ELSE status END, updated_at=now() WHERE id=$1",
            sid,
        )
    return {"state": "none"}


@app.post("/codex/sessions/{sid}/auth", dependencies=[Depends(require_token)])
async def import_auth(sid: int, request: Request) -> dict[str, Any]:
    await _session(sid)
    data = await request.body()
    try:
        state.runtime.import_auth(sid, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await _refresh(sid)


@app.post("/codex/sessions/{sid}/check", dependencies=[Depends(require_token)])
async def check(sid: int) -> dict[str, Any]:
    await _session(sid)
    if not state.runtime.has_auth(sid):
        await state.pool.execute(
            "UPDATE codex_sessions SET status=CASE WHEN status='login_pending' THEN status ELSE 'new' END, last_check_at=now() WHERE id=$1",
            sid,
        )
        return {"status": "new"}
    return await _refresh(sid)


@app.post("/codex/sessions/{sid}/logout", dependencies=[Depends(require_token)])
async def logout(sid: int) -> dict[str, Any]:
    await _session(sid)
    if state.runtime.has_auth(sid):
        try:
            async with state.runtime.session(sid) as codex:
                await asyncio.wait_for(codex.logout(), timeout=30)
        except Exception as exc:
            log.warning("codex logout failed: %s", exc)
        state.runtime.auth_path(sid).unlink(missing_ok=True)
    await state.pool.execute(
        "UPDATE codex_sessions SET status='logged_out', email='', plan='', usage='{}', cooldown_until=NULL, updated_at=now() WHERE id=$1",
        sid,
    )
    return {"status": "logged_out"}
