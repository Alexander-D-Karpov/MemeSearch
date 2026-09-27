ALTER TABLE memes ADD COLUMN analyzed_at TIMESTAMPTZ;
UPDATE memes SET analyzed_at = processed_at WHERE provider <> '';
