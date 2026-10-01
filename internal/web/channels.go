package web

import (
	"errors"
	"net/http"
	"regexp"
	"strings"

	"github.com/Alexander-D-Karpov/MemeSearch/internal/queue"
	"github.com/Alexander-D-Karpov/MemeSearch/internal/store"
)

var channelNameRe = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9_]{3,31}$`)

var reservedPaths = map[string]bool{"joinchat": true, "addstickers": true, "addemoji": true, "share": true, "proxy": true, "socks": true, "login": true}

func channelUsername(in string) (string, error) {
	s := strings.TrimSpace(in)
	s = strings.TrimPrefix(s, "@")
	for _, p := range []string{"https://", "http://"} {
		s = strings.TrimPrefix(s, p)
	}
	for _, p := range []string{"www.t.me/", "t.me/", "telegram.me/", "telegram.dog/"} {
		if strings.HasPrefix(strings.ToLower(s), p) {
			s = s[len(p):]
			break
		}
	}
	s = strings.TrimPrefix(s, "s/")
	if i := strings.IndexAny(s, "/?#"); i >= 0 {
		s = s[:i]
	}
	if !channelNameRe.MatchString(s) || reservedPaths[strings.ToLower(s)] {
		return "", errors.New("expected a public channel like @name or https://t.me/name")
	}
	return strings.ToLower(s), nil
}

func (s *Server) pageChannels(w http.ResponseWriter, r *http.Request) {
	channels, err := s.store.Channels(r.Context())
	if err != nil {
		storeErr(w, err)
		return
	}
	s.render(w, r, http.StatusOK, "admin/channels", Page{Title: "Telegram channels", Data: channels})
}

func (s *Server) apiChannels(w http.ResponseWriter, r *http.Request) {
	channels, err := s.store.Channels(r.Context())
	if err != nil {
		storeErr(w, err)
		return
	}
	if channels == nil {
		channels = []*store.Channel{}
	}
	writeJSON(w, http.StatusOK, map[string]any{"channels": channels})
}

func (s *Server) apiChannelCreate(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Channel       string `json:"channel"`
		BackfillLimit *int   `json:"backfill_limit"`
	}
	if err := decodeJSON(r, &req); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	name, err := channelUsername(req.Channel)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	backfill := 0
	if req.BackfillLimit != nil {
		backfill = max(0, *req.BackfillLimit)
	}
	c, err := s.store.CreateChannel(r.Context(), name, backfill)
	if errors.Is(err, store.ErrExists) {
		writeErr(w, http.StatusConflict, errors.New("channel is already added"))
		return
	}
	if err != nil {
		storeErr(w, err)
		return
	}
	s.queue.Push(r.Context(), queue.StreamHigh, queue.Job{Type: "channel", ID: c.ID})
	writeJSON(w, http.StatusCreated, c)
}

func (s *Server) apiChannelPatch(w http.ResponseWriter, r *http.Request) {
	id, err := pathID(r)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	var p store.ChannelPatch
	if err := decodeJSON(r, &p); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	if p.BackfillLimit != nil && *p.BackfillLimit < 0 {
		writeErr(w, http.StatusBadRequest, errors.New("backfill_limit must be >= 0"))
		return
	}
	c, err := s.store.UpdateChannel(r.Context(), id, p)
	if err != nil {
		storeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, c)
}

func (s *Server) apiChannelPoll(w http.ResponseWriter, r *http.Request) {
	id, err := pathID(r)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	if err := s.store.ScheduleChannel(r.Context(), id); err != nil {
		storeErr(w, err)
		return
	}
	if err := s.queue.Push(r.Context(), queue.StreamHigh, queue.Job{Type: "channel", ID: id}); err != nil {
		storeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"queued": true})
}

func (s *Server) apiChannelDelete(w http.ResponseWriter, r *http.Request) {
	id, err := pathID(r)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	if err := s.store.DeleteChannel(r.Context(), id); err != nil {
		storeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}
