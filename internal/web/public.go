package web

import (
	"errors"
	"log/slog"
	"net/http"
	"net/url"
	"strconv"
	"strings"

	"github.com/Alexander-D-Karpov/MemeSearch/internal/auth"
	"github.com/Alexander-D-Karpov/MemeSearch/internal/store"
)

type Results struct {
	Memes      []*store.Meme
	Query      string
	Kind       string
	Total      int
	NextOffset int
	NextBefore int64
	TookMs     int64
	Semantic   bool
}

func (s *Server) results(r *http.Request) (*Results, error) {
	q := strings.TrimSpace(r.URL.Query().Get("q"))
	kind := validKind(r.URL.Query().Get("kind"))
	limit := s.cfg.PageSize
	res := &Results{Query: q, Kind: kind}
	if q == "" {
		before, _ := strconv.ParseInt(r.URL.Query().Get("before"), 10, 64)
		memes, err := s.store.ListMemes(r.Context(), store.ListFilter{Public: true, Kind: kind, BeforeID: before, Limit: limit})
		if err != nil {
			return nil, err
		}
		s.decorate(memes...)
		res.Memes = memes
		if len(memes) == limit {
			res.NextBefore = memes[len(memes)-1].ID
		}
		return res, nil
	}
	offset := intParam(r, "offset", 0, 0, 10000)
	sr, err := s.search.Search(r.Context(), q, kind, limit, offset)
	if err != nil {
		return nil, err
	}
	s.decorate(sr.Memes...)
	res.Memes = sr.Memes
	res.Total = sr.Total
	res.TookMs = sr.TookMs
	res.Semantic = sr.Semantic
	if offset+limit < sr.Total {
		res.NextOffset = offset + limit
	}
	return res, nil
}

func (s *Server) pageIndex(w http.ResponseWriter, r *http.Request) {
	res, err := s.results(r)
	if err != nil {
		slog.Error("search", "err", err)
		http.Error(w, "search failed", http.StatusInternalServerError)
		return
	}
	title := "Meme Search"
	if res.Query != "" {
		title = res.Query + " — Meme Search"
	}
	s.render(w, r, http.StatusOK, "index", Page{
		Title:       title,
		Description: "Search memes by text, meaning, objects and context.",
		Query:       res.Query,
		Kind:        res.Kind,
		Data:        res,
	})
}

func (s *Server) partialResults(w http.ResponseWriter, r *http.Request) {
	res, err := s.results(r)
	if err != nil {
		slog.Error("search", "err", err)
		http.Error(w, "search failed", http.StatusInternalServerError)
		return
	}
	w.Header().Set("X-Total", strconv.Itoa(res.Total))
	w.Header().Set("X-Took-Ms", strconv.FormatInt(res.TookMs, 10))
	s.renderPartial(w, "results", res)
}

type MemePage struct {
	Meme    *store.Meme
	Similar []*store.Meme
}

func (s *Server) visibleMeme(r *http.Request) (*store.Meme, error) {
	id, err := pathID(r)
	if err != nil {
		return nil, store.ErrNotFound
	}
	m, err := s.store.Meme(r.Context(), id)
	if err != nil {
		return nil, err
	}
	if !auth.IsAdmin(r.Context()) && (m.Status != "done" || m.Hidden) {
		return nil, store.ErrNotFound
	}
	s.decorate(m)
	return m, nil
}

func (s *Server) pageMeme(w http.ResponseWriter, r *http.Request) {
	m, err := s.visibleMeme(r)
	if errors.Is(err, store.ErrNotFound) {
		s.notFound(w, r)
		return
	}
	if err != nil {
		slog.Error("meme", "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	similar, err := s.search.Similar(r.Context(), m.ID, 24)
	if err != nil {
		slog.Warn("similar", "err", err)
	}
	s.decorate(similar...)
	title := m.Title
	if title == "" {
		title = "Meme #" + strconv.FormatInt(m.ID, 10)
	}
	desc := m.Description
	if len([]rune(desc)) > 200 {
		desc = string([]rune(desc)[:200]) + "…"
	}
	og := m.ThumbURL
	if og != "" && strings.HasPrefix(og, "/") {
		og = s.cfg.PublicURL + og
	}
	s.render(w, r, http.StatusOK, "meme", Page{
		Title:       title,
		Description: desc,
		OGImage:     og,
		Data:        MemePage{Meme: m, Similar: similar},
	})
}

func (s *Server) pageLogin(w http.ResponseWriter, r *http.Request) {
	if auth.IsAdmin(r.Context()) {
		http.Redirect(w, r, safeNext(r.URL.Query().Get("next")), http.StatusSeeOther)
		return
	}
	s.render(w, r, http.StatusOK, "login", Page{Title: "Login", Data: map[string]string{"Next": r.URL.Query().Get("next")}})
}

func (s *Server) doLogin(w http.ResponseWriter, r *http.Request) {
	if !s.auth.SameOrigin(r) {
		http.Error(w, "bad origin", http.StatusForbidden)
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, 64<<10)
	if err := r.ParseForm(); err != nil {
		http.Error(w, "bad form", http.StatusBadRequest)
		return
	}
	next := r.PostFormValue("next")
	token, err := s.auth.Login(r.Context(), auth.ClientIP(r, s.cfg.TrustProxy), r.PostFormValue("username"), r.PostFormValue("password"))
	if err != nil {
		s.render(w, r, http.StatusUnauthorized, "login", Page{Title: "Login", Data: map[string]string{"Next": next, "Error": err.Error()}})
		return
	}
	s.auth.SetCookie(w, token)
	http.Redirect(w, r, safeNext(next), http.StatusSeeOther)
}

func (s *Server) doLogout(w http.ResponseWriter, r *http.Request) {
	if !s.auth.SameOrigin(r) {
		http.Error(w, "bad origin", http.StatusForbidden)
		return
	}
	if c, err := r.Cookie(auth.CookieName); err == nil {
		s.auth.Logout(r.Context(), c.Value)
	}
	s.auth.ClearCookie(w)
	http.Redirect(w, r, "/", http.StatusSeeOther)
}

func safeNext(next string) string {
	u, err := url.Parse(next)
	if next == "" || err != nil || u.IsAbs() || u.Host != "" || !strings.HasPrefix(next, "/") || strings.HasPrefix(next, "//") {
		return "/admin"
	}
	return next
}

func (s *Server) apiSearch(w http.ResponseWriter, r *http.Request) {
	q := strings.TrimSpace(r.URL.Query().Get("q"))
	kind := validKind(r.URL.Query().Get("kind"))
	limit := intParam(r, "limit", 24, 1, 100)
	offset := intParam(r, "offset", 0, 0, 10000)
	if q == "" {
		memes, err := s.store.ListMemes(r.Context(), store.ListFilter{Public: true, Kind: kind, Limit: limit, Offset: offset})
		if err != nil {
			storeErr(w, err)
			return
		}
		s.decorate(memes...)
		writeJSON(w, http.StatusOK, map[string]any{"memes": nonNil(memes), "total": len(memes)})
		return
	}
	res, err := s.search.Search(r.Context(), q, kind, limit, offset)
	if err != nil {
		storeErr(w, err)
		return
	}
	s.decorate(res.Memes...)
	res.Memes = nonNil(res.Memes)
	writeJSON(w, http.StatusOK, res)
}

func (s *Server) apiList(w http.ResponseWriter, r *http.Request) {
	before, _ := strconv.ParseInt(r.URL.Query().Get("before"), 10, 64)
	f := store.ListFilter{
		Public:   true,
		Kind:     validKind(r.URL.Query().Get("kind")),
		BeforeID: before,
		Limit:    intParam(r, "limit", 48, 1, 200),
	}
	if auth.IsAdmin(r.Context()) {
		f.Public = false
		f.Status = r.URL.Query().Get("status")
	}
	memes, err := s.store.ListMemes(r.Context(), f)
	if err != nil {
		storeErr(w, err)
		return
	}
	s.decorate(memes...)
	writeJSON(w, http.StatusOK, map[string]any{"memes": nonNil(memes)})
}

func (s *Server) apiMeme(w http.ResponseWriter, r *http.Request) {
	m, err := s.visibleMeme(r)
	if err != nil {
		storeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, m)
}

func (s *Server) apiSimilar(w http.ResponseWriter, r *http.Request) {
	m, err := s.visibleMeme(r)
	if err != nil {
		storeErr(w, err)
		return
	}
	similar, err := s.search.Similar(r.Context(), m.ID, intParam(r, "limit", 24, 1, 100))
	if err != nil {
		storeErr(w, err)
		return
	}
	s.decorate(similar...)
	writeJSON(w, http.StatusOK, map[string]any{"memes": nonNil(similar)})
}

func nonNil(m []*store.Meme) []*store.Meme {
	if m == nil {
		return []*store.Meme{}
	}
	return m
}
