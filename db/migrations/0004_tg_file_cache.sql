ALTER TABLE memes ADD COLUMN tg_file_id TEXT NOT NULL DEFAULT '';
ALTER TABLE memes ADD COLUMN tg_file_type TEXT NOT NULL DEFAULT '';
ALTER TABLE memes ADD COLUMN tg_cache_error TEXT NOT NULL DEFAULT '';
CREATE INDEX memes_tg_uncached_idx ON memes (id DESC) WHERE tg_file_id = '' AND tg_cache_error = '' AND status = 'done' AND NOT hidden;
