package web

import (
	"bytes"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"html/template"
	"image"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/Alexander-D-Karpov/MemeSearch/internal/auth"
	"github.com/Alexander-D-Karpov/MemeSearch/internal/ingest"
)

const (
	imageQueryMaxBytes = 20 << 20
	imageQueryTTL      = time.Hour
	imageQueryPerMin   = 20
	imagePageSize      = 48
)

var errImageQueryExpired = errors.New("this picture search has expired, upload the picture again")

type imageQuery struct {
	Clip    []float32 `json:"clip"`
	Preview string    `json:"preview,omitempty"`
}

func imageQueryKey(token string) string { return "ms:imgq:" + token }

func (s *Server) doImageSearch(w http.ResponseWriter, r *http.Request) {
	ip := auth.ClientIP(r, s.cfg.TrustProxy)
	rl := fmt.Sprintf("ms:rl:img:%s:%d", ip, time.Now().Unix()/60)
	if n, err := s.rdb.Incr(r.Context(), rl).Result(); err == nil {
		if n == 1 {
			s.rdb.Expire(r.Context(), rl, 2*time.Minute)
		}
		if n > imageQueryPerMin {
			s.imageSearchError(w, r, http.StatusTooManyRequests, errors.New("too many picture searches, wait a minute"))
			return
		}
	}
	data, err := readImageUpload(w, r)
	if err != nil {
		s.imageSearchError(w, r, http.StatusBadRequest, err)
		return
	}
	clip, err := s.ml.EmbedImage(r.Context(), data)
	if err != nil {
		slog.Warn("image search embed", "err", err)
		s.imageSearchError(w, r, http.StatusBadGateway, errors.New("picture search is unavailable right now, try again in a minute"))
		return
	}
	q := imageQuery{Clip: clip}
	if img, _, err := image.Decode(bytes.NewReader(data)); err == nil {
		if b, err := encodeJPEG(img, 240); err == nil {
			q.Preview = "data:image/jpeg;base64," + base64.StdEncoding.EncodeToString(b)
		}
	}
	raw, _ := json.Marshal(q)
	token := randomToken()
	if err := s.rdb.Set(r.Context(), imageQueryKey(token), raw, imageQueryTTL).Err(); err != nil {
		s.imageSearchError(w, r, http.StatusInternalServerError, errors.New("could not start the search"))
		return
	}
	v := url.Values{"img": {token}}
	if kind := validKind(r.FormValue("kind")); kind != "" {
		v.Set("kind", kind)
	}
	target := "/?" + v.Encode()
	if strings.Contains(r.Header.Get("Accept"), "application/json") {
		writeJSON(w, http.StatusOK, map[string]string{"url": target})
		return
	}
	http.Redirect(w, r, target, http.StatusSeeOther)
}

func readImageUpload(w http.ResponseWriter, r *http.Request) ([]byte, error) {
	r.Body = http.MaxBytesReader(w, r.Body, imageQueryMaxBytes+1<<20)
	var src io.Reader = r.Body
	if strings.HasPrefix(r.Header.Get("Content-Type"), "multipart/") {
		f, _, err := r.FormFile("image")
		if err != nil {
			return nil, errors.New("choose a picture to search with")
		}
		defer f.Close()
		src = f
	}
	data, err := io.ReadAll(io.LimitReader(src, imageQueryMaxBytes+1))
	if err != nil {
		return nil, errors.New("could not read the picture")
	}
	if len(data) > imageQueryMaxBytes {
		return nil, errors.New("the picture is larger than 20 MB")
	}
	t, ok := ingest.Sniff(data)
	if !ok || t.Kind == "video" {
		return nil, errors.New("that is not a picture (jpg, png, webp, gif, heic, avif, bmp)")
	}
	return data, nil
}

func (s *Server) imageSearchError(w http.ResponseWriter, r *http.Request, code int, err error) {
	if strings.Contains(r.Header.Get("Accept"), "application/json") {
		writeErr(w, code, err)
		return
	}
	s.render(w, r, code, "error", Page{Title: "Picture search", Data: err.Error()})
}

func (s *Server) loadImageQuery(r *http.Request, token string) (*imageQuery, error) {
	if len(token) != 32 {
		return nil, errImageQueryExpired
	}
	raw, err := s.rdb.Get(r.Context(), imageQueryKey(token)).Bytes()
	if err != nil {
		return nil, errImageQueryExpired
	}
	var q imageQuery
	if err := json.Unmarshal(raw, &q); err != nil || len(q.Clip) == 0 {
		return nil, errImageQueryExpired
	}
	return &q, nil
}

func (s *Server) visualResults(r *http.Request, res *Results) error {
	offset := intParam(r, "offset", 0, 0, 5000)
	var clip []float32
	var exclude int64
	if token := r.URL.Query().Get("img"); token != "" {
		q, err := s.loadImageQuery(r, token)
		if err != nil {
			return err
		}
		clip, res.ImgToken, res.Preview = q.Clip, token, template.URL(q.Preview)
	} else {
		id, err := strconv.ParseInt(r.URL.Query().Get("like"), 10, 64)
		if err != nil {
			return errors.New("bad meme id")
		}
		if m, err := s.store.Meme(r.Context(), id); err != nil || (m.Hidden || m.Status != "done") && !auth.IsAdmin(r.Context()) {
			return errors.New("this meme does not exist or is not public")
		} else {
			s.decorate(m)
			res.Like = m
		}
		if clip, err = s.search.MemeVector(r.Context(), id); err != nil || clip == nil {
			return errors.New("this meme has no picture vector yet")
		}
		exclude = id
	}
	start := time.Now()
	memes, err := s.search.ByImage(r.Context(), clip, res.Kind, exclude, imagePageSize, offset)
	if err != nil {
		return err
	}
	s.decorate(memes...)
	res.Memes, res.Visual, res.TookMs = memes, true, time.Since(start).Milliseconds()
	if len(memes) == imagePageSize && offset+imagePageSize < 5000 {
		res.NextOffset = offset + imagePageSize
	}
	return nil
}

func randomToken() string {
	b := make([]byte, 16)
	rand.Read(b)
	return hex.EncodeToString(b)
}
