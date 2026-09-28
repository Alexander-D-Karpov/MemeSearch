package web

import (
	"bytes"
	"errors"
	"image"
	"image/draw"
	"image/jpeg"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	_ "image/gif"
	_ "image/png"

	_ "golang.org/x/image/bmp"
	xdraw "golang.org/x/image/draw"
	_ "golang.org/x/image/webp"

	"github.com/Alexander-D-Karpov/MemeSearch/internal/store"
)

const (
	ogMaxDirectBytes = 5 << 20
	photoMaxSide     = 2048
	maxDecodePixels  = 40_000_000
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
	s.serveJPEG(w, r, false)
}

func (s *Server) memePhoto(w http.ResponseWriter, r *http.Request) {
	s.serveJPEG(w, r, true)
}

func (s *Server) serveJPEG(w http.ResponseWriter, r *http.Request, full bool) {
	m, err := s.visibleMeme(r)
	if errors.Is(err, store.ErrNotFound) {
		http.NotFound(w, r)
		return
	}
	if err != nil {
		slog.Error("jpeg preview", "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	var src image.Image
	if full && m.Kind != "video" {
		src, err = s.decodeMedia(m.FilePath)
		if err != nil {
			slog.Debug("jpeg preview original", "id", m.ID, "err", err)
		}
	}
	if src == nil && m.ThumbPath != "" {
		src, err = s.decodeMedia(m.ThumbPath)
	}
	if src == nil {
		if err != nil {
			slog.Warn("jpeg preview decode", "id", m.ID, "err", err)
		}
		http.NotFound(w, r)
		return
	}
	b, err := encodeJPEG(src, photoMaxSide)
	if err != nil {
		http.Error(w, "encode error", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "image/jpeg")
	w.Header().Set("Content-Length", strconv.Itoa(len(b)))
	w.Header().Set("Cache-Control", "public, max-age=86400")
	w.Write(b)
}

func (s *Server) decodeMedia(rel string) (image.Image, error) {
	if rel == "" {
		return nil, errors.New("no file")
	}
	f, err := os.Open(filepath.Join(s.cfg.UploadDir, filepath.FromSlash(rel)))
	if err != nil {
		return nil, err
	}
	defer f.Close()
	cfg, _, err := image.DecodeConfig(f)
	if err != nil {
		return nil, err
	}
	if cfg.Width*cfg.Height > maxDecodePixels {
		return nil, errors.New("image too large to convert")
	}
	if _, err := f.Seek(0, io.SeekStart); err != nil {
		return nil, err
	}
	img, _, err := image.Decode(f)
	return img, err
}

func encodeJPEG(src image.Image, maxSide int) ([]byte, error) {
	b := src.Bounds()
	w, h := b.Dx(), b.Dy()
	if scale := float64(maxSide) / float64(max(w, h)); scale < 1 {
		w, h = max(1, int(float64(w)*scale)), max(1, int(float64(h)*scale))
	}
	dst := image.NewRGBA(image.Rect(0, 0, w, h))
	draw.Draw(dst, dst.Bounds(), image.White, image.Point{}, draw.Src)
	if w == b.Dx() && h == b.Dy() {
		draw.Draw(dst, dst.Bounds(), src, b.Min, draw.Over)
	} else {
		xdraw.CatmullRom.Scale(dst, dst.Bounds(), src, b, draw.Over, nil)
	}
	var buf bytes.Buffer
	for _, q := range []int{88, 75, 60} {
		buf.Reset()
		if err := jpeg.Encode(&buf, dst, &jpeg.Options{Quality: q}); err != nil {
			return nil, err
		}
		if buf.Len() <= ogMaxDirectBytes {
			break
		}
	}
	return buf.Bytes(), nil
}
