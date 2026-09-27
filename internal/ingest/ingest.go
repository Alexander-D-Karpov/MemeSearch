package ingest

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path"
	"path/filepath"
	"strings"
	"unicode/utf8"

	"github.com/Alexander-D-Karpov/MemeSearch/internal/queue"
	"github.com/Alexander-D-Karpov/MemeSearch/internal/store"
)

var ErrUnsupported = errors.New("unsupported media type")
var ErrTooLarge = errors.New("file too large")

type Ingester struct {
	Store     *store.Store
	Queue     *queue.Queue
	UploadDir string
	MaxBytes  int64
}

type Result struct {
	ID        int64  `json:"id"`
	Name      string `json:"name"`
	Duplicate bool   `json:"duplicate"`
	Kind      string `json:"kind"`
	Error     string `json:"error,omitempty"`
}

type Type struct {
	Kind string
	Mime string
	Ext  string
}

func Sniff(head []byte) (Type, bool) {
	if len(head) >= 12 && string(head[4:8]) == "ftyp" {
		switch string(head[8:12]) {
		case "qt  ":
			return Type{"video", "video/quicktime", "mov"}, true
		case "heic", "heix", "hevc", "heim", "heis", "mif1", "msf1":
			return Type{"image", "image/heic", "heic"}, true
		case "avif", "avis":
			return Type{"image", "image/avif", "avif"}, true
		default:
			return Type{"video", "video/mp4", "mp4"}, true
		}
	}
	if len(head) >= 4 && bytes.Equal(head[:4], []byte{0x1A, 0x45, 0xDF, 0xA3}) {
		if bytes.Contains(head, []byte("webm")) {
			return Type{"video", "video/webm", "webm"}, true
		}
		return Type{"video", "video/x-matroska", "mkv"}, true
	}
	switch http.DetectContentType(head) {
	case "image/jpeg":
		return Type{"image", "image/jpeg", "jpg"}, true
	case "image/png":
		return Type{"image", "image/png", "png"}, true
	case "image/webp":
		return Type{"image", "image/webp", "webp"}, true
	case "image/bmp":
		return Type{"image", "image/bmp", "bmp"}, true
	case "image/gif":
		return Type{"gif", "image/gif", "gif"}, true
	case "video/webm":
		return Type{"video", "video/webm", "webm"}, true
	case "video/avi":
		return Type{"video", "video/x-msvideo", "avi"}, true
	case "video/mp4":
		return Type{"video", "video/mp4", "mp4"}, true
	}
	return Type{}, false
}

func RelPath(sha, ext string) string {
	return path.Join("originals", sha[:2], sha[2:4], sha+"."+ext)
}

type Meta struct {
	Name      string
	Source    string
	SourceRef string
	Caption   string
	Stream    string
}

func (in *Ingester) Save(ctx context.Context, r io.Reader, meta Meta) (*Result, error) {
	res := &Result{Name: meta.Name}
	tmpDir := filepath.Join(in.UploadDir, ".tmp")
	if err := os.MkdirAll(tmpDir, 0o755); err != nil {
		return nil, err
	}
	tmp, err := os.CreateTemp(tmpDir, "up-*")
	if err != nil {
		return nil, err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)

	head := make([]byte, 512)
	n, err := io.ReadFull(r, head)
	if err != nil && !errors.Is(err, io.ErrUnexpectedEOF) && !errors.Is(err, io.EOF) {
		tmp.Close()
		return nil, err
	}
	head = head[:n]
	t, ok := Sniff(head)
	if !ok {
		tmp.Close()
		return nil, ErrUnsupported
	}

	h := sha256.New()
	w := io.MultiWriter(tmp, h)
	if _, err := w.Write(head); err != nil {
		tmp.Close()
		return nil, err
	}
	limit := in.MaxBytes - int64(len(head))
	copied, err := io.Copy(w, io.LimitReader(r, limit+1))
	if err != nil {
		tmp.Close()
		return nil, err
	}
	if copied > limit {
		tmp.Close()
		return nil, ErrTooLarge
	}
	if err := tmp.Close(); err != nil {
		return nil, err
	}
	size := int64(len(head)) + copied
	sha := hex.EncodeToString(h.Sum(nil))
	res.Kind = t.Kind

	if existing, err := in.Store.MemeBySHA(ctx, sha); err == nil {
		res.ID = existing.ID
		res.Duplicate = true
		return res, nil
	} else if !errors.Is(err, store.ErrNotFound) {
		return nil, err
	}

	rel := RelPath(sha, t.Ext)
	dst := filepath.Join(in.UploadDir, filepath.FromSlash(rel))
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return nil, err
	}
	if err := os.Chmod(tmpName, 0o644); err != nil {
		return nil, err
	}
	if err := os.Rename(tmpName, dst); err != nil {
		return nil, fmt.Errorf("move: %w", err)
	}

	id, created, err := in.Store.InsertMeme(ctx, store.NewMeme{
		SHA256:       sha,
		Kind:         t.Kind,
		Mime:         t.Mime,
		Ext:          t.Ext,
		FilePath:     rel,
		SizeBytes:    size,
		OriginalName: cleanName(meta.Name),
		Source:       meta.Source,
		SourceRef:    meta.SourceRef,
		Caption:      meta.Caption,
	})
	if err != nil {
		if _, lookupErr := in.Store.MemeBySHA(context.WithoutCancel(ctx), sha); errors.Is(lookupErr, store.ErrNotFound) {
			os.Remove(dst)
		}
		return nil, err
	}
	res.ID = id
	if !created {
		res.Duplicate = true
		return res, nil
	}
	stream := meta.Stream
	if stream == "" {
		stream = queue.StreamHigh
	}
	if err := in.Queue.Process(ctx, stream, []int64{id}); err != nil {
		return nil, err
	}
	return res, nil
}

func cleanName(name string) string {
	name = filepath.Base(strings.ReplaceAll(name, "\\", "/"))
	if name == "." || name == "/" {
		return ""
	}
	for len(name) > 255 {
		_, size := utf8.DecodeLastRuneInString(name)
		name = name[:len(name)-size]
	}
	return strings.ToValidUTF8(name, "")
}

func (in *Ingester) RemoveFiles(m *store.Meme) {
	for _, rel := range []string{m.FilePath, m.ThumbPath} {
		if rel == "" || strings.Contains(rel, "..") {
			continue
		}
		os.Remove(filepath.Join(in.UploadDir, filepath.FromSlash(rel)))
	}
}
