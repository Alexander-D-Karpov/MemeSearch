package web

import (
	"bytes"
	"fmt"
	"html/template"
	"io/fs"
	"log/slog"
	"net/http"
	"net/url"
	"path"
	"strings"
	"time"

	"github.com/Alexander-D-Karpov/MemeSearch/internal/auth"
	assets "github.com/Alexander-D-Karpov/MemeSearch/web"
)

type Page struct {
	Title       string
	Description string
	Admin       bool
	Query       string
	Kind        string
	Canonical   string
	OGImage     string
	Data        any
}

func (s *Server) funcs() template.FuncMap {
	return template.FuncMap{
		"static": func(p string) string { return "/static/" + p + "?v=" + s.version },
		"bytes":  humanBytes,
		"dur": func(ms int) string {
			d := time.Duration(ms) * time.Millisecond
			return fmt.Sprintf("%d:%02d", int(d.Minutes()), int(d.Seconds())%60)
		},
		"ago": func(t any) string {
			var tm time.Time
			switch v := t.(type) {
			case time.Time:
				tm = v
			case *time.Time:
				if v == nil {
					return "—"
				}
				tm = *v
			default:
				return ""
			}
			return ago(tm)
		},
		"datetime": func(t time.Time) string { return t.UTC().Format(time.RFC3339) },
		"deref": func(t *time.Time) time.Time {
			if t == nil {
				return time.Time{}
			}
			return *t
		},
		"q":    url.QueryEscape,
		"join": strings.Join,
		"truncate": func(n int, s string) string {
			r := []rune(s)
			if len(r) <= n {
				return s
			}
			return string(r[:n]) + "…"
		},
		"lines": func(s string) []string { return strings.Split(strings.TrimSpace(s), "\n") },
		"add":   func(a, b int) int { return a + b },
		"pct": func(a, b int64) int64 {
			if b == 0 {
				return 0
			}
			return a * 100 / b
		},
	}
}

func (s *Server) loadTemplates() error {
	s.pages = map[string]*template.Template{}
	shared := []string{"templates/layout.html", "templates/partials.html"}
	pages, err := fs.Glob(assets.FS, "templates/pages/*.html")
	if err != nil {
		return err
	}
	admin, err := fs.Glob(assets.FS, "templates/pages/admin/*.html")
	if err != nil {
		return err
	}
	for _, p := range append(pages, admin...) {
		name := strings.TrimSuffix(strings.TrimPrefix(p, "templates/pages/"), ".html")
		t, err := template.New(path.Base(p)).Funcs(s.funcs()).ParseFS(assets.FS, append(shared, p)...)
		if err != nil {
			return fmt.Errorf("template %s: %w", p, err)
		}
		s.pages[name] = t
	}
	partials, err := template.New("partials").Funcs(s.funcs()).ParseFS(assets.FS, "templates/partials.html")
	if err != nil {
		return err
	}
	s.pages["_partials"] = partials
	return nil
}

func (s *Server) render(w http.ResponseWriter, r *http.Request, code int, name string, p Page) {
	t, ok := s.pages[name]
	if !ok {
		http.Error(w, "template not found", http.StatusInternalServerError)
		return
	}
	p.Admin = auth.IsAdmin(r.Context())
	if p.Canonical == "" {
		p.Canonical = s.cfg.PublicURL + r.URL.Path
	}
	var buf bytes.Buffer
	if err := t.ExecuteTemplate(&buf, "layout", p); err != nil {
		slog.Error("render", "page", name, "err", err)
		http.Error(w, "render error", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	if p.Admin {
		w.Header().Set("Cache-Control", "private, no-store")
	}
	w.WriteHeader(code)
	w.Write(buf.Bytes())
}

func (s *Server) renderPartial(w http.ResponseWriter, name string, data any) {
	var buf bytes.Buffer
	if err := s.pages["_partials"].ExecuteTemplate(&buf, name, data); err != nil {
		slog.Error("render partial", "name", name, "err", err)
		http.Error(w, "render error", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Write(buf.Bytes())
}

func (s *Server) notFound(w http.ResponseWriter, r *http.Request) {
	s.render(w, r, http.StatusNotFound, "error", Page{Title: "Not found", Data: "This meme does not exist or is not public."})
}

func humanBytes(n int64) string {
	const unit = 1024
	if n < unit {
		return fmt.Sprintf("%d B", n)
	}
	div, exp := int64(unit), 0
	for m := n / unit; m >= unit; m /= unit {
		div *= unit
		exp++
	}
	return fmt.Sprintf("%.1f %ciB", float64(n)/float64(div), "KMGTPE"[exp])
}

func ago(t time.Time) string {
	d := time.Since(t)
	switch {
	case d < time.Minute:
		return "just now"
	case d < time.Hour:
		return fmt.Sprintf("%dm ago", int(d.Minutes()))
	case d < 48*time.Hour:
		return fmt.Sprintf("%dh ago", int(d.Hours()))
	default:
		return t.Format("2006-01-02")
	}
}
