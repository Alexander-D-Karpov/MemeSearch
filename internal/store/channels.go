package store

import (
	"context"
	"errors"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

var ErrExists = errors.New("already exists")

type Channel struct {
	ID            int64      `json:"id"`
	Username      string     `json:"username"`
	Title         string     `json:"title"`
	Enabled       bool       `json:"enabled"`
	BackfillLimit int        `json:"backfill_limit"`
	LastPostID    int64      `json:"last_post_id"`
	Status        string     `json:"status"`
	Error         string     `json:"error"`
	Added         int        `json:"added"`
	Duplicates    int        `json:"duplicates"`
	Skipped       int        `json:"skipped"`
	Failed        int        `json:"failed"`
	CreatedAt     time.Time  `json:"created_at"`
	LastPolledAt  *time.Time `json:"last_polled_at"`
	NextPollAt    time.Time  `json:"next_poll_at"`
}

const channelCols = `id, username, title, enabled, backfill_limit, last_post_id, status, error, added, duplicates,
	skipped, failed, created_at, last_polled_at, next_poll_at`

func scanChannel(row pgx.Row) (*Channel, error) {
	c := &Channel{}
	err := row.Scan(&c.ID, &c.Username, &c.Title, &c.Enabled, &c.BackfillLimit, &c.LastPostID, &c.Status, &c.Error,
		&c.Added, &c.Duplicates, &c.Skipped, &c.Failed, &c.CreatedAt, &c.LastPolledAt, &c.NextPollAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, ErrNotFound
	}
	return c, err
}

func (s *Store) Channels(ctx context.Context) ([]*Channel, error) {
	rows, err := s.Pool.Query(ctx, "SELECT "+channelCols+" FROM channels ORDER BY id")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*Channel
	for rows.Next() {
		c, err := scanChannel(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	return out, rows.Err()
}

func (s *Store) Channel(ctx context.Context, id int64) (*Channel, error) {
	return scanChannel(s.Pool.QueryRow(ctx, "SELECT "+channelCols+" FROM channels WHERE id=$1", id))
}

func (s *Store) CreateChannel(ctx context.Context, username string, backfill int) (*Channel, error) {
	c, err := scanChannel(s.Pool.QueryRow(ctx,
		"INSERT INTO channels (username, backfill_limit) VALUES ($1, $2) RETURNING "+channelCols, username, backfill))
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) && pgErr.Code == "23505" {
		return nil, ErrExists
	}
	return c, err
}

type ChannelPatch struct {
	Enabled       *bool `json:"enabled"`
	BackfillLimit *int  `json:"backfill_limit"`
}

func (s *Store) UpdateChannel(ctx context.Context, id int64, p ChannelPatch) (*Channel, error) {
	return scanChannel(s.Pool.QueryRow(ctx, `UPDATE channels SET
		enabled = COALESCE($2, enabled),
		backfill_limit = COALESCE($3, backfill_limit)
		WHERE id=$1 RETURNING `+channelCols, id, p.Enabled, p.BackfillLimit))
}

func (s *Store) ScheduleChannel(ctx context.Context, id int64) error {
	tag, err := s.Pool.Exec(ctx, "UPDATE channels SET next_poll_at=now() WHERE id=$1", id)
	if err == nil && tag.RowsAffected() == 0 {
		return ErrNotFound
	}
	return err
}

func (s *Store) DeleteChannel(ctx context.Context, id int64) error {
	tag, err := s.Pool.Exec(ctx, "DELETE FROM channels WHERE id=$1", id)
	if err == nil && tag.RowsAffected() == 0 {
		return ErrNotFound
	}
	return err
}

type MemeSource struct {
	URL      string     `json:"url"`
	Source   string     `json:"source"`
	Channel  string     `json:"channel,omitempty"`
	PostID   int64      `json:"post_id,omitempty"`
	Caption  string     `json:"caption,omitempty"`
	PostedAt *time.Time `json:"posted_at,omitempty"`
}

func (s *Store) MemeSources(ctx context.Context, memeID int64) ([]*MemeSource, error) {
	rows, err := s.Pool.Query(ctx, `SELECT ms.url, ms.source, COALESCE(c.username, ''), COALESCE(ms.post_id, 0),
		ms.caption, ms.posted_at
		FROM meme_sources ms LEFT JOIN channels c ON c.id = ms.channel_id
		WHERE ms.meme_id=$1 ORDER BY COALESCE(ms.posted_at, ms.created_at), ms.id`, memeID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*MemeSource
	for rows.Next() {
		m := &MemeSource{}
		if err := rows.Scan(&m.URL, &m.Source, &m.Channel, &m.PostID, &m.Caption, &m.PostedAt); err != nil {
			return nil, err
		}
		out = append(out, m)
	}
	return out, rows.Err()
}
