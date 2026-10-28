from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
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


VISUAL_SQL = """SELECT a.id AS a, n.id AS b, n.locked, n.sim, n.tsim, a.ocr_text = '' AS a_empty, n.ocr_text = '' AS b_empty
FROM memes a CROSS JOIN LATERAL (
    SELECT b.id, b.kind, b.status, b.locked, b.ocr_text, 1 - (b.clip_vec <=> a.clip_vec) AS sim,
        similarity(lower(b.ocr_text), lower(a.ocr_text)) AS tsim
    FROM memes b WHERE b.clip_vec IS NOT NULL ORDER BY b.clip_vec <=> a.clip_vec LIMIT 12
) n
WHERE a.id = ANY($1) AND a.clip_vec IS NOT NULL AND a.kind <> 'video'
    AND n.id <> a.id AND n.kind = a.kind AND n.status = 'done' AND n.sim >= $2"""


@dataclass(frozen=True)
class Visual:
    min_sim: float
    no_text_sim: float
    text_sim: float

    def threshold(self, a_empty: bool, b_empty: bool) -> float | None:
        if a_empty and b_empty:
            return self.no_text_sim
        if a_empty or b_empty:
            return None
        return self.min_sim

    def match(self, r: Any) -> float | None:
        need = self.threshold(r["a_empty"], r["b_empty"])
        if need is None or r["sim"] < need or (not r["a_empty"] and r["tsim"] < self.text_sim):
            return None
        return (1 - r["sim"]) / max(1 - need, 1e-6)


def visual_settings(s: Any) -> Visual | None:
    if not s.dedup_visual:
        return None
    return Visual(s.dedup_visual_min, s.dedup_visual_no_text_min, s.dedup_text_min)


async def visual_pairs(pool: asyncpg.Pool, ids: list[int], v: Visual) -> list[tuple[int, int, str, float]]:
    rows = await pool.fetch(VISUAL_SQL, ids, min(v.min_sim, v.no_text_sim))
    out = []
    for r in rows:
        weak = v.match(r)
        if weak is not None:
            out.append((r["a"], r["b"], f"looks {r['sim'] * 100:.0f}% alike", weak))
    return out


async def find_visual(pool: asyncpg.Pool, meme_id: int, v: Visual) -> int | None:
    rows = await pool.fetch(VISUAL_SQL, [meme_id], min(v.min_sim, v.no_text_sim))
    best = [r for r in rows if (r["b"] < meme_id or r["locked"]) and v.match(r) is not None]
    return max(best, key=lambda r: r["sim"])["b"] if best else None


def groups(rows: list[dict[str, Any]], distance: int, visual: list[tuple[int, int, str, float]] = ()) -> list[dict[str, Any]]:
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
    for a, b, label, weak in visual:
        if a in index and b in index:
            ia, ib = index[a], index[b]
            union(ia, ib)
            if weak < links.get((ia, ib), ("", 2.0))[1]:
                links[(ia, ib)] = links[(ib, ia)] = (label, weak)

    clusters: dict[int, list[int]] = defaultdict(list)
    for i in range(len(rows)):
        clusters[find(i)].append(i)
    out = []
    for members in clusters.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda i: (rows[i]["locked"] is False, rows[i]["hidden"], rows[i]["status"] != "done", rows[i]["id"]))
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
