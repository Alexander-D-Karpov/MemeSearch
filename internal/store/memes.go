package store

import (
	"context"
	"errors"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/pgvector/pgvector-go"
)

var ErrNotFound = errors.New("not found")

type Meme struct {
	ID           int64      `json:"id"`
	SHA256       string     `json:"sha256"`
	Kind         string     `json:"kind"`
	Mime         string     `json:"mime"`
	Ext          string     `json:"ext"`
	FilePath     string     `json:"-"`
	ThumbPath    string     `json:"-"`
	URL          string     `json:"url"`
	ThumbURL     string     `json:"thumb_url"`
	SizeBytes    int64      `json:"size_bytes"`
	Width        int        `json:"width"`
	Height       int        `json:"height"`
	DurationMs   int        `json:"duration_ms"`
	OriginalName string     `json:"original_name"`
	Source       string     `json:"source"`
	SourceRef    string     `json:"source_ref"`
	Caption      string     `json:"caption"`
	Status       string     `json:"status"`
	Error        string     `json:"error,omitempty"`
	Attempts     int        `json:"attempts"`
	Provider     string     `json:"provider"`
	Model        string     `json:"model"`
	Title        string     `json:"title"`
	Description  string     `json:"description"`
	OCRText      string     `json:"text"`
	Transcript   string     `json:"transcript"`
	Objects      []string   `json:"objects"`
	Tags         []string   `json:"tags"`
	People       []string   `json:"people"`
	Template     string     `json:"template"`
	Lang         string     `json:"lang"`
	Mood         string     `json:"mood"`
	NSFW         bool       `json:"nsfw"`
	Hidden       bool       `json:"hidden"`
	Locked       bool       `json:"locked"`
	CreatedAt    time.Time  `json:"created_at"`
	UpdatedAt    time.Time  `json:"updated_at"`
	ProcessedAt  *time.Time `json:"processed_at"`
	Score        float64    `json:"score,omitempty"`
}

const memeCols = `id, sha256, kind, mime, ext, file_path, thumb_path, size_bytes, width, height,
	duration_ms, original_name, source, source_ref, caption, status, error, attempts, provider, model,
	title, description, ocr_text, transcript, objects, tags, people, template, lang, mood, nsfw, hidden,
	locked, created_at, updated_at, processed_at`

func scanMeme(row pgx.Row) (*Meme, error) {
	m := &Meme{}
	err := row.Scan(&m.ID, &m.SHA256, &m.Kind, &m.Mime, &m.Ext, &m.FilePath, &m.ThumbPath, &m.SizeBytes,
		&m.Width, &m.Height, &m.DurationMs, &m.OriginalName, &m.Source, &m.SourceRef, &m.Caption, &m.Status,
		&m.Error, &m.Attempts, &m.Provider, &m.Model, &m.Title, &m.Description, &m.OCRText, &m.Transcript,
		&m.Objects, &m.Tags, &m.People, &m.Template, &m.Lang, &m.Mood, &m.NSFW, &m.Hidden, &m.Locked,
		&m.CreatedAt, &m.UpdatedAt, &m.ProcessedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, ErrNotFound
	}
	return m, err
}

func collectMemes(rows pgx.Rows) ([]*Meme, error) {
	defer rows.Close()
	var out []*Meme
	for rows.Next() {
		m, err := scanMeme(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, m)
	}
	return out, rows.Err()
}

func (s *Store) Meme(ctx context.Context, id int64) (*Meme, error) {
	return scanMeme(s.Pool.QueryRow(ctx, "SELECT "+memeCols+" FROM memes WHERE id=$1", id))
}

func (s *Store) MemeBySHA(ctx context.Context, sha string) (*Meme, error) {
	return scanMeme(s.Pool.QueryRow(ctx, "SELECT "+memeCols+" FROM memes WHERE sha256=$1", sha))
}

func (s *Store) MemesByIDs(ctx context.Context, ids []int64) ([]*Meme, error) {
	if len(ids) == 0 {
		return nil, nil
	}
	rows, err := s.Pool.Query(ctx, "SELECT "+memeCols+" FROM memes WHERE id = ANY($1)", ids)
	if err != nil {
		return nil, err
	}
	list, err := collectMemes(rows)
	if err != nil {
		return nil, err
	}
	byID := make(map[int64]*Meme, len(list))
	for _, m := range list {
		byID[m.ID] = m
	}
	out := make([]*Meme, 0, len(ids))
	for _, id := range ids {
		if m, ok := byID[id]; ok {
			out = append(out, m)
		}
	}
	return out, nil
}

type ListFilter struct {
	Status   string
	Kind     string
	Public   bool
	BeforeID int64
	Limit    int
	Offset   int
}

func (s *Store) ListMemes(ctx context.Context, f ListFilter) ([]*Meme, error) {
	q := "SELECT " + memeCols + " FROM memes WHERE true"
	args := []any{}
	add := func(cond string, v any) {
		args = append(args, v)
		q += " AND " + cond + "$" + itoa(len(args))
	}
	if f.Public {
		q += " AND status='done' AND NOT hidden"
	}
	if f.Status == "hidden" {
		q += " AND hidden"
	} else if f.Status != "" {
		add("status=", f.Status)
	}
	if f.Kind != "" {
		add("kind=", f.Kind)
	}
	if f.BeforeID > 0 {
		add("id<", f.BeforeID)
	}
	if f.Limit <= 0 {
		f.Limit = 48
	}
	args = append(args, f.Limit, f.Offset)
	q += " ORDER BY id DESC LIMIT $" + itoa(len(args)-1) + " OFFSET $" + itoa(len(args))
	rows, err := s.Pool.Query(ctx, q, args...)
	if err != nil {
		return nil, err
	}
	return collectMemes(rows)
}

type NewMeme struct {
	SHA256       string
	Kind         string
	Mime         string
	Ext          string
	FilePath     string
	SizeBytes    int64
	OriginalName string
	Source       string
	SourceRef    string
	Caption      string
}

func (s *Store) InsertMeme(ctx context.Context, n NewMeme) (id int64, created bool, err error) {
	err = s.Pool.QueryRow(ctx, `INSERT INTO memes (sha256, kind, mime, ext, file_path, size_bytes, original_name, source, source_ref, caption)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
		ON CONFLICT (sha256) DO NOTHING RETURNING id`,
		n.SHA256, n.Kind, n.Mime, n.Ext, n.FilePath, n.SizeBytes, n.OriginalName, n.Source, n.SourceRef, n.Caption).Scan(&id)
	if errors.Is(err, pgx.ErrNoRows) {
		err = s.Pool.QueryRow(ctx, "SELECT id FROM memes WHERE sha256=$1", n.SHA256).Scan(&id)
		return id, false, err
	}
	return id, err == nil, err
}

type MemePatch struct {
	Title       *string   `json:"title"`
	Description *string   `json:"description"`
	OCRText     *string   `json:"text"`
	Transcript  *string   `json:"transcript"`
	Tags        *[]string `json:"tags"`
	Objects     *[]string `json:"objects"`
	People      *[]string `json:"people"`
	Template    *string   `json:"template"`
	NSFW        *bool     `json:"nsfw"`
	Hidden      *bool     `json:"hidden"`
	Locked      *bool     `json:"locked"`
}

func (p MemePatch) TouchesText() bool {
	return p.Title != nil || p.Description != nil || p.OCRText != nil || p.Transcript != nil ||
		p.Tags != nil || p.Objects != nil || p.People != nil || p.Template != nil
}

func (s *Store) UpdateMeme(ctx context.Context, id int64, p MemePatch) error {
	q := "UPDATE memes SET updated_at=now()"
	args := []any{}
	set := func(col string, v any) {
		args = append(args, v)
		q += ", " + col + "=$" + itoa(len(args))
	}
	if p.Title != nil {
		set("title", *p.Title)
	}
	if p.Description != nil {
		set("description", *p.Description)
	}
	if p.OCRText != nil {
		set("ocr_text", *p.OCRText)
	}
	if p.Transcript != nil {
		set("transcript", *p.Transcript)
	}
	if p.Tags != nil {
		set("tags", *p.Tags)
	}
	if p.Objects != nil {
		set("objects", *p.Objects)
	}
	if p.People != nil {
		set("people", *p.People)
	}
	if p.Template != nil {
		set("template", *p.Template)
	}
	if p.NSFW != nil {
		set("nsfw", *p.NSFW)
	}
	if p.Hidden != nil {
		set("hidden", *p.Hidden)
	}
	if p.Locked != nil {
		set("locked", *p.Locked)
	}
	if p.TouchesText() && p.Locked == nil {
		q += ", locked=true"
	}
	args = append(args, id)
	q += " WHERE id=$" + itoa(len(args))
	tag, err := s.Pool.Exec(ctx, q, args...)
	if err != nil {
		return err
	}
	if tag.RowsAffected() == 0 {
		return ErrNotFound
	}
	return nil
}

func (s *Store) SetHidden(ctx context.Context, ids []int64, hidden bool) error {
	_, err := s.Pool.Exec(ctx, "UPDATE memes SET hidden=$1, updated_at=now() WHERE id = ANY($2)", hidden, ids)
	return err
}

func (s *Store) DeleteMemes(ctx context.Context, ids []int64) ([]*Meme, error) {
	rows, err := s.Pool.Query(ctx, "DELETE FROM memes WHERE id = ANY($1) RETURNING "+memeCols, ids)
	if err != nil {
		return nil, err
	}
	return collectMemes(rows)
}

const resetStatus = `status = CASE WHEN status = 'done' THEN 'done' ELSE 'pending' END,
	error = '', attempts = 0, updated_at = now()`

func (s *Store) MarkPending(ctx context.Context, ids []int64) ([]int64, error) {
	rows, err := s.Pool.Query(ctx, `UPDATE memes SET `+resetStatus+`
		WHERE id = ANY($1) RETURNING id`, ids)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, pgx.RowTo[int64])
}

func (s *Store) MarkPendingScope(ctx context.Context, scope string) ([]int64, error) {
	var where string
	switch scope {
	case "failed":
		where = "status='failed'"
	case "pending":
		where = "status='pending'"
	case "stuck":
		where = "status IN ('processing', 'pending') AND updated_at < now() - interval '30 minutes'"
	case "all":
		where = "true"
	case "done":
		where = "status='done'"
	default:
		return nil, errors.New("unknown scope")
	}
	rows, err := s.Pool.Query(ctx, `UPDATE memes SET `+resetStatus+`
		WHERE `+where+` RETURNING id`)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, pgx.RowTo[int64])
}

func (s *Store) IDsForScope(ctx context.Context, scope string) ([]int64, error) {
	where := "status='done'"
	if scope == "all" {
		where = "status IN ('done','failed')"
	}
	rows, err := s.Pool.Query(ctx, "SELECT id FROM memes WHERE "+where+" ORDER BY id")
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, pgx.RowTo[int64])
}

func (s *Store) Vectors(ctx context.Context, id int64) (clip, text *pgvector.Vector, err error) {
	err = s.Pool.QueryRow(ctx, "SELECT clip_vec, text_vec FROM memes WHERE id=$1", id).Scan(&clip, &text)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil, ErrNotFound
	}
	return clip, text, err
}

type Stats struct {
	Total      int64            `json:"total"`
	ByStatus   map[string]int64 `json:"by_status"`
	ByKind     map[string]int64 `json:"by_kind"`
	ByProvider map[string]int64 `json:"by_provider"`
	Hidden     int64            `json:"hidden"`
	Bytes      int64            `json:"bytes"`
}

func (s *Store) Stats(ctx context.Context) (*Stats, error) {
	st := &Stats{ByStatus: map[string]int64{}, ByKind: map[string]int64{}, ByProvider: map[string]int64{}}
	rows, err := s.Pool.Query(ctx, `SELECT status, kind, provider, hidden, count(*), coalesce(sum(size_bytes),0)
		FROM memes GROUP BY status, kind, provider, hidden`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var status, kind, provider string
		var hidden bool
		var n, b int64
		if err := rows.Scan(&status, &kind, &provider, &hidden, &n, &b); err != nil {
			return nil, err
		}
		st.Total += n
		st.Bytes += b
		st.ByStatus[status] += n
		st.ByKind[kind] += n
		if provider != "" {
			st.ByProvider[provider] += n
		}
		if hidden {
			st.Hidden += n
		}
	}
	return st, rows.Err()
}

func (s *Store) RecentFailures(ctx context.Context, limit int) ([]*Meme, error) {
	rows, err := s.Pool.Query(ctx, "SELECT "+memeCols+" FROM memes WHERE status='failed' ORDER BY updated_at DESC LIMIT $1", limit)
	if err != nil {
		return nil, err
	}
	return collectMemes(rows)
}
