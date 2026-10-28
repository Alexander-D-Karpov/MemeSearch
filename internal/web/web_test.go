package web

import (
	"bytes"
	"html"
	"html/template"
	"net/http/httptest"
	"net/url"
	"regexp"
	"strings"
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

func TestDashboardRendersDuplicatePanels(t *testing.T) {
	s := &Server{version: "test"}
	if err := s.loadTemplates(); err != nil {
		t.Fatal(err)
	}
	a, b := &store.Meme{ID: 1, ThumbURL: "/t/1.webp"}, &store.Meme{ID: 2, ThumbURL: "/t/2.webp"}
	d := Dashboard{
		Stats: &store.Stats{},
		Dedupe: DedupeReport{State: "preview", Groups: 1, ToRemove: 1, Visual: 1, Ask: 3,
			VisualBy: []SimCount{{"97", 1}}, Samples: []DedupePair{{Keeper: a, Dup: b, Why: "looks 98% alike, no text"}}},
		Review: ReviewReport{
			DedupeQueue: store.DedupeQueue{Todo: 3, Different: 2},
			State:       "paused: limited", Total: 5, Merged: 1,
			NextPairs:   []ReviewPair{{Keeper: a, Dup: b, Label: "looks 92% alike, text 80% shared"}},
			RecentPairs: []ReviewPair{{Keeper: a, Dup: b, Verdict: "different", Reason: "0.90 · other word"}},
		},
	}
	var buf bytes.Buffer
	if err := s.pages["admin/dashboard"].ExecuteTemplate(&buf, "layout", Page{Title: "Admin", Admin: true, Data: d}); err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"Ask Codex about 3 pairs", "looks 92% alike", "other word", "Merge 1 duplicates", "97%: 1"} {
		if !strings.Contains(buf.String(), want) {
			t.Errorf("dashboard is missing %q", want)
		}
	}
}

func TestIsCrawler(t *testing.T) {
	for ua, want := range map[string]bool{
		"TelegramBot (like TwitterBot)":                                     true,
		"Mozilla/5.0 (compatible; ExaSearchBot/1.0)":                        true,
		"facebookexternalhit/1.1":                                           true,
		"WhatsApp/2.23.20.0":                                                true,
		"Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)": true,
		"": true,
		"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36":                                       false,
		"Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1": false,
	} {
		if got := isCrawler(ua); got != want {
			t.Errorf("isCrawler(%q) = %v, want %v", ua, got, want)
		}
	}
}
