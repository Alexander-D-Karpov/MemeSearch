package mlclient

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
)

type Client struct {
	base     string
	token    string
	http     *http.Client
	rdb      *redis.Client
	cacheTTL time.Duration
}

func New(base, token string, rdb *redis.Client, cacheTTL time.Duration) *Client {
	return &Client{
		base:     base,
		token:    token,
		http:     &http.Client{Timeout: 60 * time.Second},
		rdb:      rdb,
		cacheTTL: cacheTTL,
	}
}

type QueryVectors struct {
	Clip []float32 `json:"clip"`
	Text []float32 `json:"text"`
}

func NormalizeQuery(q string) string {
	return strings.Join(strings.Fields(strings.ToLower(q)), " ")
}

func (c *Client) EmbedQuery(ctx context.Context, q string) (*QueryVectors, error) {
	q = NormalizeQuery(q)
	sum := sha256.Sum256([]byte(q))
	key := "ms:emb:q:" + hex.EncodeToString(sum[:16])
	if raw, err := c.rdb.Get(ctx, key).Bytes(); err == nil {
		var v QueryVectors
		if json.Unmarshal(raw, &v) == nil {
			return &v, nil
		}
	}
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	var v QueryVectors
	if err := c.do(ctx, http.MethodPost, "/embed/query", map[string]string{"q": q}, &v); err != nil {
		return nil, err
	}
	if raw, err := json.Marshal(v); err == nil {
		c.rdb.Set(context.WithoutCancel(ctx), key, raw, c.cacheTTL)
	}
	return &v, nil
}

type APIError struct {
	Status int
	Detail string
}

func (e *APIError) Error() string { return fmt.Sprintf("ml api %d: %s", e.Status, e.Detail) }

func (c *Client) do(ctx context.Context, method, path string, body any, out any) error {
	var rd io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return err
		}
		rd = bytes.NewReader(raw)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.base+path, rd)
	if err != nil {
		return err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	return c.send(req, out)
}

func (c *Client) send(req *http.Request, out any) error {
	req.Header.Set("X-Internal-Token", c.token)
	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if err != nil {
		return err
	}
	if resp.StatusCode >= 300 {
		var e struct {
			Detail any `json:"detail"`
		}
		detail := string(raw)
		if json.Unmarshal(raw, &e) == nil && e.Detail != nil {
			detail = fmt.Sprint(e.Detail)
		}
		return &APIError{Status: resp.StatusCode, Detail: detail}
	}
	if out == nil {
		return nil
	}
	return json.Unmarshal(raw, out)
}

func (c *Client) Codex(ctx context.Context, method, path string, body any) (json.RawMessage, error) {
	var out json.RawMessage
	err := c.do(ctx, method, "/codex"+path, body, &out)
	return out, err
}

func (c *Client) CodexUploadAuth(ctx context.Context, id int64, data []byte) (json.RawMessage, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, fmt.Sprintf("%s/codex/sessions/%d/auth", c.base, id), bytes.NewReader(data))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	var out json.RawMessage
	err = c.send(req, &out)
	return out, err
}

func (c *Client) EmbedImage(ctx context.Context, data []byte) ([]float32, error) {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.base+"/embed/image", bytes.NewReader(data))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/octet-stream")
	var out struct {
		Clip []float32 `json:"clip"`
	}
	if err := c.send(req, &out); err != nil {
		return nil, err
	}
	return out.Clip, nil
}

func (c *Client) Health(ctx context.Context) (json.RawMessage, error) {
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	var out json.RawMessage
	err := c.do(ctx, http.MethodGet, "/health", nil, &out)
	return out, err
}
