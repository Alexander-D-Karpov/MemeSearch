package web

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"mime/multipart"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"github.com/Alexander-D-Karpov/MemeSearch/internal/ingest"
	"github.com/Alexander-D-Karpov/MemeSearch/internal/mlclient"
	"github.com/Alexander-D-Karpov/MemeSearch/internal/queue"
	"github.com/Alexander-D-Karpov/MemeSearch/internal/store"
)

type AnalysisSettings struct {
	PromptExtra     string `json:"prompt_extra"`
	CodexEnabled    bool   `json:"codex_enabled"`
	FallbackEnabled bool   `json:"fallback_enabled"`
	CodexModel      string `json:"codex_model"`
	CodexEffort     string `json:"codex_effort"`
	FallbackModel   string `json:"fallback_model"`
	Transcribe      bool   `json:"transcribe"`
}

func defaultSettings() AnalysisSettings {
	return AnalysisSettings{CodexEnabled: true, FallbackEnabled: true, Transcribe: true}
}

func (s *Server) loadSettings(ctx context.Context) (AnalysisSettings, error) {
	st := defaultSettings()
	_, err := s.store.Setting(ctx, "analysis", &st)
	return st, err
}

type Dashboard struct {
	Stats           *store.Stats
	LastHour        int64
	ETA             string
	TransientFailed int64
	TgCache         store.TgCache
	Media           MediaProgress
	Dedupe          DedupeReport
	Review          ReviewReport
	Queue           queue.Lengths
	Failures        []*store.Meme
	Imports         []*store.ImportJob
	Codex           []*store.CodexSession
	ML              string
}

func (s *Server) pageDashboard(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	d := Dashboard{Queue: s.queue.Lengths(ctx)}
	var err error
	if d.Stats, err = s.store.Stats(ctx); err != nil {
		storeErr(w, err)
		return
	}
	if d.TransientFailed, err = s.store.CountTransientFailed(ctx); err != nil {
		storeErr(w, err)
		return
	}
	if d.TgCache, err = s.store.TgCacheStats(ctx); err != nil {
		storeErr(w, err)
		return
	}
	if d.LastHour, err = s.store.AnalyzedSince(ctx, time.Hour); err != nil {
		storeErr(w, err)
		return
	}
	d.ETA = eta(d.Stats.ByStatus["pending"]+d.Stats.ByStatus["processing"], d.LastHour)
	d.Dedupe = s.dedupeReport(ctx)
	d.Review = s.reviewReport(ctx)
	if d.Media, err = s.mediaProgress(ctx); err != nil {
		storeErr(w, err)
		return
	}
	d.Failures, _ = s.store.RecentFailures(ctx, 10)
	s.decorate(d.Failures...)
	d.Imports, _ = s.store.Imports(ctx, 5)
	d.Codex, _ = s.store.CodexSessions(ctx)
	if raw, err := s.ml.Health(ctx); err != nil {
		d.ML = "unavailable: " + err.Error()
	} else {
		d.ML = string(raw)
	}
	w.Header().Set("Cache-Control", "no-store")
	s.render(w, r, http.StatusOK, "admin/dashboard", Page{Title: "Admin", Data: d})
}

func (s *Server) pageUpload(w http.ResponseWriter, r *http.Request) {
	imports, err := s.store.Imports(r.Context(), 20)
	if err != nil {
		storeErr(w, err)
		return
	}
	s.render(w, r, http.StatusOK, "admin/upload", Page{Title: "Upload", Data: map[string]any{
		"Imports":   imports,
		"MaxFileMB": s.cfg.MaxFileBytes >> 20,
		"MaxZipGB":  s.cfg.MaxZipBytes >> 30,
		"InboxPath": filepath.Join(inboxRoot(s.cfg.UploadDir), "inbox"),
	}})
}

func inboxRoot(uploadDir string) string {
	if host := strings.TrimSpace(os.Getenv("MEDIA_HOST_DIR")); strings.HasPrefix(host, "/") {
		return host
	}
	return uploadDir
}

type AdminMemes struct {
	TransientFailed int64
	Memes           []*store.Meme
	Status          string
	Kind            string
	NextBefore      int64
}

func (s *Server) pageAdminMemes(w http.ResponseWriter, r *http.Request) {
	status := r.URL.Query().Get("status")
	switch status {
	case "", "pending", "processing", "done", "failed", "hidden":
	default:
		status = ""
	}
	var before int64
	fmt.Sscan(r.URL.Query().Get("before"), &before)
	limit := 60
	memes, err := s.store.ListMemes(r.Context(), store.ListFilter{Status: status, Kind: validKind(r.URL.Query().Get("kind")), BeforeID: before, Limit: limit})
	if err != nil {
		storeErr(w, err)
		return
	}
	s.decorate(memes...)
	d := AdminMemes{Memes: memes, Status: status, Kind: validKind(r.URL.Query().Get("kind"))}
	if status == "failed" {
		if d.TransientFailed, err = s.store.CountTransientFailed(r.Context()); err != nil {
			storeErr(w, err)
			return
		}
	}
	if len(memes) == limit {
		d.NextBefore = memes[len(memes)-1].ID
	}
	s.render(w, r, http.StatusOK, "admin/memes", Page{Title: "Memes", Data: d})
}

func (s *Server) pageEdit(w http.ResponseWriter, r *http.Request) {
	id, err := pathID(r)
	if err != nil {
		s.notFound(w, r)
		return
	}
	m, err := s.store.Meme(r.Context(), id)
	if errors.Is(err, store.ErrNotFound) {
		s.notFound(w, r)
		return
	}
	if err != nil {
		storeErr(w, err)
		return
	}
	s.decorate(m)
	s.render(w, r, http.StatusOK, "admin/edit", Page{Title: "Edit #" + fmt.Sprint(m.ID), Data: m})
}

func (s *Server) pageCodex(w http.ResponseWriter, r *http.Request) {
	sessions, err := s.store.CodexSessions(r.Context())
	if err != nil {
		storeErr(w, err)
		return
	}
	s.render(w, r, http.StatusOK, "admin/codex", Page{Title: "Codex sessions", Data: sessions})
}

func (s *Server) pageSettings(w http.ResponseWriter, r *http.Request) {
	st, err := s.loadSettings(r.Context())
	if err != nil {
		storeErr(w, err)
		return
	}
	s.render(w, r, http.StatusOK, "admin/settings", Page{Title: "Settings", Data: st})
}

func (s *Server) apiStats(w http.ResponseWriter, r *http.Request) {
	st, err := s.store.Stats(r.Context())
	if err != nil {
		storeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"memes": st, "queue": s.queue.Lengths(r.Context())})
}

func (s *Server) apiUpload(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, s.cfg.MaxFileBytes*64)
	mr, err := r.MultipartReader()
	if err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	stream := queue.StreamHigh
	if r.URL.Query().Get("bulk") == "1" {
		stream = queue.StreamMid
	}
	var caption string
	results := []*ingest.Result{}
	added := 0
	for {
		part, err := mr.NextPart()
		if err == io.EOF {
			break
		}
		if err != nil {
			writeErr(w, http.StatusBadRequest, err)
			return
		}
		if part.FormName() == "caption" {
			b, _ := io.ReadAll(io.LimitReader(part, 8<<10))
			caption = string(b)
			part.Close()
			continue
		}
		if part.FileName() == "" {
			part.Close()
			continue
		}
		res, err := s.ingest.Save(r.Context(), part, ingest.Meta{
			Name: part.FileName(), Source: "upload", Caption: caption, Stream: stream,
		})
		part.Close()
		if err != nil {
			var mbe *http.MaxBytesError
			if errors.As(err, &mbe) {
				writeErr(w, http.StatusRequestEntityTooLarge, err)
				return
			}
			results = append(results, &ingest.Result{Name: part.FileName(), Error: err.Error()})
			continue
		}
		if !res.Duplicate {
			added++
		}
		results = append(results, res)
	}
	if added > 0 {
		s.queue.Bump(r.Context())
	}
	writeJSON(w, http.StatusOK, map[string]any{"results": results, "added": added})
}

const uploadProgressEvery = 2 * time.Second

type countingWriter struct {
	w io.Writer
	n atomic.Int64
}

func (c *countingWriter) Write(p []byte) (int, error) {
	n, err := c.w.Write(p)
	c.n.Add(int64(n))
	return n, err
}

func (s *Server) apiImportZip(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, s.cfg.MaxZipBytes)
	mr, err := r.MultipartReader()
	if err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	var part *multipart.Part
	for {
		part, err = mr.NextPart()
		if err != nil {
			writeErr(w, http.StatusBadRequest, errors.New("file part is required"))
			return
		}
		if part.FormName() == "file" && part.FileName() != "" {
			break
		}
		part.Close()
	}
	defer part.Close()

	dir := filepath.Join(s.cfg.UploadDir, ".imports")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		writeErr(w, http.StatusInternalServerError, err)
		return
	}
	suffix := make([]byte, 8)
	rand.Read(suffix)
	dst := filepath.Join(dir, hex.EncodeToString(suffix)+".zip")
	f, err := os.OpenFile(dst, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err)
		return
	}
	id, err := s.store.CreateUpload(r.Context(), part.FileName(), dst, max(r.ContentLength, 0))
	if err != nil {
		f.Close()
		os.Remove(dst)
		storeErr(w, err)
		return
	}
	fail := func(code int, msg string) {
		f.Close()
		os.Remove(dst)
		s.store.FailImport(context.WithoutCancel(r.Context()), id, msg)
		writeErr(w, code, errors.New(msg))
	}
	head := make([]byte, 4)
	n, _ := io.ReadFull(part, head)
	if n < 4 || !bytes.Equal(head, []byte("PK\x03\x04")) {
		fail(http.StatusBadRequest, "not a zip archive")
		return
	}
	f.Write(head)
	cw := &countingWriter{w: f}
	cw.n.Store(int64(n))
	done := make(chan struct{})
	go func() {
		t := time.NewTicker(uploadProgressEvery)
		defer t.Stop()
		for {
			select {
			case <-done:
				return
			case <-t.C:
				s.store.UploadProgress(context.Background(), id, cw.n.Load())
			}
		}
	}()
	_, err = io.Copy(cw, part)
	close(done)
	if err != nil {
		fail(http.StatusBadRequest, fmt.Sprintf("upload interrupted after %d MB: %v", cw.n.Load()>>20, err))
		return
	}
	if err := f.Close(); err != nil {
		os.Remove(dst)
		s.store.FailImport(context.WithoutCancel(r.Context()), id, err.Error())
		writeErr(w, http.StatusInternalServerError, err)
		return
	}
	if err := s.store.UploadFinished(r.Context(), id, cw.n.Load()); err != nil {
		storeErr(w, err)
		return
	}
	if err := s.queue.Import(r.Context(), id); err != nil {
		storeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"job_id": id, "bytes": cw.n.Load()})
}

func (s *Server) apiImportInbox(w http.ResponseWriter, r *http.Request) {
	dir := filepath.Join(s.cfg.UploadDir, "inbox")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		writeErr(w, http.StatusInternalServerError, err)
		return
	}
	id, err := s.store.CreateImport(r.Context(), "dir", "inbox", dir)
	if err != nil {
		storeErr(w, err)
		return
	}
	if err := s.queue.Import(r.Context(), id); err != nil {
		storeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"job_id": id})
}

func (s *Server) apiImports(w http.ResponseWriter, r *http.Request) {
	jobs, err := s.store.Imports(r.Context(), intParam(r, "limit", 20, 1, 200))
	if err != nil {
		storeErr(w, err)
		return
	}
	if jobs == nil {
		jobs = []*store.ImportJob{}
	}
	writeJSON(w, http.StatusOK, map[string]any{"imports": jobs})
}

func (s *Server) apiReprocessScope(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Scope string `json:"scope"`
		Mode  string `json:"mode"`
	}
	if err := decodeJSON(r, &req); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	var ids []int64
	var err error
	if req.Mode == "embed" {
		ids, err = s.store.IDsForScope(r.Context(), req.Scope)
		if err == nil {
			err = s.queue.Embed(r.Context(), queue.StreamLow, ids)
		}
	} else {
		ids, err = s.store.MarkPendingScope(r.Context(), req.Scope)
		if err == nil {
			err = s.queue.Process(r.Context(), queue.StreamLow, ids)
		}
	}
	if err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	s.queue.Bump(r.Context())
	writeJSON(w, http.StatusOK, map[string]any{"queued": len(ids)})
}

func (s *Server) apiBulk(w http.ResponseWriter, r *http.Request) {
	var req struct {
		IDs    []int64 `json:"ids"`
		Action string  `json:"action"`
	}
	if err := decodeJSON(r, &req); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	if len(req.IDs) == 0 || len(req.IDs) > 10000 {
		writeErr(w, http.StatusBadRequest, errors.New("1..10000 ids required"))
		return
	}
	ctx := r.Context()
	var err error
	affected := len(req.IDs)
	switch req.Action {
	case "reprocess":
		var ids []int64
		if ids, err = s.store.MarkPending(ctx, req.IDs); err == nil {
			err = s.queue.Process(ctx, queue.StreamHigh, ids)
			affected = len(ids)
		}
	case "embed":
		err = s.queue.Embed(ctx, queue.StreamHigh, req.IDs)
	case "hide", "unhide":
		err = s.store.SetHidden(ctx, req.IDs, req.Action == "hide")
	case "delete":
		var deleted []*store.Meme
		if deleted, err = s.store.DeleteMemes(ctx, req.IDs); err == nil {
			for _, m := range deleted {
				s.ingest.RemoveFiles(m)
			}
			affected = len(deleted)
		}
	default:
		err = errors.New("unknown action")
	}
	if err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	s.queue.Bump(ctx)
	writeJSON(w, http.StatusOK, map[string]any{"affected": affected})
}

func (s *Server) apiPatchMeme(w http.ResponseWriter, r *http.Request) {
	id, err := pathID(r)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	var p store.MemePatch
	if err := decodeJSON(r, &p); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	for _, list := range []*[]string{p.Tags, p.Objects, p.People} {
		if list != nil {
			*list = cleanList(*list)
		}
	}
	if err := s.store.UpdateMeme(r.Context(), id, p); err != nil {
		storeErr(w, err)
		return
	}
	if p.TouchesText() {
		s.queue.Embed(r.Context(), queue.StreamHigh, []int64{id})
	}
	s.queue.Bump(r.Context())
	m, err := s.store.Meme(r.Context(), id)
	if err != nil {
		storeErr(w, err)
		return
	}
	s.decorate(m)
	writeJSON(w, http.StatusOK, m)
}

func cleanList(in []string) []string {
	out := make([]string, 0, len(in))
	seen := map[string]bool{}
	for _, v := range in {
		v = strings.TrimSpace(v)
		if v == "" || seen[strings.ToLower(v)] {
			continue
		}
		seen[strings.ToLower(v)] = true
		out = append(out, v)
	}
	return out
}

func (s *Server) apiReprocessOne(w http.ResponseWriter, r *http.Request) {
	id, err := pathID(r)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	if r.URL.Query().Get("mode") == "embed" {
		err = s.queue.Embed(r.Context(), queue.StreamHigh, []int64{id})
	} else {
		var ids []int64
		if ids, err = s.store.MarkPending(r.Context(), []int64{id}); err == nil {
			if len(ids) == 0 {
				storeErr(w, store.ErrNotFound)
				return
			}
			err = s.queue.Process(r.Context(), queue.StreamHigh, ids)
		}
	}
	if err != nil {
		storeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"queued": true})
}

func (s *Server) apiDeleteMeme(w http.ResponseWriter, r *http.Request) {
	id, err := pathID(r)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	deleted, err := s.store.DeleteMemes(r.Context(), []int64{id})
	if err != nil {
		storeErr(w, err)
		return
	}
	if len(deleted) == 0 {
		storeErr(w, store.ErrNotFound)
		return
	}
	for _, m := range deleted {
		s.ingest.RemoveFiles(m)
	}
	s.queue.Bump(r.Context())
	writeJSON(w, http.StatusOK, map[string]any{"deleted": id})
}

func (s *Server) apiGetSettings(w http.ResponseWriter, r *http.Request) {
	st, err := s.loadSettings(r.Context())
	if err != nil {
		storeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, st)
}

func (s *Server) apiPutSettings(w http.ResponseWriter, r *http.Request) {
	var st AnalysisSettings
	if err := decodeJSON(r, &st); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	if len(st.PromptExtra) > 20000 {
		writeErr(w, http.StatusBadRequest, errors.New("prompt_extra is too long"))
		return
	}
	switch st.CodexEffort {
	case "", "minimal", "low", "medium", "high":
	default:
		writeErr(w, http.StatusBadRequest, errors.New("codex_effort must be minimal|low|medium|high"))
		return
	}
	if err := s.store.SetSetting(r.Context(), "analysis", st); err != nil {
		storeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, st)
}

func (s *Server) apiCodexList(w http.ResponseWriter, r *http.Request) {
	sessions, err := s.store.CodexSessions(r.Context())
	if err != nil {
		storeErr(w, err)
		return
	}
	if sessions == nil {
		sessions = []*store.CodexSession{}
	}
	writeJSON(w, http.StatusOK, map[string]any{"sessions": sessions})
}

func (s *Server) apiCodexCreate(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Name string `json:"name"`
	}
	if err := decodeJSON(r, &req); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	s.proxyML(w, r, http.MethodPost, "/sessions", req)
}

func (s *Server) apiCodexPatch(w http.ResponseWriter, r *http.Request) {
	id, err := pathID(r)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	var p store.CodexPatch
	if err := decodeJSON(r, &p); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	if err := s.store.UpdateCodexSession(r.Context(), id, p); err != nil {
		storeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (s *Server) apiCodexProxy(w http.ResponseWriter, r *http.Request) {
	id, err := pathID(r)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	suffix := ""
	if i := strings.LastIndex(r.URL.Path, "/"); i >= 0 && r.URL.Path[i+1:] != r.PathValue("id") {
		suffix = r.URL.Path[i:]
	}
	s.proxyML(w, r, r.Method, fmt.Sprintf("/sessions/%d%s", id, suffix), nil)
}

func (s *Server) apiCodexAuth(w http.ResponseWriter, r *http.Request) {
	id, err := pathID(r)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, 2<<20)
	var data []byte
	if strings.HasPrefix(r.Header.Get("Content-Type"), "multipart/") {
		f, _, err := r.FormFile("file")
		if err != nil {
			writeErr(w, http.StatusBadRequest, err)
			return
		}
		defer f.Close()
		data, err = io.ReadAll(f)
		if err != nil {
			writeErr(w, http.StatusBadRequest, err)
			return
		}
	} else {
		if data, err = io.ReadAll(r.Body); err != nil {
			writeErr(w, http.StatusBadRequest, err)
			return
		}
	}
	if !json.Valid(data) {
		writeErr(w, http.StatusBadRequest, errors.New("auth.json must be valid JSON"))
		return
	}
	out, err := s.ml.CodexUploadAuth(r.Context(), id, data)
	s.writeML(w, out, err)
}

func (s *Server) proxyML(w http.ResponseWriter, r *http.Request, method, path string, body any) {
	out, err := s.ml.Codex(r.Context(), method, path, body)
	s.writeML(w, out, err)
}

func (s *Server) writeML(w http.ResponseWriter, out json.RawMessage, err error) {
	if err != nil {
		var apiErr *mlclient.APIError
		if errors.As(err, &apiErr) {
			writeJSON(w, apiErr.Status, map[string]string{"error": apiErr.Detail})
			return
		}
		slog.Error("ml proxy", "err", err)
		writeErr(w, http.StatusBadGateway, errors.New("ml service unavailable"))
		return
	}
	if len(out) == 0 {
		out = json.RawMessage(`{}`)
	}
	w.Header().Set("Content-Type", "application/json")
	w.Write(out)
}

const (
	mediaStatsKey    = "ms:media:stats"
	speechStatsKey   = "ms:speech:stats"
	mediaMaxAttempts = 3
)

type MediaProgress struct {
	store.MediaStats
	Percent     float64
	Rate        float64
	Done        int64
	Regenerated int64
	Remuxed     int64
	LastError   string
	SpeechDone  int64
	SpeechFound int64
	SpeechError string
	State       string
	ETA         string
	Failures    []store.MediaFailure
}

func (s *Server) mediaProgress(ctx context.Context) (MediaProgress, error) {
	var p MediaProgress
	var err error
	if p.MediaStats, err = s.store.MediaStats(ctx, mediaMaxAttempts); err != nil {
		return p, err
	}
	if p.Total > 0 {
		p.Percent = float64(p.OK) * 100 / float64(p.Total)
	}
	h, _ := s.rdb.HGetAll(ctx, mediaStatsKey).Result()
	num := func(k string) int64 { n, _ := strconv.ParseInt(h[k], 10, 64); return n }
	p.Rate, _ = strconv.ParseFloat(h["rate"], 64)
	p.Done, p.Regenerated, p.Remuxed, p.LastError = num("done"), num("regenerated"), num("remuxed"), h["last_error"]
	sp, _ := s.rdb.HGetAll(ctx, speechStatsKey).Result()
	p.SpeechDone, _ = strconv.ParseInt(sp["done"], 10, 64)
	p.SpeechFound, _ = strconv.ParseInt(sp["with_speech"], 10, 64)
	p.SpeechError = sp["last_error"]
	seen := num("seen_at")
	left := p.Todo
	switch {
	case seen == 0 || time.Since(time.Unix(seen, 0)) > 3*time.Minute:
		p.State = "the worker is not running the media check"
	case p.Todo == 0 && p.Retrying == 0:
		p.State = "all done"
	case p.Todo == 0:
		p.State = "waiting to retry"
	default:
		p.State = "working"
	}
	if left > 0 && p.Rate > 0 {
		p.ETA = eta(left, int64(p.Rate*60))
	}
	p.Failures, _ = s.store.MediaFailures(ctx, 5)
	return p, nil
}

type DedupePair struct {
	Keeper *store.Meme
	Dup    *store.Meme
	Why    string
}

type SimCount struct {
	Percent string
	N       int64
}

type DedupeReport struct {
	State    string
	Apply    bool
	Scanned  int64
	Groups   int64
	ToRemove int64
	Visual   int64
	Ask      int64
	VisualBy []SimCount
	Merged   int64
	Distance int64
	Error    string
	Started  time.Time
	Finished time.Time
	Samples  []DedupePair
}

func (s *Server) dedupeReport(ctx context.Context) DedupeReport {
	h, _ := s.rdb.HGetAll(ctx, "ms:dedupe:report").Result()
	num := func(k string) int64 { n, _ := strconv.ParseInt(h[k], 10, 64); return n }
	r := DedupeReport{
		State: h["state"], Apply: h["apply"] == "1", Scanned: num("scanned"), Groups: num("groups"),
		ToRemove: num("to_remove"), Visual: num("visual"), Ask: num("ask"), Merged: num("merged"), Distance: num("distance"), Error: h["error"],
	}
	if n := num("started_at"); n > 0 {
		r.Started = time.Unix(n, 0)
	}
	if n := num("finished_at"); n > 0 {
		r.Finished = time.Unix(n, 0)
	}
	var hist map[string]int64
	if json.Unmarshal([]byte(h["visual_hist"]), &hist) == nil {
		for p, n := range hist {
			r.VisualBy = append(r.VisualBy, SimCount{p, n})
		}
		sort.Slice(r.VisualBy, func(i, j int) bool { return r.VisualBy[i].Percent < r.VisualBy[j].Percent })
	}
	var raw []struct {
		K   int64  `json:"k"`
		D   int64  `json:"d"`
		Why string `json:"why"`
	}
	if json.Unmarshal([]byte(h["samples"]), &raw) != nil || len(raw) == 0 {
		return r
	}
	ids := make([]int64, 0, len(raw)*2)
	for _, p := range raw {
		ids = append(ids, p.K, p.D)
	}
	memes, err := s.store.MemesByIDs(ctx, ids)
	if err != nil {
		return r
	}
	s.decorate(memes...)
	byID := make(map[int64]*store.Meme, len(memes))
	for _, m := range memes {
		byID[m.ID] = m
	}
	for _, p := range raw {
		if k, d := byID[p.K], byID[p.D]; k != nil && d != nil {
			r.Samples = append(r.Samples, DedupePair{Keeper: k, Dup: d, Why: p.Why})
		}
	}
	return r
}

type ReviewPair struct {
	Keeper  *store.Meme
	Dup     *store.Meme
	Label   string
	Verdict string
	Reason  string
}

type ReviewReport struct {
	store.DedupeQueue
	State       string
	Running     bool
	Total       int64
	Merged      int64
	Started     time.Time
	Finished    time.Time
	NextPairs   []ReviewPair
	RecentPairs []ReviewPair
}

func (s *Server) reviewReport(ctx context.Context) ReviewReport {
	h, _ := s.rdb.HGetAll(ctx, "ms:dedupe:review").Result()
	num := func(k string) int64 { n, _ := strconv.ParseInt(h[k], 10, 64); return n }
	r := ReviewReport{State: h["state"], Total: num("total"), Merged: num("merged")}
	if n, _ := s.rdb.Exists(ctx, "ms:dedupe:review:lock").Result(); n > 0 {
		r.Running = true
	}
	if n := num("started_at"); n > 0 {
		r.Started = time.Unix(n, 0)
	}
	if n := num("finished_at"); n > 0 {
		r.Finished = time.Unix(n, 0)
	}
	q, err := s.store.DedupeQueue(ctx)
	if err != nil {
		return r
	}
	r.DedupeQueue = q
	ids := []int64{}
	for _, c := range append(append([]store.DedupeCandidate{}, q.Next...), q.Recent...) {
		ids = append(ids, c.A, c.B)
	}
	if len(ids) == 0 {
		return r
	}
	memes, err := s.store.MemesByIDs(ctx, ids)
	if err != nil {
		return r
	}
	s.decorate(memes...)
	byID := make(map[int64]*store.Meme, len(memes))
	for _, m := range memes {
		byID[m.ID] = m
	}
	pairs := func(cs []store.DedupeCandidate) []ReviewPair {
		var out []ReviewPair
		for _, c := range cs {
			if a, b := byID[c.A], byID[c.B]; a != nil && b != nil {
				out = append(out, ReviewPair{Keeper: a, Dup: b, Label: c.Label, Verdict: c.Verdict, Reason: c.Reason})
			}
		}
		return out
	}
	r.NextPairs, r.RecentPairs = pairs(q.Next), pairs(q.Recent)
	return r
}

func (s *Server) apiDedupeReview(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Stop bool `json:"stop"`
	}
	if err := decodeJSON(r, &req); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	if req.Stop {
		s.rdb.Set(r.Context(), "ms:dedupe:review:stop", "1", time.Hour)
		writeJSON(w, http.StatusOK, map[string]any{"stopping": true})
		return
	}
	if n, _ := s.rdb.Exists(r.Context(), "ms:dedupe:review:lock").Result(); n > 0 {
		writeErr(w, http.StatusConflict, errors.New("Codex is already checking duplicates"))
		return
	}
	if err := s.queue.Push(r.Context(), queue.StreamHigh, queue.Job{Type: "dedupe_review"}); err != nil {
		storeErr(w, err)
		return
	}
	s.rdb.HSet(r.Context(), "ms:dedupe:review", "state", "queued")
	writeJSON(w, http.StatusOK, map[string]any{"queued": true})
}

func (s *Server) apiDedupe(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Apply bool `json:"apply"`
	}
	if err := decodeJSON(r, &req); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	if n, _ := s.rdb.Exists(r.Context(), "ms:dedupe:lock").Result(); n > 0 {
		writeErr(w, http.StatusConflict, errors.New("a duplicate scan is already running"))
		return
	}
	if err := s.queue.Push(r.Context(), queue.StreamHigh, queue.Job{Type: "dedupe", Apply: req.Apply}); err != nil {
		storeErr(w, err)
		return
	}
	s.rdb.HSet(r.Context(), "ms:dedupe:report", "state", "queued", "apply", map[bool]int{true: 1, false: 0}[req.Apply])
	writeJSON(w, http.StatusOK, map[string]any{"queued": true})
}

func (s *Server) apiRetryMedia(w http.ResponseWriter, r *http.Request) {
	n, err := s.store.RetryMedia(r.Context())
	if err != nil {
		storeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"queued": n})
}

func (s *Server) apiRetryTgCache(w http.ResponseWriter, r *http.Request) {
	n, err := s.store.RetryTgCache(r.Context())
	if err != nil {
		storeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"queued": n})
}

func eta(left, perHour int64) string {
	switch {
	case left == 0:
		return "queue is empty"
	case perHour == 0:
		return "no analyses in the last hour"
	}
	h := float64(left) / float64(perHour)
	switch {
	case h < 1:
		return fmt.Sprintf("about %d min at this pace", max(1, int(h*60)))
	case h < 48:
		return fmt.Sprintf("about %.0f h at this pace", h)
	default:
		return fmt.Sprintf("about %.0f days at this pace", h/24)
	}
}
