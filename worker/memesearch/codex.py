from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import asyncpg
from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, LocalImageInput, TextInput
from openai_codex.generated.v2_all import GetAccountRateLimitsResponse

from .config import Settings
from .prompt import SCHEMA, SYSTEM, parse_response

log = logging.getLogger(__name__)

LIMIT_MARKERS = (
    "usage limit",
    "usagelimit",
    "rate limit",
    "ratelimit",
    "rate_limit",
    "too many requests",
    "429",
    "quota",
    "limit reached",
    "try again at",
    "usage_limit",
)
AUTH_MARKERS = (
    "401",
    "unauthorized",
    "not logged in",
    "log in",
    "login required",
    "refresh token",
    "token expired",
    "invalid_grant",
    "authentication",
    "requiresopenaiauth",
    "sign in",
)


class CodexUnavailable(Exception):
    def __init__(self, message: str, retry_at: datetime | None = None) -> None:
        super().__init__(message)
        self.retry_at = retry_at


class CodexFailed(Exception):
    pass


def classify(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc} {getattr(exc, 'data', '')}".lower()
    if any(m in text for m in LIMIT_MARKERS):
        return "limit"
    if any(m in text for m in AUTH_MARKERS):
        return "auth"
    return "other"


class CodexRuntime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def home(self, session_id: int) -> Path:
        p = self.settings.codex_dir / str(session_id)
        p.mkdir(parents=True, exist_ok=True)
        os.chmod(p, 0o700)
        return p

    def auth_path(self, session_id: int) -> Path:
        return self.home(session_id) / "auth.json"

    def has_auth(self, session_id: int) -> bool:
        return (self.settings.codex_dir / str(session_id) / "auth.json").exists()

    def remove_home(self, session_id: int) -> None:
        shutil.rmtree(self.settings.codex_dir / str(session_id), ignore_errors=True)

    def env(self, session_id: int) -> dict[str, str]:
        allowed = {
            "PATH",
            "USER",
            "LOGNAME",
            "LANG",
            "LANGUAGE",
            "LC_ALL",
            "LC_CTYPE",
            "TERM",
            "TMPDIR",
            "TZ",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "REQUESTS_CA_BUNDLE",
        }
        env = {k: v for k, v in os.environ.items() if k in allowed}
        home = self.home(session_id)
        env["HOME"] = str(home)
        env["CODEX_HOME"] = str(home)
        env["NO_PROXY"] = env["no_proxy"] = self.settings.no_proxy
        for key, value in (
            ("HTTP_PROXY", self.settings.codex_http_proxy),
            ("HTTPS_PROXY", self.settings.codex_https_proxy),
            ("ALL_PROXY", self.settings.codex_all_proxy),
        ):
            if value:
                env[key] = env[key.lower()] = value
        return env

    def write_config(self, session_id: int) -> None:
        work = json.dumps(str(self.settings.work_dir.resolve()))
        lines = [
            'cli_auth_credentials_store = "file"',
            'history.persistence = "none"',
            'web_search = "disabled"',
            "check_for_update_on_startup = false",
            "allow_login_shell = false",
            'default_permissions = "memesearch"',
            "",
            "[shell_environment_policy]",
            'inherit = "core"',
            "ignore_default_excludes = false",
            "",
            "[permissions.memesearch]",
            'extends = ":read-only"',
            "",
            "[permissions.memesearch.filesystem]",
            '":root" = "deny"',
            '":minimal" = "read"',
            f'{work} = "read"',
            "",
            "[permissions.memesearch.network]",
            "enabled = false",
            "",
        ]
        path = self.home(session_id) / "config.toml"
        body = "\n".join(lines)
        if path.exists() and path.read_text(encoding="utf-8") == body:
            return
        tmp = path.with_suffix(".tmp")
        tmp.write_text(body, encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def client(self, session_id: int) -> AsyncCodex:
        self.write_config(session_id)
        return AsyncCodex(CodexConfig(env=self.env(session_id)))

    def import_auth(self, session_id: int, data: bytes) -> None:
        if len(data) > 2 * 1024 * 1024:
            raise ValueError("auth.json is too large")
        parsed = json.loads(data)
        if not isinstance(parsed, dict) or not parsed:
            raise ValueError("auth.json must be a JSON object")
        dest = self.auth_path(session_id)
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)

    @staticmethod
    async def rate_limits(codex: AsyncCodex) -> dict[str, Any] | None:
        try:
            await codex._ensure_initialized()
            resp = await codex._client.request("account/rateLimits/read", None, response_model=GetAccountRateLimitsResponse)
            return resp.model_dump(by_alias=True, mode="json", exclude_none=True)
        except Exception as exc:
            log.debug("rate limit read failed: %s", exc)
            return None

    async def account_info(self, session_id: int) -> dict[str, Any]:
        async with self.client(session_id) as codex:
            acc = await codex.account(refresh_token=True)
            info: dict[str, Any] = {"logged_in": False, "email": "", "plan": "", "usage": {}}
            root = getattr(acc.account, "root", None) if acc.account is not None else None
            if root is not None:
                info["logged_in"] = True
                info["email"] = getattr(root, "email", "") or ""
                plan = getattr(root, "plan_type", "")
                info["plan"] = getattr(plan, "value", plan) or getattr(root, "type", "")
            if info["logged_in"]:
                info["usage"] = await self.rate_limits(codex) or {}
            return info


def cooldown_from_usage(usage: dict[str, Any] | None, default_minutes: int) -> datetime:
    now = datetime.now(timezone.utc)
    best: datetime | None = None
    rl = (usage or {}).get("rateLimits") or {}
    for key in ("primary", "secondary"):
        w = rl.get(key) or {}
        if (w.get("usedPercent") or 0) >= 100 and w.get("resetsAt"):
            at = datetime.fromtimestamp(int(w["resetsAt"]), timezone.utc)
            if at > now and (best is None or at > best):
                best = at
    if best is None:
        for key in ("primary", "secondary"):
            w = rl.get(key) or {}
            if w.get("resetsAt") and (w.get("usedPercent") or 0) >= 95:
                at = datetime.fromtimestamp(int(w["resetsAt"]), timezone.utc)
                if at > now:
                    best = at
                    break
    return best or now + timedelta(minutes=default_minutes)


@dataclass
class CodexResult:
    fields: dict[str, Any]
    model: str
    session_id: int


class CodexPool:
    def __init__(self, settings: Settings, pool: asyncpg.Pool, runtime: CodexRuntime) -> None:
        self.settings = settings
        self.pool = pool
        self.runtime = runtime
        self.sem = asyncio.Semaphore(max(1, settings.codex_concurrency))
        self.inflight: dict[int, int] = {}

    async def _candidates(self, exclude: set[int]) -> tuple[list[asyncpg.Record], datetime | None]:
        rows = await self.pool.fetch(
            """SELECT id, name, status, cooldown_until FROM codex_sessions
            WHERE enabled AND status IN ('new','ok','limited')
            ORDER BY priority DESC, last_used_at NULLS FIRST, id"""
        )
        now = datetime.now(timezone.utc)
        ready, earliest = [], None
        for r in rows:
            if r["id"] in exclude or not self.runtime.has_auth(r["id"]):
                continue
            if r["cooldown_until"] and r["cooldown_until"] > now:
                if earliest is None or r["cooldown_until"] < earliest:
                    earliest = r["cooldown_until"]
                continue
            ready.append(r)
        ready.sort(key=lambda r: self.inflight.get(r["id"], 0))
        return ready, earliest

    async def analyze(self, prompt: str, images: list[Path], workdir: Path, model: str, effort: str) -> CodexResult:
        async with self.sem:
            tried: set[int] = set()
            last_error = "no Codex session is logged in"
            while True:
                ready, earliest = await self._candidates(tried)
                ready = [r for r in ready if self.inflight.get(r["id"], 0) < self.settings.codex_per_session]
                if not ready:
                    if tried or earliest:
                        raise CodexUnavailable(last_error, earliest)
                    raise CodexUnavailable(last_error, None)
                sess = ready[0]
                sid = sess["id"]
                tried.add(sid)
                self.inflight[sid] = self.inflight.get(sid, 0) + 1
                try:
                    fields, used_model = await self._run(sid, prompt, images, workdir, model, effort)
                except Exception as exc:
                    kind = classify(exc)
                    last_error = f"{sess['name']}: {exc}"[:1000]
                    log.warning("codex session %s failed (%s): %s", sid, kind, exc)
                    if kind == "limit":
                        await self._mark_limited(sid, str(exc))
                        continue
                    if kind == "auth":
                        await self.pool.execute(
                            "UPDATE codex_sessions SET status='error', last_error=$2, fail_count=fail_count+1, updated_at=now() WHERE id=$1",
                            sid,
                            last_error,
                        )
                        continue
                    await self.pool.execute(
                        "UPDATE codex_sessions SET last_error=$2, fail_count=fail_count+1, last_used_at=now(), updated_at=now() WHERE id=$1",
                        sid,
                        last_error,
                    )
                    raise CodexFailed(last_error) from exc
                finally:
                    self.inflight[sid] -= 1
                await self.pool.execute(
                    """UPDATE codex_sessions SET status='ok', last_error='', cooldown_until=NULL,
                    ok_count=ok_count+1, last_used_at=now(), updated_at=now() WHERE id=$1""",
                    sid,
                )
                return CodexResult(fields, used_model, sid)

    async def _mark_limited(self, sid: int, message: str) -> None:
        usage = None
        try:
            async with self.runtime.client(sid) as codex:
                usage = await self.runtime.rate_limits(codex)
        except Exception:
            pass
        until = cooldown_from_usage(usage, self.settings.codex_default_cooldown_minutes)
        await self.pool.execute(
            """UPDATE codex_sessions SET status='limited', cooldown_until=$2, last_error=$3,
            usage=COALESCE($4::jsonb, usage), fail_count=fail_count+1, updated_at=now() WHERE id=$1""",
            sid,
            until,
            message[:1000],
            usage,
        )
        log.info("codex session %s limited until %s", sid, until.isoformat())

    async def _run(
        self, sid: int, prompt: str, images: list[Path], workdir: Path, model: str, effort: str
    ) -> tuple[dict[str, Any], str]:
        started = time.monotonic()
        async with self.runtime.client(sid) as codex:
            kwargs: dict[str, Any] = {
                "approval_mode": ApprovalMode.deny_all,
                "developer_instructions": SYSTEM,
                "ephemeral": True,
                "cwd": str(workdir),
                "config": {"web_search": "disabled", "model_reasoning_effort": effort or self.settings.codex_reasoning_effort},
            }
            if model:
                kwargs["model"] = model
            thread = await codex.thread_start(**kwargs)
            inputs: list[Any] = [TextInput(text=prompt)]
            inputs += [LocalImageInput(path=str(p)) for p in images]
            result = await asyncio.wait_for(
                thread.run(inputs, output_schema=SCHEMA, approval_mode=ApprovalMode.deny_all),
                timeout=self.settings.codex_timeout_seconds,
            )
        fields = parse_response(result.final_response)
        log.info("codex session %s analyzed in %.1fs", sid, time.monotonic() - started)
        return fields, model or "codex-default"
