CREATE TABLE dedupe_candidates (
    a          BIGINT NOT NULL REFERENCES memes(id) ON DELETE CASCADE,
    b          BIGINT NOT NULL REFERENCES memes(id) ON DELETE CASCADE,
    sim        REAL NOT NULL,
    label      TEXT NOT NULL DEFAULT '',
    verdict    TEXT NOT NULL DEFAULT '',
    reason     TEXT NOT NULL DEFAULT '',
    checked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (a, b),
    CHECK (a < b)
);
CREATE INDEX dedupe_candidates_todo_idx ON dedupe_candidates (sim DESC) WHERE verdict = '';
CREATE INDEX dedupe_candidates_b_idx ON dedupe_candidates (b);
