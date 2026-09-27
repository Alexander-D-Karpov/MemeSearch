package queue

import (
	"context"
	"encoding/json"
	"time"

	"github.com/redis/go-redis/v9"
)

const (
	StreamHigh   = "ms:q:high"
	StreamLow    = "ms:q:low"
	Delayed      = "ms:q:delayed"
	IndexVersion = "ms:index:ver"
	Events       = "ms:events"
	Group        = "workers"
)

type Job struct {
	Type    string  `json:"type"`
	ID      int64   `json:"id,omitempty"`
	JobID   int64   `json:"job_id,omitempty"`
	Analyze bool    `json:"analyze"`
	At      float64 `json:"at,omitempty"`
}

type Queue struct {
	R *redis.Client
}

func New(r *redis.Client) *Queue { return &Queue{R: r} }

func (q *Queue) Push(ctx context.Context, stream string, jobs ...Job) error {
	const batch = 1000
	now := float64(time.Now().UnixMilli()) / 1000
	for i := range jobs {
		if jobs[i].At == 0 {
			jobs[i].At = now
		}
	}
	for start := 0; start < len(jobs); start += batch {
		end := min(start+batch, len(jobs))
		pipe := q.R.Pipeline()
		for _, j := range jobs[start:end] {
			raw, _ := json.Marshal(j)
			pipe.XAdd(ctx, &redis.XAddArgs{Stream: stream, Values: map[string]any{"job": string(raw)}})
		}
		if _, err := pipe.Exec(ctx); err != nil {
			return err
		}
	}
	return nil
}

func (q *Queue) Process(ctx context.Context, stream string, ids []int64) error {
	jobs := make([]Job, len(ids))
	for i, id := range ids {
		jobs[i] = Job{Type: "process", ID: id, Analyze: true}
	}
	return q.Push(ctx, stream, jobs...)
}

func (q *Queue) Embed(ctx context.Context, stream string, ids []int64) error {
	jobs := make([]Job, len(ids))
	for i, id := range ids {
		jobs[i] = Job{Type: "process", ID: id, Analyze: false}
	}
	return q.Push(ctx, stream, jobs...)
}

func (q *Queue) Import(ctx context.Context, jobID int64) error {
	return q.Push(ctx, StreamHigh, Job{Type: "import", JobID: jobID})
}

func (q *Queue) Bump(ctx context.Context) {
	q.R.Incr(ctx, IndexVersion)
}

func (q *Queue) Version(ctx context.Context) int64 {
	v, _ := q.R.Get(ctx, IndexVersion).Int64()
	return v
}

type Lengths struct {
	High    int64 `json:"high"`
	Low     int64 `json:"low"`
	Delayed int64 `json:"delayed"`
	Pending int64 `json:"in_flight"`
}

func (q *Queue) Lengths(ctx context.Context) Lengths {
	var l Lengths
	for _, s := range []string{StreamHigh, StreamLow} {
		groups, err := q.R.XInfoGroups(ctx, s).Result()
		if err != nil {
			continue
		}
		for _, g := range groups {
			if g.Name != Group {
				continue
			}
			if s == StreamHigh {
				l.High = g.Lag
			} else {
				l.Low = g.Lag
			}
			l.Pending += g.Pending
		}
	}
	l.Delayed, _ = q.R.ZCard(ctx, Delayed).Result()
	return l
}
