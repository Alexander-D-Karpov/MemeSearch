CREATE TABLE meme_similar (
    meme_id     BIGINT PRIMARY KEY REFERENCES memes(id) ON DELETE CASCADE,
    ids         BIGINT[] NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX meme_similar_age_idx ON meme_similar (computed_at);
