package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/redis/go-redis/v9"

	"github.com/Alexander-D-Karpov/MemeSearch/internal/auth"
	"github.com/Alexander-D-Karpov/MemeSearch/internal/config"
	"github.com/Alexander-D-Karpov/MemeSearch/internal/ingest"
	"github.com/Alexander-D-Karpov/MemeSearch/internal/mlclient"
	"github.com/Alexander-D-Karpov/MemeSearch/internal/queue"
	"github.com/Alexander-D-Karpov/MemeSearch/internal/search"
	"github.com/Alexander-D-Karpov/MemeSearch/internal/store"
	"github.com/Alexander-D-Karpov/MemeSearch/internal/web"
)

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, nil)))

	if len(os.Args) == 3 && os.Args[1] == "hash-password" {
		h, err := auth.HashPassword(os.Args[2])
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		fmt.Println(h)
		return
	}

	if err := run(); err != nil {
		slog.Error("fatal", "err", err)
		os.Exit(1)
	}
}

func run() error {
	cfg, err := config.Load()
	if err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	var st *store.Store
	for attempt := 1; ; attempt++ {
		st, err = store.Open(ctx, cfg.DatabaseURL)
		if err == nil {
			break
		}
		if attempt >= 30 {
			return fmt.Errorf("database: %w", err)
		}
		slog.Warn("waiting for database", "err", err)
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(2 * time.Second):
		}
	}
	defer st.Close()

	ropts, err := redis.ParseURL(cfg.RedisURL)
	if err != nil {
		return fmt.Errorf("redis url: %w", err)
	}
	rdb := redis.NewClient(ropts)
	defer rdb.Close()
	if err := rdb.Ping(ctx).Err(); err != nil {
		return fmt.Errorf("redis: %w", err)
	}

	for _, dir := range []string{"originals", "thumbs", "inbox", ".tmp", ".imports"} {
		if err := os.MkdirAll(cfg.UploadDir+"/"+dir, 0o755); err != nil {
			return fmt.Errorf("upload dir: %w", err)
		}
	}

	q := queue.New(rdb)
	q.Bump(ctx)
	ml := mlclient.New(cfg.MLURL, cfg.InternalToken, rdb, cfg.EmbedCacheTTL)
	sr := search.New(st, ml, rdb, q, cfg.SearchCacheTTL, cfg.TextMaxDist, cfg.ClipMaxDist, cfg.LookAlikeMaxDist)
	a := auth.New(cfg, rdb)
	in := &ingest.Ingester{Store: st, Queue: q, UploadDir: cfg.UploadDir, MaxBytes: cfg.MaxFileBytes}

	srv, err := web.New(cfg, st, rdb, q, sr, ml, a, in)
	if err != nil {
		return err
	}
	httpSrv := &http.Server{
		Addr:              cfg.Addr,
		Handler:           srv.Handler(),
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       120 * time.Second,
	}
	errc := make(chan error, 1)
	go func() {
		slog.Info("listening", "addr", cfg.Addr)
		errc <- httpSrv.ListenAndServe()
	}()
	select {
	case err := <-errc:
		if !errors.Is(err, http.ErrServerClosed) {
			return err
		}
	case <-ctx.Done():
	}
	shutdown, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	return httpSrv.Shutdown(shutdown)
}
