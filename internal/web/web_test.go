package web

import (
	"html/template"
	"net/http/httptest"
	"testing"
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
