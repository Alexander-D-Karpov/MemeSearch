package search

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"github.com/jackc/pgx/v5"
	"log/slog"
	"strings"
	"time"

	"github.com/pgvector/pgvector-go"
	"github.com/redis/go-redis/v9"

	"github.com/Alexander-D-Karpov/MemeSearch/internal/mlclient"
	"github.com/Alexander-D-Karpov/MemeSearch/internal/queue"
	"github.com/Alexander-D-Karpov/MemeSearch/internal/store"
)

type Service struct {
	store    *store.Store
	ml       *mlclient.Client
	rdb      *redis.Client
	q        *queue.Queue
	cacheTTL time.Duration
	textMax  float64
	clipMax  float64
	lookMax  float64
}

func New(s *store.Store, ml *mlclient.Client, rdb *redis.Client, q *queue.Queue, cacheTTL time.Duration, textMax, clipMax, lookMax float64) *Service {
	return &Service{store: s, ml: ml, rdb: rdb, q: q, cacheTTL: cacheTTL, textMax: textMax, clipMax: clipMax, lookMax: lookMax}
}

type Hit struct {
	ID    int64   `json:"id"`
	Score float64 `json:"score"`
}

type Result struct {
	Memes    []*store.Meme `json:"memes"`
	Total    int           `json:"total"`
	Semantic bool          `json:"semantic"`
	TookMs   int64         `json:"took_ms"`
}

type cached struct {
	Hits     []Hit `json:"h"`
	Semantic bool  `json:"s"`
}

const candidates = 240

const hybridSQL = `
WITH tq AS (
	SELECT websearch_to_tsquery('simple', $1) || websearch_to_tsquery('russian', $1) || websearch_to_tsquery('english', $1) AS q
),
fts AS (
	SELECT m.id, row_number() OVER (ORDER BY ts_rank_cd(m.tsv, tq.q, 32) DESC, m.id DESC) AS r
	FROM memes m, tq
	WHERE m.tsv @@ tq.q AND m.status = 'done' AND NOT m.hidden AND ($4 = '' OR m.kind = $4)
	ORDER BY ts_rank_cd(m.tsv, tq.q, 32) DESC, m.id DESC
	LIMIT $5
),
trg AS (
	SELECT id, row_number() OVER (ORDER BY sim DESC, id DESC) AS r
	FROM (
		SELECT m.id, word_similarity($1, m.search_text) AS sim
		FROM memes m
		WHERE $1 <% m.search_text AND m.status = 'done' AND NOT m.hidden AND ($4 = '' OR m.kind = $4)
		ORDER BY sim DESC, m.id DESC
		LIMIT $5
	) s
),
tv AS (
	SELECT id, row_number() OVER (ORDER BY d) AS r
	FROM (
		SELECT id, text_vec <=> $2 AS d FROM memes
		WHERE $2::vector IS NOT NULL AND text_vec IS NOT NULL
			AND status = 'done' AND NOT hidden AND ($4 = '' OR kind = $4)
		ORDER BY text_vec <=> $2 LIMIT $5
	) s
	WHERE d < $6
),
cv AS (
	SELECT id, row_number() OVER (ORDER BY d) AS r
	FROM (
		SELECT id, clip_vec <=> $3 AS d FROM memes
		WHERE $3::vector IS NOT NULL AND clip_vec IS NOT NULL
			AND status = 'done' AND NOT hidden AND ($4 = '' OR kind = $4)
		ORDER BY clip_vec <=> $3 LIMIT $5
	) s
	WHERE d < $7
),
fused AS (
	SELECT id, sum(w / (60.0 + r)) AS score
	FROM (
		SELECT id, r, 1.3 AS w FROM fts
		UNION ALL SELECT id, r, 1.0 FROM trg
		UNION ALL SELECT id, r, 0.9 FROM tv
		UNION ALL SELECT id, r, 0.9 FROM cv
	) x
	GROUP BY id
)
SELECT f.id, f.score
FROM fused f JOIN memes m ON m.id = f.id
WHERE m.status = 'done' AND NOT m.hidden AND ($4 = '' OR m.kind = $4)
ORDER BY f.score DESC, f.id DESC
LIMIT $5`

func (s *Service) Search(ctx context.Context, query, kind string, limit, offset int) (*Result, error) {
	start := time.Now()
	query = strings.TrimSpace(query)
	if r := []rune(query); len(r) > 200 {
		query = string(r[:200])
	}
	query = strings.ToValidUTF8(query, "")
	norm := mlclient.NormalizeQuery(query)
	sum := sha256.Sum256([]byte(kind + "\x00" + norm))
	key := fmt.Sprintf("ms:search:%d:%s", s.q.Version(ctx), hex.EncodeToString(sum[:16]))

	var c cached
	if raw, err := s.rdb.Get(ctx, key).Bytes(); err != nil || json.Unmarshal(raw, &c) != nil {
		c, err = s.compute(ctx, norm, kind)
		if err != nil {
			return nil, err
		}
		ttl := s.cacheTTL
		if !c.Semantic {
			ttl = 30 * time.Second
		}
		if raw, err := json.Marshal(c); err == nil {
			s.rdb.Set(ctx, key, raw, ttl)
		}
	}

	res := &Result{Total: len(c.Hits), Semantic: c.Semantic}
	if offset < len(c.Hits) {
		page := c.Hits[offset:min(len(c.Hits), offset+limit)]
		ids := make([]int64, len(page))
		scores := make(map[int64]float64, len(page))
		for i, h := range page {
			ids[i] = h.ID
			scores[h.ID] = h.Score
		}
		memes, err := s.store.MemesByIDs(ctx, ids)
		if err != nil {
			return nil, err
		}
		for _, m := range memes {
			m.Score = scores[m.ID]
		}
		res.Memes = memes
	}
	res.TookMs = time.Since(start).Milliseconds()
	return res, nil
}

func (s *Service) compute(ctx context.Context, query, kind string) (cached, error) {
	var clip, text *pgvector.Vector
	semantic := false
	if vec, err := s.ml.EmbedQuery(ctx, query); err != nil {
		slog.Warn("query embedding unavailable, lexical only", "err", err)
	} else {
		if len(vec.Clip) > 0 {
			v := pgvector.NewVector(vec.Clip)
			clip = &v
		}
		if len(vec.Text) > 0 {
			v := pgvector.NewVector(vec.Text)
			text = &v
		}
		semantic = clip != nil || text != nil
	}
	rows, err := s.store.Pool.Query(ctx, hybridSQL, query, text, clip, kind, candidates, s.textMax, s.clipMax)
	if err != nil {
		return cached{}, err
	}
	defer rows.Close()
	var hits []Hit
	for rows.Next() {
		var h Hit
		if err := rows.Scan(&h.ID, &h.Score); err != nil {
			return cached{}, err
		}
		hits = append(hits, h)
	}
	return cached{Hits: hits, Semantic: semantic}, rows.Err()
}

const (
	similarMaxTagShare      = 0.03
	similarSameTemplateDist = 0.15
)

const similarSQL = `
WITH me AS (
	SELECT tags, people, lower(template) AS tpl FROM memes WHERE id = $1
),
total AS (
	SELECT greatest(count(*), 1)::float8 AS n FROM memes WHERE status = 'done' AND NOT hidden
),
idf AS (
	SELECT ts.tag, ln((SELECT n FROM total) / ts.n) AS w
	FROM tag_stats ts, me
	WHERE ts.tag = ANY(me.tags) AND ts.n > 1 AND ts.n <= (SELECT n FROM total) * $6
),
tg AS (
	SELECT id, row_number() OVER (ORDER BY s DESC, id DESC) AS r FROM (
		SELECT m.id, sum(idf.w) AS s
		FROM memes m JOIN idf ON idf.tag = ANY(m.tags)
		WHERE m.tags && (SELECT coalesce(array_agg(tag), '{}') FROM idf)
			AND m.id <> $1 AND m.status = 'done' AND NOT m.hidden
		GROUP BY m.id ORDER BY s DESC LIMIT $4
	) x
),
tp AS (
	SELECT id, row_number() OVER (ORDER BY s DESC, id DESC) AS r FROM (
		SELECT m.id,
			(CASE WHEN me.tpl <> '' AND lower(m.template) = me.tpl THEN 2 ELSE 0 END)
			+ cardinality(ARRAY(SELECT unnest(m.people) INTERSECT SELECT unnest(me.people))) AS s
		FROM memes m, me
		WHERE m.id <> $1 AND m.status = 'done' AND NOT m.hidden
			AND ((me.tpl <> '' AND lower(m.template) = me.tpl AND m.template <> '')
				OR (cardinality(me.people) > 0 AND m.people && me.people))
		ORDER BY s DESC LIMIT $4
	) x
),
cv AS (
	SELECT id, d, row_number() OVER (ORDER BY d) AS r
	FROM (SELECT id, clip_vec <=> $2 AS d FROM memes
		WHERE $2::vector IS NOT NULL AND clip_vec IS NOT NULL AND status = 'done' AND NOT hidden
		ORDER BY clip_vec <=> $2 LIMIT $4) s
),
tv AS (
	SELECT id, row_number() OVER (ORDER BY d) AS r
	FROM (SELECT id, text_vec <=> $3 AS d FROM memes
		WHERE $3::vector IS NOT NULL AND text_vec IS NOT NULL AND status = 'done' AND NOT hidden
		ORDER BY text_vec <=> $3 LIMIT $4) s
),
fused AS (
	SELECT id, sum(w / (30.0 + r)) AS score FROM (
		SELECT id, r, 1.0 AS w FROM tv
		UNION ALL SELECT id, r, 1.3 FROM tg
		UNION ALL SELECT id, r, 1.2 FROM tp
		UNION ALL SELECT id, r, CASE WHEN d < $7 THEN 1.6 WHEN d <= $8 THEN 1.1 ELSE 0.3 END FROM cv
	) x GROUP BY id
)
SELECT f.id, f.score FROM fused f JOIN memes m ON m.id = f.id
WHERE f.id <> $1 AND m.status = 'done' AND NOT m.hidden
ORDER BY f.score DESC LIMIT $5`

func (s *Service) Similar(ctx context.Context, id int64, limit int) ([]*store.Meme, error) {
	key := fmt.Sprintf("ms:similar:%d:%d:%d", s.q.Version(ctx), id, limit)
	var ids []int64
	if raw, err := s.rdb.Get(ctx, key).Bytes(); err == nil && json.Unmarshal(raw, &ids) == nil {
		return s.store.MemesByIDs(ctx, ids)
	}
	clip, text, err := s.store.Vectors(ctx, id)
	if err != nil {
		return nil, err
	}
	if clip == nil && text == nil {
		return nil, nil
	}
	rows, err := s.store.Pool.Query(ctx, similarSQL, id, clip, text, limit*4, limit, similarMaxTagShare, similarSameTemplateDist, s.lookMax)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var mid int64
		var score float64
		if err := rows.Scan(&mid, &score); err != nil {
			return nil, err
		}
		ids = append(ids, mid)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	if raw, err := json.Marshal(ids); err == nil {
		s.rdb.Set(ctx, key, raw, time.Hour)
	}
	return s.store.MemesByIDs(ctx, ids)
}

const lookAlikeSQL = `
SELECT id FROM (
	SELECT id, clip_vec <=> $2 AS d FROM memes
	WHERE clip_vec IS NOT NULL AND status = 'done' AND NOT hidden AND id <> $1
	ORDER BY clip_vec <=> $2 LIMIT $3
) s WHERE d <= $4 ORDER BY d`

func (s *Service) LookAlike(ctx context.Context, id int64, limit int) ([]*store.Meme, error) {
	key := fmt.Sprintf("ms:lookalike:%d:%d:%d", s.q.Version(ctx), id, limit)
	var ids []int64
	if raw, err := s.rdb.Get(ctx, key).Bytes(); err == nil && json.Unmarshal(raw, &ids) == nil {
		return s.store.MemesByIDs(ctx, ids)
	}
	clip, _, err := s.store.Vectors(ctx, id)
	if err != nil || clip == nil {
		return nil, err
	}
	rows, err := s.store.Pool.Query(ctx, lookAlikeSQL, id, clip, limit, s.lookMax)
	if err != nil {
		return nil, err
	}
	ids, err = pgx.CollectRows(rows, pgx.RowTo[int64])
	if err != nil {
		return nil, err
	}
	if raw, err := json.Marshal(ids); err == nil {
		s.rdb.Set(ctx, key, raw, time.Hour)
	}
	return s.store.MemesByIDs(ctx, ids)
}

func (s *Service) ByImage(ctx context.Context, clip []float32, kind string, exclude int64, limit, offset int) ([]*store.Meme, error) {
	q := `SELECT id FROM memes WHERE clip_vec IS NOT NULL AND status = 'done' AND NOT hidden
		AND id <> $1 AND ($2 = '' OR kind = $2)
		ORDER BY clip_vec <=> $3 LIMIT $4 OFFSET $5`
	rows, err := s.store.Pool.Query(ctx, q, exclude, kind, pgvector.NewVector(clip), limit, offset)
	if err != nil {
		return nil, err
	}
	ids, err := pgx.CollectRows(rows, pgx.RowTo[int64])
	if err != nil {
		return nil, err
	}
	return s.store.MemesByIDs(ctx, ids)
}

func (s *Service) MemeVector(ctx context.Context, id int64) ([]float32, error) {
	clip, _, err := s.store.Vectors(ctx, id)
	if err != nil || clip == nil {
		return nil, err
	}
	return clip.Slice(), nil
}
