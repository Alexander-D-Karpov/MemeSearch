package web

import (
	"encoding/json"
	"errors"
	"fmt"
	"html/template"
	"io/fs"
	"log/slog"
	"net/http"
	"path"
	"strconv"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"

	"github.com/Alexander-D-Karpov/MemeSearch/internal/auth"
	"github.com/Alexander-D-Karpov/MemeSearch/internal/config"
	"github.com/Alexander-D-Karpov/MemeSearch/internal/ingest"
	"github.com/Alexander-D-Karpov/MemeSearch/internal/mlclient"
	"github.com/Alexander-D-Karpov/MemeSearch/internal/queue"
	"github.com/Alexander-D-Karpov/MemeSearch/internal/search"
	"github.com/Alexander-D-Karpov/MemeSearch/internal/store"
	assets "github.com/Alexander-D-Karpov/MemeSearch/web"
)

type Server struct {
	cfg     *config.Config
	store   *store.Store
	rdb     *redis.Client
	queue   *queue.Queue
	search  *search.Service
	ml      *mlclient.Client
	auth    *auth.Auth
	ingest  *ingest.Ingester
	pages   map[string]*template.Template
	version string
}

func New(cfg *config.Config, st *store.Store, rdb *redis.Client, q *queue.Queue, sr *search.Service,
	ml *mlclient.Client, a *auth.Auth, in *ingest.Ingester) (*Server, error) {
	s := &Server{cfg: cfg, store: st, rdb: rdb, queue: q, search: sr, ml: ml, auth: a, ingest: in,
		version: strconv.FormatInt(time.Now().Unix(), 36)}
	if err := s.loadTemplates(); err != nil {
		return nil, err
	}
	return s, nil
}

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	admin := s.auth.RequireAdmin

	static, _ := fs.Sub(assets.FS, "static")
	mux.Handle("GET /static/", cacheForever(http.StripPrefix("/static/", http.FileServerFS(static))))
	if s.cfg.ServeMedia {
		mux.Handle("GET "+s.cfg.MediaURL+"/", http.StripPrefix(s.cfg.MediaURL+"/", mediaOnly(http.FileServer(http.Dir(s.cfg.UploadDir)))))
	}

	mux.HandleFunc("GET /healthz", s.healthz)
	mux.HandleFunc("GET /robots.txt", func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("User-agent: *\nDisallow: /admin\nDisallow: /api/\n"))
	})

	mux.HandleFunc("GET /{$}", s.pageIndex)
	mux.HandleFunc("GET /search", s.pageIndex)
	mux.HandleFunc("GET /partials/results", s.partialResults)
	mux.HandleFunc("GET /m/{id}", s.pageMeme)
	mux.HandleFunc("GET /m/{id}/og.jpg", s.memeOGImage)
	mux.HandleFunc("GET /m/{id}/photo.jpg", s.memePhoto)
	mux.HandleFunc("GET /login", s.pageLogin)
	mux.HandleFunc("POST /login", s.doLogin)
	mux.HandleFunc("POST /logout", s.doLogout)

	mux.HandleFunc("GET /api/v1/search", s.apiSearch)
	mux.HandleFunc("GET /api/v1/memes", s.apiList)
	mux.HandleFunc("GET /api/v1/memes/{id}", s.apiMeme)
	mux.HandleFunc("GET /api/v1/memes/{id}/similar", s.apiSimilar)

	mux.Handle("GET /admin", admin(http.HandlerFunc(s.pageDashboard)))
	mux.Handle("GET /admin/upload", admin(http.HandlerFunc(s.pageUpload)))
	mux.Handle("GET /admin/memes", admin(http.HandlerFunc(s.pageAdminMemes)))
	mux.Handle("GET /admin/m/{id}/edit", admin(http.HandlerFunc(s.pageEdit)))
	mux.Handle("GET /admin/codex", admin(http.HandlerFunc(s.pageCodex)))
	mux.Handle("GET /admin/channels", admin(http.HandlerFunc(s.pageChannels)))
	mux.Handle("GET /admin/settings", admin(http.HandlerFunc(s.pageSettings)))

	mux.Handle("GET /api/v1/admin/stats", admin(http.HandlerFunc(s.apiStats)))
	mux.Handle("POST /api/v1/admin/upload", admin(http.HandlerFunc(s.apiUpload)))
	mux.Handle("POST /api/v1/admin/import/zip", admin(http.HandlerFunc(s.apiImportZip)))
	mux.Handle("POST /api/v1/admin/import/inbox", admin(http.HandlerFunc(s.apiImportInbox)))
	mux.Handle("GET /api/v1/admin/imports", admin(http.HandlerFunc(s.apiImports)))
	mux.Handle("POST /api/v1/admin/reprocess", admin(http.HandlerFunc(s.apiReprocessScope)))
	mux.Handle("POST /api/v1/admin/memes/bulk", admin(http.HandlerFunc(s.apiBulk)))
	mux.Handle("PATCH /api/v1/admin/memes/{id}", admin(http.HandlerFunc(s.apiPatchMeme)))
	mux.Handle("POST /api/v1/admin/memes/{id}/reprocess", admin(http.HandlerFunc(s.apiReprocessOne)))
	mux.Handle("DELETE /api/v1/admin/memes/{id}", admin(http.HandlerFunc(s.apiDeleteMeme)))
	mux.Handle("GET /api/v1/admin/settings", admin(http.HandlerFunc(s.apiGetSettings)))
	mux.Handle("PUT /api/v1/admin/settings", admin(http.HandlerFunc(s.apiPutSettings)))

	mux.Handle("POST /api/v1/admin/tgcache/retry", admin(http.HandlerFunc(s.apiRetryTgCache)))
	mux.Handle("GET /api/v1/admin/channels", admin(http.HandlerFunc(s.apiChannels)))
	mux.Handle("POST /api/v1/admin/channels", admin(http.HandlerFunc(s.apiChannelCreate)))
	mux.Handle("PATCH /api/v1/admin/channels/{id}", admin(http.HandlerFunc(s.apiChannelPatch)))
	mux.Handle("POST /api/v1/admin/channels/{id}/poll", admin(http.HandlerFunc(s.apiChannelPoll)))
	mux.Handle("DELETE /api/v1/admin/channels/{id}", admin(http.HandlerFunc(s.apiChannelDelete)))

	mux.Handle("GET /api/v1/admin/codex", admin(http.HandlerFunc(s.apiCodexList)))
	mux.Handle("POST /api/v1/admin/codex", admin(http.HandlerFunc(s.apiCodexCreate)))
	mux.Handle("PATCH /api/v1/admin/codex/{id}", admin(http.HandlerFunc(s.apiCodexPatch)))
	mux.Handle("DELETE /api/v1/admin/codex/{id}", admin(http.HandlerFunc(s.apiCodexProxy)))
	mux.Handle("POST /api/v1/admin/codex/{id}/login", admin(http.HandlerFunc(s.apiCodexProxy)))
	mux.Handle("GET /api/v1/admin/codex/{id}/login", admin(http.HandlerFunc(s.apiCodexProxy)))
	mux.Handle("DELETE /api/v1/admin/codex/{id}/login", admin(http.HandlerFunc(s.apiCodexProxy)))
	mux.Handle("POST /api/v1/admin/codex/{id}/check", admin(http.HandlerFunc(s.apiCodexProxy)))
	mux.Handle("POST /api/v1/admin/codex/{id}/logout", admin(http.HandlerFunc(s.apiCodexProxy)))
	mux.Handle("POST /api/v1/admin/codex/{id}/auth", admin(http.HandlerFunc(s.apiCodexAuth)))

	return recoverer(logRequests(securityHeaders(s.auth.Middleware(mux))))
}

func (s *Server) healthz(w http.ResponseWriter, r *http.Request) {
	status := map[string]string{"db": "ok", "redis": "ok"}
	code := http.StatusOK
	if err := s.store.Pool.Ping(r.Context()); err != nil {
		status["db"] = err.Error()
		code = http.StatusServiceUnavailable
	}
	if err := s.rdb.Ping(r.Context()).Err(); err != nil {
		status["redis"] = err.Error()
		code = http.StatusServiceUnavailable
	}
	writeJSON(w, code, status)
}

func (s *Server) mediaURL(rel string) string {
	if rel == "" {
		return ""
	}
	return s.cfg.MediaURL + "/" + strings.TrimPrefix(path.Clean("/"+rel), "/")
}

func (s *Server) decorate(memes ...*store.Meme) {
	for _, m := range memes {
		if m == nil {
			continue
		}
		m.URL = s.mediaURL(m.FilePath)
		m.ThumbURL = s.mediaURL(m.ThumbPath)
		if m.ThumbURL == "" && m.Kind != "video" {
			m.ThumbURL = m.URL
		}
	}
}

func pathID(r *http.Request) (int64, error) {
	return strconv.ParseInt(r.PathValue("id"), 10, 64)
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)
	enc.Encode(v)
}

func writeErr(w http.ResponseWriter, code int, err error) {
	writeJSON(w, code, map[string]string{"error": err.Error()})
}

func storeErr(w http.ResponseWriter, err error) {
	if errors.Is(err, store.ErrNotFound) {
		writeErr(w, http.StatusNotFound, err)
		return
	}
	slog.Error("store", "err", err)
	writeErr(w, http.StatusInternalServerError, errors.New("internal error"))
}

func decodeJSON(r *http.Request, v any) error {
	dec := json.NewDecoder(http.MaxBytesReader(nil, r.Body, 1<<20))
	if err := dec.Decode(v); err != nil {
		return fmt.Errorf("invalid json: %w", err)
	}
	return nil
}

func intParam(r *http.Request, key string, def, lo, hi int) int {
	v, err := strconv.Atoi(r.URL.Query().Get(key))
	if err != nil {
		return def
	}
	return max(lo, min(hi, v))
}

func validKind(k string) string {
	switch k {
	case "image", "gif", "video":
		return k
	}
	return ""
}

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (r *statusRecorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}

func (r *statusRecorder) Unwrap() http.ResponseWriter { return r.ResponseWriter }

func logRequests(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rec := &statusRecorder{ResponseWriter: w, status: 200}
		next.ServeHTTP(rec, r)
		if strings.HasPrefix(r.URL.Path, "/static/") || r.URL.Path == "/healthz" {
			return
		}
		slog.Info("http", "method", r.Method, "path", r.URL.Path, "status", rec.status, "ms", time.Since(start).Milliseconds(), "ua", truncate(r.UserAgent(), 120))
	})
}

func recoverer(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if v := recover(); v != nil {
				if v == http.ErrAbortHandler {
					panic(v)
				}
				slog.Error("panic", "err", v, "path", r.URL.Path)
				http.Error(w, "internal error", http.StatusInternalServerError)
			}
		}()
		next.ServeHTTP(w, r)
	})
}

func securityHeaders(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h := w.Header()
		h.Set("X-Content-Type-Options", "nosniff")
		h.Set("Referrer-Policy", "strict-origin-when-cross-origin")
		h.Set("X-Frame-Options", "DENY")
		next.ServeHTTP(w, r)
	})
}

func cacheForever(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("v") != "" {
			w.Header().Set("Cache-Control", "public, max-age=31536000, immutable")
		} else {
			w.Header().Set("Cache-Control", "public, max-age=3600")
		}
		next.ServeHTTP(w, r)
	})
}

func mediaOnly(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p := strings.TrimPrefix(r.URL.Path, "/")
		if !strings.HasPrefix(p, "originals/") && !strings.HasPrefix(p, "thumbs/") || strings.Contains(p, "/.") {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Cache-Control", "public, max-age=31536000, immutable")
		next.ServeHTTP(w, r)
	})
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}
