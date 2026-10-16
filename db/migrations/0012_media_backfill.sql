ALTER TABLE memes
    ADD COLUMN media_ok        BOOLEAN     NOT NULL DEFAULT false,
    ADD COLUMN media_attempts  SMALLINT    NOT NULL DEFAULT 0,
    ADD COLUMN media_error     TEXT        NOT NULL DEFAULT '',
    ADD COLUMN media_failed_at TIMESTAMPTZ;
CREATE INDEX memes_media_todo_idx ON memes (id DESC) WHERE NOT media_ok AND media_error = '' AND thumb_path <> '';
CREATE INDEX memes_media_failed_idx ON memes (media_failed_at) WHERE media_error <> '';
