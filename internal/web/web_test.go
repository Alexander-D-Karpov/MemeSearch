package web

import (
	"bytes"
	"html"
	"html/template"
	"net/http/httptest"
	"net/url"
	"regexp"
	"testing"

	"github.com/Alexander-D-Karpov/MemeSearch/internal/store"
)

func TestSafeNext(t *testing.T) {
	cases := map[string]string{
		"":                     "/admin",
		"/admin/codex":         "/admin/codex",
		"/m/12?x=1":            "/m/12?x=1",
		"https://evil.example": "/admin",
		"//evil.example/path":  "/admin",
		"javascript:alert(1)":  "/admin",
		"admin":                "/admin",
	}
	for in, want := range cases {
		if got := safeNext(in); got != want {
			t.Errorf("safeNext(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestAspect(t *testing.T) {
	s := &Server{}
	aspect := s.funcs()["aspect"].(func(int, int) template.CSS)
	cases := []struct {
		w, h int
		want template.CSS
	}{
		{0, 0, "aspect-ratio: 1 / 1"},
		{600, 600, "aspect-ratio: 1000 / 1000"},
		{800, 600, "aspect-ratio: 1000 / 750"},
		{100, 1000, "aspect-ratio: 1000 / 2200"},
		{1000, 100, "aspect-ratio: 1000 / 450"},
	}
	for _, c := range cases {
		if got := aspect(c.w, c.h); got != c.want {
			t.Errorf("aspect(%d, %d) = %q, want %q", c.w, c.h, got, c.want)
		}
	}
}

func TestNavKey(t *testing.T) {
	cases := map[string]string{
		"/admin":                     "dashboard",
		"/admin/upload":              "upload",
		"/admin/memes":               "memes",
		"/admin/memes?status=failed": "failed",
		"/admin/m/3/edit":            "memes",
		"/admin/codex":               "codex",
		"/admin/settings":            "settings",
		"/":                          "",
	}
	for target, want := range cases {
		if got := navKey(httptest.NewRequest("GET", target, nil)); got != want {
			t.Errorf("navKey(%q) = %q, want %q", target, got, want)
		}
	}
}

func TestHumanBytes(t *testing.T) {
	cases := map[int64]string{
		512:     "512 B",
		2048:    "2.0 KiB",
		5 << 20: "5.0 MiB",
		3 << 30: "3.0 GiB",
	}
	for n, want := range cases {
		if got := humanBytes(n); got != want {
			t.Errorf("humanBytes(%d) = %q, want %q", n, got, want)
		}
	}
}

func TestTemplatesParse(t *testing.T) {
	s := &Server{version: "test"}
	if err := s.loadTemplates(); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"index", "meme", "login", "error", "admin/dashboard", "admin/upload", "admin/memes", "admin/edit", "admin/codex", "admin/settings"} {
		if _, ok := s.pages[name]; !ok {
			t.Errorf("template %q not loaded", name)
		}
	}
}

func TestMemeChipLinksDecodeToTheTag(t *testing.T) {
	s := &Server{version: "test"}
	if err := s.loadTemplates(); err != nil {
		t.Fatal(err)
	}
	m := &store.Meme{
		ID:       1,
		Status:   "done",
		Kind:     "image",
		Template: "Поле чудес",
		Tags:     []string{"леонид якубович", "что падает каждый день"},
		People:   []string{"Леонид Якубович — «Поле чудес»"},
		Objects:  []string{"microphone / микрофон"},
		Sounds:   []string{"music / музыка"},
		Song:     "Artist — Песня & Co",
	}
	var buf bytes.Buffer
	if err := s.pages["meme"].ExecuteTemplate(&buf, "layout", Page{Title: "x", Data: MemePage{Meme: m}}); err != nil {
		t.Fatal(err)
	}
	want := map[string]bool{"Поле чудес": false, "Artist — Песня & Co": false}
	for _, v := range append(append(append(append([]string{}, m.Tags...), m.People...), m.Objects...), m.Sounds...) {
		want[v] = false
	}
	for _, href := range regexp.MustCompile(`href="/\?q=([^"]*)"`).FindAllStringSubmatch(buf.String(), -1) {
		u, err := url.Parse("/?q=" + html.UnescapeString(href[1]))
		if err != nil {
			t.Fatal(err)
		}
		q := u.Query().Get("q")
		if _, ok := want[q]; !ok {
			t.Errorf("link %q searches for %q", href[1], q)
		}
		want[q] = true
	}
	for v, seen := range want {
		if !seen {
			t.Errorf("no search link for %q", v)
		}
	}
}
