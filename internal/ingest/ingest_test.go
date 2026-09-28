package ingest

import (
	"strings"
	"testing"
	"unicode/utf8"
)

func TestSniff(t *testing.T) {
	cases := []struct {
		name string
		head []byte
		kind string
		ext  string
		ok   bool
	}{
		{"jpeg", []byte("\xff\xd8\xff\xe0\x00\x10JFIF\x00"), "image", "jpg", true},
		{"png", []byte("\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR"), "image", "png", true},
		{"gif", []byte("GIF89a\x01\x00\x01\x00"), "gif", "gif", true},
		{"webp", []byte("RIFF\x24\x00\x00\x00WEBPVP8 "), "image", "webp", true},
		{"mp4", []byte("\x00\x00\x00\x20ftypisom\x00\x00\x02\x00"), "video", "mp4", true},
		{"mov", []byte("\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00"), "video", "mov", true},
		{"heic", []byte("\x00\x00\x00\x18ftypheic\x00\x00\x00\x00"), "image", "heic", true},
		{"avif", []byte("\x00\x00\x00\x1cftypavif\x00\x00\x00\x00"), "image", "avif", true},
		{"webm", []byte("\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01webm"), "video", "webm", true},
		{"mkv", []byte("\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01matroska"), "video", "mkv", true},
		{"text", []byte("hello world, not media"), "", "", false},
		{"empty", nil, "", "", false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got, ok := Sniff(c.head)
			if ok != c.ok || got.Kind != c.kind || got.Ext != c.ext {
				t.Fatalf("Sniff(%s) = %+v, %v; want kind=%q ext=%q ok=%v", c.name, got, ok, c.kind, c.ext, c.ok)
			}
		})
	}
}

func TestRelPath(t *testing.T) {
	sha := "cd57fbedcca7b1b947ad3ac670b6fb3bdffd56c255ca44973fe7c09ac2bd5c05"
	want := "originals/cd/57/" + sha + ".jpg"
	if got := RelPath(sha, "jpg"); got != want {
		t.Fatalf("RelPath = %q, want %q", got, want)
	}
}

func TestCleanName(t *testing.T) {
	if got := cleanName("folder/sub/meme.png"); got != "meme.png" {
		t.Fatalf("path not stripped: %q", got)
	}
	if got := cleanName(`C:\Users\me\meme.png`); got != "meme.png" {
		t.Fatalf("windows path not stripped: %q", got)
	}
	long := strings.Repeat("ж", 200) + ".jpg"
	got := cleanName(long)
	if len(got) > 255 {
		t.Fatalf("name is %d bytes, want <= 255", len(got))
	}
	if !utf8.ValidString(got) {
		t.Fatalf("truncated name is not valid UTF-8: %q", got)
	}
}
