ALTER TABLE memes ADD COLUMN phash BIT(256);

CREATE TABLE channels (
    id             BIGSERIAL PRIMARY KEY,
    username       TEXT        NOT NULL UNIQUE,
    title          TEXT        NOT NULL DEFAULT '',
    enabled        BOOLEAN     NOT NULL DEFAULT true,
    backfill_limit INT         NOT NULL DEFAULT 1000,
    last_post_id   BIGINT      NOT NULL DEFAULT 0,
    status         TEXT        NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'running', 'idle', 'failed')),
    error          TEXT        NOT NULL DEFAULT '',
    added          INT         NOT NULL DEFAULT 0,
    duplicates     INT         NOT NULL DEFAULT 0,
    skipped        INT         NOT NULL DEFAULT 0,
    failed         INT         NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_polled_at TIMESTAMPTZ,
    next_poll_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE meme_sources (
    id         BIGSERIAL PRIMARY KEY,
    meme_id    BIGINT      NOT NULL REFERENCES memes(id) ON DELETE CASCADE,
    url        TEXT        NOT NULL UNIQUE,
    source     TEXT        NOT NULL,
    channel_id BIGINT      REFERENCES channels(id) ON DELETE SET NULL,
    post_id    BIGINT,
    caption    TEXT        NOT NULL DEFAULT '',
    posted_at  TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX meme_sources_meme_idx ON meme_sources (meme_id);
