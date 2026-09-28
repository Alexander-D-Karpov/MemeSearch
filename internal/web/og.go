package web

import (
	"bytes"
	"errors"
	"image"
	"image/draw"
	"image/jpeg"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	_ "image/gif"
	_ "image/png"

	_ "golang.org/x/image/webp"

	"github.com/Alexander-D-Karpov/MemeSearch/internal/store"
)

const (
	ogMaxDirectBytes = 5 << 20
	thumbMaxW        = 480
	thumbMaxH        = 1440
)

func (s *Server) absURL(u string) string {
	if strings.HasPrefix(u, "/") {
		return strings.TrimSuffix(s.cfg.PublicURL, "/") + u
	}
	return u
}

func (s *Server) memeOG(m *store.Meme) OpenGraph {
	og := OpenGraph{Type: "article", ImageAlt: firstNonEmpty(m.Title, m.Description, "Meme")}
	switch {
	case m.Kind != "video" && directOGExt(m.Ext) && m.SizeBytes > 0 && m.SizeBytes <= ogMaxDirectBytes && m.URL != "":
		og.Image = s.absURL(m.URL)
		og.ImageType = m.Mime
		og.ImageW, og.ImageH = m.Width, m.Height
	case m.ThumbPath != "":
		og.Image = s.absURL("/m/" + strconv.FormatInt(m.ID, 10) + "/og.jpg")
		og.ImageType = "image/jpeg"
		og.ImageW, og.ImageH = thumbSize(m.Width, m.Height)
	}
	if m.Kind == "video" && m.URL != "" {
		og.Type = "video.other"
		og.Video = s.absURL(m.URL)
		og.VideoType = m.Mime
		og.VideoW, og.VideoH = m.Width, m.Height
	}
	return og
}

func directOGExt(ext string) bool {
	switch strings.ToLower(ext) {
	case "jpg", "jpeg", "png", "gif":
		return true
	}
	return false
}

func thumbSize(w, h int) (int, int) {
	if w <= 0 || h <= 0 {
		return 0, 0
	}
	scale := min(1, float64(thumbMaxW)/float64(w), float64(thumbMaxH)/float64(h))
	return max(1, int(float64(w)*scale)), max(1, int(float64(h)*scale))
}

func firstNonEmpty(vals ...string) string {
	for _, v := range vals {
		if v = strings.TrimSpace(v); v != "" {
			return v
		}
	}
	return ""
}

func clip(s string, n int) string {
	s = strings.Join(strings.Fields(s), " ")
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n-1]) + "…"
}

func (s *Server) memeOGImage(w http.ResponseWriter, r *http.Request) {
	m, err := s.visibleMeme(r)
	if errors.Is(err, store.ErrNotFound) || (err == nil && m.ThumbPath == "") {
		http.NotFound(w, r)
		return
	}
	if err != nil {
		slog.Error("og image", "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	f, err := os.Open(filepath.Join(s.cfg.UploadDir, filepath.FromSlash(m.ThumbPath)))
	if err != nil {
		http.NotFound(w, r)
		return
	}
	defer f.Close()
	src, _, err := image.Decode(f)
	if err != nil {
		slog.Warn("og image decode", "id", m.ID, "err", err)
		http.NotFound(w, r)
		return
	}
	dst := image.NewRGBA(src.Bounds())
	draw.Draw(dst, dst.Bounds(), image.White, image.Point{}, draw.Src)
	draw.Draw(dst, dst.Bounds(), src, src.Bounds().Min, draw.Over)
	var buf bytes.Buffer
	if err := jpeg.Encode(&buf, dst, &jpeg.Options{Quality: 85}); err != nil {
		http.Error(w, "encode error", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "image/jpeg")
	w.Header().Set("Content-Length", strconv.Itoa(buf.Len()))
	w.Header().Set("Cache-Control", "public, max-age=86400")
	w.Write(buf.Bytes())
}
