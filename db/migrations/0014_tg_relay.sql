CREATE TABLE tg_files (
    bot_id     BIGINT NOT NULL,
    meme_id    BIGINT NOT NULL REFERENCES memes(id) ON DELETE CASCADE,
    file_id    TEXT NOT NULL DEFAULT '',
    file_type  TEXT NOT NULL DEFAULT '',
    error      TEXT NOT NULL DEFAULT '',
    attempts   INT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (bot_id, meme_id)
);
CREATE INDEX tg_files_meme_idx ON tg_files (meme_id);
