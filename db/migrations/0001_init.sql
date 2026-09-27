CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE memes (
    id            BIGSERIAL PRIMARY KEY,
    sha256        TEXT        NOT NULL UNIQUE,
    kind          TEXT        NOT NULL CHECK (kind IN ('image', 'gif', 'video')),
    mime          TEXT        NOT NULL,
    ext           TEXT        NOT NULL,
    file_path     TEXT        NOT NULL,
    thumb_path    TEXT        NOT NULL DEFAULT '',
    size_bytes    BIGINT      NOT NULL,
    width         INT         NOT NULL DEFAULT 0,
    height        INT         NOT NULL DEFAULT 0,
    duration_ms   INT         NOT NULL DEFAULT 0,
    original_name TEXT        NOT NULL DEFAULT '',
    source        TEXT        NOT NULL DEFAULT 'upload',
    source_ref    TEXT        NOT NULL DEFAULT '',
    caption       TEXT        NOT NULL DEFAULT '',
    status        TEXT        NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'processing', 'done', 'failed')),
    error         TEXT        NOT NULL DEFAULT '',
    attempts      INT         NOT NULL DEFAULT 0,
    provider      TEXT        NOT NULL DEFAULT '',
    model         TEXT        NOT NULL DEFAULT '',
    title         TEXT        NOT NULL DEFAULT '',
    description   TEXT        NOT NULL DEFAULT '',
    ocr_text      TEXT        NOT NULL DEFAULT '',
    transcript    TEXT        NOT NULL DEFAULT '',
    objects       TEXT[]      NOT NULL DEFAULT '{}',
    tags          TEXT[]      NOT NULL DEFAULT '{}',
    people        TEXT[]      NOT NULL DEFAULT '{}',
    template      TEXT        NOT NULL DEFAULT '',
    lang          TEXT        NOT NULL DEFAULT '',
    mood          TEXT        NOT NULL DEFAULT '',
    nsfw          BOOLEAN     NOT NULL DEFAULT false,
    hidden        BOOLEAN     NOT NULL DEFAULT false,
    locked        BOOLEAN     NOT NULL DEFAULT false,
    search_text   TEXT        NOT NULL DEFAULT '',
    tsv           TSVECTOR,
    clip_vec      vector(768),
    text_vec      vector(384),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at  TIMESTAMPTZ
);

CREATE FUNCTION memes_refresh_search() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    head TEXT;
BEGIN
    head := concat_ws(' ',
        NEW.title,
        NEW.template,
        array_to_string(NEW.tags, ' '),
        array_to_string(NEW.people, ' '));
    NEW.search_text := concat_ws(E'\n',
        NEW.title,
        NEW.ocr_text,
        NEW.description,
        NEW.transcript,
        NEW.template,
        array_to_string(NEW.tags, ' '),
        array_to_string(NEW.objects, ' '),
        array_to_string(NEW.people, ' '),
        NEW.caption);
    NEW.tsv :=
        setweight(to_tsvector('simple', head), 'A') ||
        setweight(to_tsvector('simple', NEW.ocr_text), 'A') ||
        setweight(to_tsvector('russian', NEW.search_text), 'B') ||
        setweight(to_tsvector('english', NEW.search_text), 'C');
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER memes_search_insert
    BEFORE INSERT ON memes
    FOR EACH ROW EXECUTE FUNCTION memes_refresh_search();

CREATE TRIGGER memes_search_update
    BEFORE UPDATE OF title, description, ocr_text, transcript, template, tags, objects, people, caption
    ON memes
    FOR EACH ROW EXECUTE FUNCTION memes_refresh_search();

CREATE INDEX memes_status_idx ON memes (status, updated_at);
CREATE INDEX memes_created_idx ON memes (created_at DESC, id DESC);
CREATE INDEX memes_visible_idx ON memes (id DESC) WHERE status = 'done' AND NOT hidden;
CREATE INDEX memes_tsv_idx ON memes USING gin (tsv);
CREATE INDEX memes_trgm_idx ON memes USING gin (search_text gin_trgm_ops);
CREATE INDEX memes_tags_idx ON memes USING gin (tags);
CREATE INDEX memes_clip_idx ON memes USING hnsw (clip_vec vector_cosine_ops);
CREATE INDEX memes_text_idx ON memes USING hnsw (text_vec vector_cosine_ops);

CREATE TABLE import_jobs (
    id          BIGSERIAL PRIMARY KEY,
    kind        TEXT        NOT NULL CHECK (kind IN ('zip', 'dir')),
    name        TEXT        NOT NULL DEFAULT '',
    path        TEXT        NOT NULL,
    status      TEXT        NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'running', 'done', 'failed')),
    total       INT         NOT NULL DEFAULT 0,
    added       INT         NOT NULL DEFAULT 0,
    duplicates  INT         NOT NULL DEFAULT 0,
    skipped     INT         NOT NULL DEFAULT 0,
    failed      INT         NOT NULL DEFAULT 0,
    error       TEXT        NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);

CREATE TABLE codex_sessions (
    id             BIGSERIAL PRIMARY KEY,
    name           TEXT        NOT NULL,
    enabled        BOOLEAN     NOT NULL DEFAULT true,
    priority       INT         NOT NULL DEFAULT 0,
    status         TEXT        NOT NULL DEFAULT 'new'
                   CHECK (status IN ('new', 'login_pending', 'ok', 'limited', 'error', 'logged_out')),
    email          TEXT        NOT NULL DEFAULT '',
    plan           TEXT        NOT NULL DEFAULT '',
    last_error     TEXT        NOT NULL DEFAULT '',
    usage          JSONB       NOT NULL DEFAULT '{}',
    cooldown_until TIMESTAMPTZ,
    last_used_at   TIMESTAMPTZ,
    last_check_at  TIMESTAMPTZ,
    ok_count       BIGINT      NOT NULL DEFAULT 0,
    fail_count     BIGINT      NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE app_settings (
    key        TEXT PRIMARY KEY,
    value      JSONB       NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
