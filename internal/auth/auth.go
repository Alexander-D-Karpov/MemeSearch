package auth

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
	"golang.org/x/crypto/bcrypt"

	"github.com/Alexander-D-Karpov/MemeSearch/internal/config"
)

const CookieName = "ms_session"

var ErrRateLimited = errors.New("too many attempts, try again later")
var ErrBadCredentials = errors.New("invalid username or password")

type Auth struct {
	cfg *config.Config
	rdb *redis.Client
}

func New(cfg *config.Config, rdb *redis.Client) *Auth {
	return &Auth{cfg: cfg, rdb: rdb}
}

type ctxKey struct{}

func WithAdmin(ctx context.Context) context.Context {
	return context.WithValue(ctx, ctxKey{}, true)
}

func IsAdmin(ctx context.Context) bool {
	v, _ := ctx.Value(ctxKey{}).(bool)
	return v
}

func (a *Auth) Login(ctx context.Context, ip, username, password string) (string, error) {
	key := "ms:login:fail:" + ip
	fails, _ := a.rdb.Get(ctx, key).Int()
	if fails >= 10 {
		return "", ErrRateLimited
	}
	if !a.checkCredentials(username, password) {
		pipe := a.rdb.TxPipeline()
		pipe.Incr(ctx, key)
		pipe.Expire(ctx, key, 15*time.Minute)
		pipe.Exec(ctx)
		return "", ErrBadCredentials
	}
	a.rdb.Del(ctx, key)
	buf := make([]byte, 32)
	if _, err := rand.Read(buf); err != nil {
		return "", err
	}
	token := base64.RawURLEncoding.EncodeToString(buf)
	if err := a.rdb.Set(ctx, sessionKey(token), username, a.cfg.SessionTTL).Err(); err != nil {
		return "", err
	}
	return token, nil
}

func (a *Auth) checkCredentials(username, password string) bool {
	userOK := subtle.ConstantTimeCompare([]byte(username), []byte(a.cfg.AdminUsername)) == 1
	var passOK bool
	if a.cfg.AdminPasswordHash != "" {
		passOK = bcrypt.CompareHashAndPassword([]byte(a.cfg.AdminPasswordHash), []byte(password)) == nil
	} else {
		passOK = subtle.ConstantTimeCompare([]byte(password), []byte(a.cfg.AdminPassword)) == 1
	}
	return userOK && passOK
}

func (a *Auth) Logout(ctx context.Context, token string) {
	if token != "" {
		a.rdb.Del(ctx, sessionKey(token))
	}
}

func sessionKey(token string) string {
	sum := sha256.Sum256([]byte(token))
	return "ms:sess:" + hex.EncodeToString(sum[:])
}

func (a *Auth) SetCookie(w http.ResponseWriter, token string) {
	http.SetCookie(w, &http.Cookie{
		Name:     CookieName,
		Value:    token,
		Path:     "/",
		MaxAge:   int(a.cfg.SessionTTL.Seconds()),
		HttpOnly: true,
		Secure:   a.cfg.CookieSecure,
		SameSite: http.SameSiteLaxMode,
	})
}

func (a *Auth) ClearCookie(w http.ResponseWriter) {
	http.SetCookie(w, &http.Cookie{
		Name: CookieName, Value: "", Path: "/", MaxAge: -1,
		HttpOnly: true, Secure: a.cfg.CookieSecure, SameSite: http.SameSiteLaxMode,
	})
}

type Kind int

const (
	None Kind = iota
	Cookie
	Bearer
)

func (a *Auth) Identify(r *http.Request) Kind {
	if h := r.Header.Get("Authorization"); strings.HasPrefix(h, "Bearer ") && a.cfg.AdminAPIToken != "" {
		tok := strings.TrimPrefix(h, "Bearer ")
		if subtle.ConstantTimeCompare([]byte(tok), []byte(a.cfg.AdminAPIToken)) == 1 {
			return Bearer
		}
		return None
	}
	c, err := r.Cookie(CookieName)
	if err != nil || c.Value == "" {
		return None
	}
	n, err := a.rdb.Exists(r.Context(), sessionKey(c.Value)).Result()
	if err != nil || n == 0 {
		return None
	}
	return Cookie
}

func (a *Auth) Middleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if a.Identify(r) != None {
			r = r.WithContext(WithAdmin(r.Context()))
		}
		next.ServeHTTP(w, r)
	})
}

func (a *Auth) SameOrigin(r *http.Request) bool {
	origin := r.Header.Get("Origin")
	if origin == "" {
		origin = r.Header.Get("Referer")
	}
	if origin == "" {
		return false
	}
	u, err := url.Parse(origin)
	if err != nil {
		return false
	}
	return u.Host == r.Host || u.Host == a.cfg.PublicHost()
}

func (a *Auth) RequireAdmin(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		kind := a.Identify(r)
		if kind == None {
			if strings.HasPrefix(r.URL.Path, "/api/") {
				http.Error(w, `{"error":"unauthorized"}`, http.StatusUnauthorized)
				return
			}
			http.Redirect(w, r, "/login?next="+url.QueryEscape(r.URL.RequestURI()), http.StatusSeeOther)
			return
		}
		if kind == Cookie && r.Method != http.MethodGet && r.Method != http.MethodHead && !a.SameOrigin(r) {
			http.Error(w, `{"error":"bad origin"}`, http.StatusForbidden)
			return
		}
		next.ServeHTTP(w, r.WithContext(WithAdmin(r.Context())))
	})
}

func ClientIP(r *http.Request, trustProxy bool) string {
	if trustProxy {
		if v := r.Header.Get("X-Real-IP"); v != "" {
			return v
		}
		if v := r.Header.Get("X-Forwarded-For"); v != "" {
			return strings.TrimSpace(strings.Split(v, ",")[0])
		}
	}
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	return host
}

func HashPassword(pw string) (string, error) {
	b, err := bcrypt.GenerateFromPassword([]byte(pw), 12)
	if err != nil {
		return "", fmt.Errorf("hash: %w", err)
	}
	return string(b), nil
}
