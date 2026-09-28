package web

import (
	"bytes"
	"image"
	"image/jpeg"
	"testing"

	"github.com/Alexander-D-Karpov/MemeSearch/internal/config"
	"github.com/Alexander-D-Karpov/MemeSearch/internal/store"
)

func TestThumbSize(t *testing.T) {
	cases := []struct{ w, h, ww, wh int }{
		{1920, 1080, 480, 270},
		{300, 200, 300, 200},
		{500, 5000, 144, 1440},
		{0, 100, 0, 0},
	}
	for _, c := range cases {
		if w, h := thumbSize(c.w, c.h); w != c.ww || h != c.wh {
			t.Errorf("thumbSize(%d,%d) = %d,%d want %d,%d", c.w, c.h, w, h, c.ww, c.wh)
		}
	}
}

func TestClip(t *testing.T) {
	if got := clip("  a \n b  ", 10); got != "a b" {
		t.Errorf("clip whitespace = %q", got)
	}
	if got := clip("абвгдеёжзи", 5); got != "абвг…" {
		t.Errorf("clip runes = %q", got)
	}
}

func TestMemeOG(t *testing.T) {
	s := &Server{cfg: &config.Config{PublicURL: "https://ms.example"}}
	img := &store.Meme{ID: 7, Kind: "image", Ext: "png", Mime: "image/png", SizeBytes: 1000, Width: 800, Height: 600, URL: "/media/originals/a.png", ThumbPath: "thumbs/a.webp"}
	og := s.memeOG(img)
	if og.Image != "https://ms.example/media/originals/a.png" || og.ImageW != 800 || og.Type != "article" {
		t.Errorf("image og = %+v", og)
	}
	img.Ext, img.Mime = "webp", "image/webp"
	og = s.memeOG(img)
	if og.Image != "https://ms.example/m/7/og.jpg" || og.ImageType != "image/jpeg" || og.ImageW != 480 || og.ImageH != 360 {
		t.Errorf("webp og = %+v", og)
	}
	vid := &store.Meme{ID: 9, Kind: "video", Ext: "mp4", Mime: "video/mp4", Width: 1280, Height: 720, URL: "https://cdn.example/v.mp4", ThumbPath: "thumbs/v.webp"}
	og = s.memeOG(vid)
	if og.Type != "video.other" || og.Video != "https://cdn.example/v.mp4" || og.Image != "https://ms.example/m/9/og.jpg" || og.VideoW != 1280 {
		t.Errorf("video og = %+v", og)
	}
}

func TestChannelUsername(t *testing.T) {
	ok := map[string]string{
		"@BestMemes":                         "bestmemes",
		"bestmemes":                          "bestmemes",
		"https://t.me/bestmemes":             "bestmemes",
		"https://t.me/s/bestmemes":           "bestmemes",
		"t.me/bestmemes/123":                 "bestmemes",
		"https://telegram.me/best_memes?x=1": "best_memes",
	}
	for in, want := range ok {
		if got, err := channelUsername(in); err != nil || got != want {
			t.Errorf("channelUsername(%q) = %q, %v; want %q", in, got, err, want)
		}
	}
	for _, in := range []string{"", "abc", "https://t.me/+invitehash", "https://t.me/joinchat/xyz", "bad name", "1memes"} {
		if got, err := channelUsername(in); err == nil {
			t.Errorf("channelUsername(%q) = %q, want error", in, got)
		}
	}
}

func TestEncodeJPEGScales(t *testing.T) {
	src := image.NewNRGBA(image.Rect(0, 0, 4000, 1000))
	b, err := encodeJPEG(src, 2048)
	if err != nil {
		t.Fatal(err)
	}
	cfg, err := jpeg.DecodeConfig(bytes.NewReader(b))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Width != 2048 || cfg.Height != 512 {
		t.Fatalf("got %dx%d, want 2048x512", cfg.Width, cfg.Height)
	}
}
