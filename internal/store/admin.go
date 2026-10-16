package store

import (
	"context"
	"encoding/json"
	"errors"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"
)

func itoa(i int) string { return strconv.Itoa(i) }

type ImportJob struct {
	ID         int64      `json:"id"`
	Kind       string     `json:"kind"`
	Name       string     `json:"name"`
	Path       string     `json:"-"`
	Status     string     `json:"status"`
	Total      int        `json:"total"`
	Added      int        `json:"added"`
	Duplicates int        `json:"duplicates"`
	Skipped    int        `json:"skipped"`
	Failed     int        `json:"failed"`
	Error      string     `json:"error"`
	Stage      string     `json:"stage"`
	BytesTotal int64      `json:"bytes_total"`
	BytesDone  int64      `json:"bytes_done"`
	CreatedAt  time.Time  `json:"created_at"`
	StartedAt  *time.Time `json:"started_at"`
	FinishedAt *time.Time `json:"finished_at"`
	UpdatedAt  time.Time  `json:"updated_at"`
}

func (s *Store) CreateImport(ctx context.Context, kind, name, path string) (int64, error) {
	var id int64
	err := s.Pool.QueryRow(ctx, "INSERT INTO import_jobs(kind, name, path) VALUES ($1,$2,$3) RETURNING id", kind, name, path).Scan(&id)
	return id, err
}

func (s *Store) Imports(ctx context.Context, limit int) ([]*ImportJob, error) {
	rows, err := s.Pool.Query(ctx, `SELECT id, kind, name, path, status, total, added, duplicates, skipped, failed, error,
		stage, bytes_total, bytes_done, created_at, started_at, finished_at, updated_at
		FROM import_jobs ORDER BY id DESC LIMIT $1`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*ImportJob
	for rows.Next() {
		j := &ImportJob{}
		if err := rows.Scan(&j.ID, &j.Kind, &j.Name, &j.Path, &j.Status, &j.Total, &j.Added, &j.Duplicates, &j.Skipped,
			&j.Failed, &j.Error, &j.Stage, &j.BytesTotal, &j.BytesDone, &j.CreatedAt, &j.StartedAt, &j.FinishedAt,
			&j.UpdatedAt); err != nil {
			return nil, err
		}
		out = append(out, j)
	}
	return out, rows.Err()
}

func (s *Store) CreateUpload(ctx context.Context, name, path string, size int64) (int64, error) {
	var id int64
	err := s.Pool.QueryRow(ctx, `INSERT INTO import_jobs(kind, name, path, status, stage, bytes_total)
		VALUES ('zip', $1, $2, 'uploading', 'receiving the archive', $3) RETURNING id`, name, path, size).Scan(&id)
	return id, err
}

func (s *Store) UploadProgress(ctx context.Context, id, done int64) error {
	_, err := s.Pool.Exec(ctx, "UPDATE import_jobs SET bytes_done=$2, updated_at=now() WHERE id=$1 AND status='uploading'", id, done)
	return err
}

func (s *Store) UploadFinished(ctx context.Context, id, size int64) error {
	_, err := s.Pool.Exec(ctx, `UPDATE import_jobs SET status='pending', stage='queued for import', bytes_done=$2,
		bytes_total=$2, updated_at=now() WHERE id=$1`, id, size)
	return err
}

func (s *Store) FailImport(ctx context.Context, id int64, msg string) error {
	_, err := s.Pool.Exec(ctx, `UPDATE import_jobs SET status='failed', stage='', error=$2, finished_at=now(), updated_at=now()
		WHERE id=$1 AND status IN ('uploading', 'pending')`, id, msg)
	return err
}

func (s *Store) FailInterruptedUploads(ctx context.Context) (int64, error) {
	tag, err := s.Pool.Exec(ctx, `UPDATE import_jobs SET status='failed', stage='',
		error='upload was interrupted (the server restarted), upload the archive again', finished_at=now(), updated_at=now()
		WHERE status='uploading'`)
	return tag.RowsAffected(), err
}

type CodexSession struct {
	ID            int64           `json:"id"`
	Name          string          `json:"name"`
	Enabled       bool            `json:"enabled"`
	Priority      int             `json:"priority"`
	Status        string          `json:"status"`
	Email         string          `json:"email"`
	Plan          string          `json:"plan"`
	LastError     string          `json:"last_error"`
	Usage         json.RawMessage `json:"usage"`
	CooldownUntil *time.Time      `json:"cooldown_until"`
	LastUsedAt    *time.Time      `json:"last_used_at"`
	LastCheckAt   *time.Time      `json:"last_check_at"`
	OKCount       int64           `json:"ok_count"`
	FailCount     int64           `json:"fail_count"`
	CreatedAt     time.Time       `json:"created_at"`
}

func (c *CodexSession) CoolingDown() bool {
	return c.CooldownUntil != nil && c.CooldownUntil.After(time.Now())
}

func (s *Store) CodexSessions(ctx context.Context) ([]*CodexSession, error) {
	rows, err := s.Pool.Query(ctx, `SELECT id, name, enabled, priority, status, email, plan, last_error, usage,
		cooldown_until, last_used_at, last_check_at, ok_count, fail_count, created_at
		FROM codex_sessions ORDER BY priority DESC, id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*CodexSession
	for rows.Next() {
		c := &CodexSession{}
		if err := rows.Scan(&c.ID, &c.Name, &c.Enabled, &c.Priority, &c.Status, &c.Email, &c.Plan, &c.LastError,
			&c.Usage, &c.CooldownUntil, &c.LastUsedAt, &c.LastCheckAt, &c.OKCount, &c.FailCount, &c.CreatedAt); err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	return out, rows.Err()
}

type CodexPatch struct {
	Name          *string `json:"name"`
	Enabled       *bool   `json:"enabled"`
	Priority      *int    `json:"priority"`
	ClearCooldown bool    `json:"clear_cooldown"`
}

func (s *Store) UpdateCodexSession(ctx context.Context, id int64, p CodexPatch) error {
	tag, err := s.Pool.Exec(ctx, `UPDATE codex_sessions SET
		name = coalesce($2, name),
		enabled = coalesce($3, enabled),
		priority = coalesce($4, priority),
		cooldown_until = CASE WHEN $5 THEN NULL ELSE cooldown_until END,
		status = CASE WHEN $5 AND status='limited' THEN 'ok' ELSE status END,
		updated_at = now()
		WHERE id=$1`, id, p.Name, p.Enabled, p.Priority, p.ClearCooldown)
	if err != nil {
		return err
	}
	if tag.RowsAffected() == 0 {
		return ErrNotFound
	}
	return nil
}

func (s *Store) Setting(ctx context.Context, key string, dst any) (bool, error) {
	var raw []byte
	err := s.Pool.QueryRow(ctx, "SELECT value FROM app_settings WHERE key=$1", key).Scan(&raw)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return true, json.Unmarshal(raw, dst)
}

func (s *Store) SetSetting(ctx context.Context, key string, value any) error {
	raw, err := json.Marshal(value)
	if err != nil {
		return err
	}
	_, err = s.Pool.Exec(ctx, `INSERT INTO app_settings(key, value) VALUES ($1,$2)
		ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()`, key, raw)
	return err
}
