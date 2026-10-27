from __future__ import annotations

import logging
from collections import defaultdict
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


def groups(rows: list[dict[str, Any]], distance: int) -> list[dict[str, Any]]:
    rows = [r for r in rows if usable(r["phash"])]
    if not rows:
        return []
    bits = np.array([[int(r["phash"][i * 64 : (i + 1) * 64], 2) for i in range(4)] for r in rows], dtype=np.uint64)
    edges = np.linspace(0, 256, distance + 2).astype(int)
    parent = list(range(len(rows)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for a, b in zip(edges[:-1], edges[1:], strict=True):
        buckets: dict[tuple[bool, str], list[int]] = defaultdict(list)
        for i, r in enumerate(rows):
            buckets[(r["video"], r["phash"][a:b])].append(i)
        for members in buckets.values():
            if len(members) < 2 or len(members) > BUCKET_LIMIT:
                continue
            m = np.array(members)
            x = bits[m]
            d = np.bitwise_count(x[:, None, :] ^ x[None, :, :]).sum(-1)
            for i, j in zip(*np.nonzero(np.triu(d <= distance, 1)), strict=True):
                ri, rj = find(int(m[i])), find(int(m[j]))
                if ri != rj:
                    parent[rj] = ri

    clusters: dict[int, list[int]] = defaultdict(list)
    for i in range(len(rows)):
        clusters[find(i)].append(i)
    out = []
    for members in clusters.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda i: (rows[i]["locked"] is False, rows[i]["hidden"], rows[i]["status"] != "done", rows[i]["id"]))
        keeper = members[0]
        dists = np.bitwise_count(bits[members] ^ bits[keeper]).sum(-1)
        dups = [
            (rows[i]["id"], int(dist))
            for i, dist in zip(members[1:], dists[1:], strict=True)
            if dist <= distance and not rows[i]["locked"]
        ]
        if dups:
            out.append({"keeper": rows[keeper]["id"], "dups": dups})
    return out
