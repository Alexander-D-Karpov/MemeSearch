from __future__ import annotations

import logging
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import asyncpg
import numpy as np

from .storage import rel_thumb, safe_join

log = logging.getLogger(__name__)

BUCKET_LIMIT = 200
MIN_BITS = 24

NEAR_SQL = """SELECT id FROM memes
WHERE id <> $1 AND phash IS NOT NULL AND (kind = 'video') = $2 AND status <> 'failed'
    AND (id < $1 OR analyzed_at IS NOT NULL)
    AND bit_count(phash # $3::text::bit(256)) <= $4
ORDER BY bit_count(phash # $3::text::bit(256)), id LIMIT 1"""


def usable(phash: str | None) -> bool:
    if not phash or len(phash) != 256:
        return False
    ones = phash.count("1")
    return MIN_BITS <= ones <= 256 - MIN_BITS


async def find_near(pool: asyncpg.Pool, meme_id: int, is_video: bool, phash: str, distance: int) -> int | None:
    if not usable(phash):
        return None
    return await pool.fetchval(NEAR_SQL, meme_id, is_video, phash, distance)


def provenance_url(dup: asyncpg.Record) -> str:
    ref = dup["source_ref"] or ""
    if ref.startswith(("http://", "https://")):
        return ref
    return f"{dup['source'] or 'upload'}:{dup['sha256']}"


async def merge(pool: asyncpg.Pool, upload_dir: Path, keeper_id: int, dup_id: int) -> bool:
    async with pool.acquire() as conn, conn.transaction():
        keeper = await conn.fetchrow("SELECT id FROM memes WHERE id = $1 FOR UPDATE", keeper_id)
        dup = await conn.fetchrow(
            """SELECT id, sha256, source, source_ref, caption, file_path, thumb_path, locked
            FROM memes WHERE id = $1 FOR UPDATE""",
            dup_id,
        )
        if keeper is None or dup is None or dup["locked"] or keeper_id == dup_id:
            return False
        await conn.execute("UPDATE meme_sources SET meme_id = $1 WHERE meme_id = $2", keeper_id, dup_id)
        await conn.execute(
            """INSERT INTO meme_sources (meme_id, url, source, caption) VALUES ($1, $2, $3, $4)
            ON CONFLICT (url) DO NOTHING""",
            keeper_id,
            provenance_url(dup),
            dup["source"] or "upload",
            dup["caption"] or "",
        )
        await conn.execute(
            "UPDATE memes SET caption = $2 WHERE id = $1 AND caption = '' AND $2 <> ''", keeper_id, dup["caption"] or ""
        )
        await conn.execute("DELETE FROM memes WHERE id = $1", dup_id)
    for rel in {dup["file_path"], dup["thumb_path"], rel_thumb(dup["sha256"])}:
        if rel:
            try:
                safe_join(upload_dir, rel).unlink(missing_ok=True)
            except (OSError, ValueError) as exc:
                log.warning("could not remove %s of merged meme %s: %s", rel, dup_id, exc)
    return True


VISUAL_SQL = """SELECT a.id AS a, n.id AS b, n.locked, n.sim, a.ocr_text AS a_text, n.ocr_text AS b_text
FROM memes a CROSS JOIN LATERAL (
    SELECT b.id, b.kind, b.status, b.locked, b.ocr_text, 1 - (b.clip_vec <=> a.clip_vec) AS sim
    FROM memes b WHERE b.clip_vec IS NOT NULL ORDER BY b.clip_vec <=> a.clip_vec LIMIT 12
) n
WHERE a.id = ANY($1) AND a.clip_vec IS NOT NULL AND a.kind <> 'video'
    AND n.id <> a.id AND n.kind = a.kind AND n.status = 'done' AND n.sim >= $2"""

WORD = re.compile(r"\w+")


def pct(x: float) -> int:
    return math.floor(x * 100 + 1e-9)


def norm_text(text: str) -> list[str]:
    return WORD.findall(text.lower())


@dataclass(frozen=True)
class TextDiff:
    cover: float
    whole: float
    replaced: int


def text_diff(a: list[str], b: list[str]) -> TextDiff:
    m = SequenceMatcher(None, a, b, autojunk=False)
    same = sum(blk.size for blk in m.get_matching_blocks())
    replaced = sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in m.get_opcodes() if tag == "replace")
    return TextDiff(same / min(len(a), len(b)), same / max(len(a), len(b)), replaced)


@dataclass(frozen=True)
class Verdict:
    sure: bool
    label: str
    weak: float


@dataclass(frozen=True)
class Visual:
    sure_sim: float = 0.97
    ask_sim: float = 0.9
    ask_no_text_sim: float = 0.93
    same_text: float = 0.9
    ask_text: float = 0.5

    @property
    def floor(self) -> float:
        return min(self.sure_sim, self.ask_sim, self.ask_no_text_sim)

    def weak(self, sim: float) -> float:
        return (1 - sim) / max(1 - self.sure_sim, 1e-6)

    def judge(self, sim: float, a_text: str, b_text: str) -> Verdict | None:
        a, b = norm_text(a_text), norm_text(b_text)
        if not a and not b:
            label = f"looks {pct(sim)}% alike, no text"
            if sim >= self.sure_sim:
                return Verdict(True, label, self.weak(sim))
            return Verdict(False, label, self.weak(sim)) if sim >= self.ask_no_text_sim else None
        if not a or not b:
            return Verdict(False, f"looks {pct(sim)}% alike, text on one only", self.weak(sim)) if sim >= self.sure_sim else None
        d = text_diff(a, b)
        label = f"looks {pct(sim)}% alike, text {pct(d.cover)}% shared"
        if sim >= self.sure_sim and d.cover >= self.same_text and d.replaced <= min(len(a), len(b)) // 15:
            return Verdict(True, label, self.weak(sim))
        if sim >= self.ask_sim and d.cover >= self.ask_text:
            return Verdict(False, label, self.weak(sim))
        return None


def visual_settings(s: Any) -> Visual | None:
    if not s.dedup_visual:
        return None
    return Visual(
        sure_sim=s.dedup_visual_sure,
        ask_sim=s.dedup_visual_ask,
        ask_no_text_sim=s.dedup_visual_ask_no_text,
        same_text=s.dedup_text_min,
    )


@dataclass(frozen=True)
class Pair:
    a: int
    b: int
    sim: float
    verdict: Verdict


async def visual_pairs(pool: asyncpg.Pool, ids: list[int], v: Visual) -> list[Pair]:
    out = []
    for r in await pool.fetch(VISUAL_SQL, ids, v.floor):
        if verdict := v.judge(r["sim"], r["a_text"], r["b_text"]):
            out.append(Pair(r["a"], r["b"], r["sim"], verdict))
    return out


async def find_visual(pool: asyncpg.Pool, meme_id: int, v: Visual) -> tuple[int | None, list[Pair]]:
    pairs = [p for p in await visual_pairs(pool, [meme_id], v)]
    sure = [p for p in pairs if p.verdict.sure and p.b < meme_id]
    keeper = max(sure, key=lambda p: p.sim).b if sure else None
    return keeper, [p for p in pairs if not p.verdict.sure]


async def save_candidates(pool: asyncpg.Pool, pairs: list[Pair], fresh: bool = False) -> int:
    best: dict[tuple[int, int], Pair] = {}
    for p in pairs:
        key = (min(p.a, p.b), max(p.a, p.b))
        if key not in best or p.sim > best[key].sim:
            best[key] = p
    async with pool.acquire() as conn, conn.transaction():
        if fresh:
            await conn.execute("DELETE FROM dedupe_candidates WHERE verdict = ''")
        if best:
            await conn.execute(
                """INSERT INTO dedupe_candidates (a, b, sim, label)
                SELECT * FROM unnest($1::bigint[], $2::bigint[], $3::real[], $4::text[])
                ON CONFLICT (a, b) DO NOTHING""",
                [k[0] for k in best],
                [k[1] for k in best],
                [p.sim for p in best.values()],
                [p.verdict.label for p in best.values()],
            )
    return len(best)


def keeper_key(r: Any) -> tuple:
    return (r["locked"] is False, r["hidden"], r["status"] != "done", r["id"])


def groups(rows: list[dict[str, Any]], distance: int, visual: list[Pair] = ()) -> list[dict[str, Any]]:
    if not rows:
        return []
    index = {r["id"]: i for i, r in enumerate(rows)}
    ok = np.array([usable(r["phash"]) for r in rows])
    bits = np.zeros((len(rows), 4), dtype=np.uint64)
    for i, r in enumerate(rows):
        if ok[i]:
            bits[i] = [int(r["phash"][k * 64 : (k + 1) * 64], 2) for k in range(4)]
    parent = list(range(len(rows)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    edges = np.linspace(0, 256, distance + 2).astype(int)
    for a, b in zip(edges[:-1], edges[1:], strict=True):
        buckets: dict[tuple[bool, str], list[int]] = defaultdict(list)
        for i, r in enumerate(rows):
            if ok[i]:
                buckets[(r["video"], r["phash"][a:b])].append(i)
        for members in buckets.values():
            if len(members) < 2 or len(members) > BUCKET_LIMIT:
                continue
            m = np.array(members)
            x = bits[m]
            d = np.bitwise_count(x[:, None, :] ^ x[None, :, :]).sum(-1)
            for i, j in zip(*np.nonzero(np.triu(d <= distance, 1)), strict=True):
                union(int(m[i]), int(m[j]))

    links: dict[tuple[int, int], tuple[str, float]] = {}
    for p in visual:
        if p.verdict.sure and p.a in index and p.b in index:
            ia, ib = index[p.a], index[p.b]
            union(ia, ib)
            if p.verdict.weak < links.get((ia, ib), ("", 2.0))[1]:
                links[(ia, ib)] = links[(ib, ia)] = (p.verdict.label, p.verdict.weak)

    clusters: dict[int, list[int]] = defaultdict(list)
    for i in range(len(rows)):
        clusters[find(i)].append(i)
    out = []
    for members in clusters.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda i: keeper_key(rows[i]))
        keeper = members[0]
        dups = []
        for i in members[1:]:
            if rows[i]["locked"]:
                continue
            hit = None
            if ok[i] and ok[keeper] and rows[i]["video"] == rows[keeper]["video"]:
                dist = int(np.bitwise_count(bits[i] ^ bits[keeper]).sum())
                if dist <= distance:
                    hit = (f"{dist} bits apart", dist / max(distance, 1))
            hit = hit or links.get((keeper, i))
            if hit:
                dups.append((rows[i]["id"], *hit))
        if dups:
            out.append({"keeper": rows[keeper]["id"], "dups": dups})
    return out
