package config

import (
	"fmt"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

type Config struct {
	Addr              string
	PublicURL         string
	DatabaseURL       string
	RedisURL          string
	UploadDir         string
	MediaURL          string
	ServeMedia        bool
	AdminUsername     string
	AdminPassword     string
	AdminPasswordHash string
	AdminAPIToken     string
	CookieSecure      bool
	SessionTTL        time.Duration
	MLURL             string
	InternalToken     string
	MaxFileBytes      int64
	MaxZipBytes       int64
	SearchCacheTTL    time.Duration
	EmbedCacheTTL     time.Duration
	PageSize          int
	TextMaxDist       float64
	ClipMaxDist       float64
	LookAlikeMaxDist  float64
	TrustProxy        bool
}

func Load() (*Config, error) {
	c := &Config{
		Addr:              env("WEB_ADDR", ":8080"),
		PublicURL:         strings.TrimRight(env("PUBLIC_URL", "http://localhost:8080"), "/"),
		DatabaseURL:       env("DATABASE_URL", ""),
		RedisURL:          env("REDIS_URL", "redis://redis:6379/0"),
		UploadDir:         env("UPLOAD_DIR", "/media"),
		MediaURL:          strings.TrimRight(env("MEDIA_URL", "/media"), "/"),
		ServeMedia:        envBool("SERVE_MEDIA", false),
		AdminUsername:     env("ADMIN_USERNAME", "admin"),
		AdminPassword:     env("ADMIN_PASSWORD", ""),
		AdminPasswordHash: env("ADMIN_PASSWORD_HASH", ""),
		AdminAPIToken:     env("ADMIN_API_TOKEN", ""),
		CookieSecure:      envBool("COOKIE_SECURE", true),
		SessionTTL:        envDuration("SESSION_TTL", 30*24*time.Hour),
		MLURL:             strings.TrimRight(env("ML_URL", "http://ml:8001"), "/"),
		InternalToken:     env("INTERNAL_TOKEN", ""),
		MaxFileBytes:      envInt64("MAX_FILE_MB", 512) << 20,
		MaxZipBytes:       envInt64("MAX_ZIP_GB", 100) << 30,
		SearchCacheTTL:    envDuration("SEARCH_CACHE_TTL", 10*time.Minute),
		EmbedCacheTTL:     envDuration("EMBED_CACHE_TTL", 30*24*time.Hour),
		PageSize:          int(envInt64("PAGE_SIZE", 48)),
		TextMaxDist:       envFloat("SEARCH_TEXT_MAX_DIST", 2),
		ClipMaxDist:       envFloat("SEARCH_CLIP_MAX_DIST", 2),
		LookAlikeMaxDist:  envFloat("LOOKALIKE_MAX_DIST", 0.35),
		TrustProxy:        envBool("TRUST_PROXY", true),
	}
	if c.DatabaseURL == "" {
		return nil, fmt.Errorf("DATABASE_URL is required")
	}
	if c.AdminPassword == "" && c.AdminPasswordHash == "" {
		return nil, fmt.Errorf("ADMIN_PASSWORD or ADMIN_PASSWORD_HASH is required")
	}
	if c.InternalToken == "" {
		return nil, fmt.Errorf("INTERNAL_TOKEN is required")
	}
	if _, err := url.Parse(c.PublicURL); err != nil {
		return nil, fmt.Errorf("PUBLIC_URL: %w", err)
	}
	return c, nil
}

func (c *Config) PublicHost() string {
	u, err := url.Parse(c.PublicURL)
	if err != nil {
		return ""
	}
	return u.Host
}

func env(key, def string) string {
	if v, ok := os.LookupEnv(key); ok && strings.TrimSpace(v) != "" {
		return strings.TrimSpace(v)
	}
	return def
}

func envBool(key string, def bool) bool {
	v, err := strconv.ParseBool(env(key, strconv.FormatBool(def)))
	if err != nil {
		return def
	}
	return v
}

func envInt64(key string, def int64) int64 {
	v, err := strconv.ParseInt(env(key, strconv.FormatInt(def, 10)), 10, 64)
	if err != nil {
		return def
	}
	return v
}

func envFloat(key string, def float64) float64 {
	v, err := strconv.ParseFloat(env(key, strconv.FormatFloat(def, 'f', -1, 64)), 64)
	if err != nil {
		return def
	}
	return v
}

func envDuration(key string, def time.Duration) time.Duration {
	v, err := time.ParseDuration(env(key, def.String()))
	if err != nil {
		return def
	}
	return v
}
