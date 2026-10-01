ALTER TABLE channels ADD COLUMN oldest_post_id BIGINT NOT NULL DEFAULT 0;
ALTER TABLE channels ADD COLUMN history_done BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE channels ADD COLUMN history_posts INT NOT NULL DEFAULT 0;
UPDATE channels SET
    oldest_post_id = COALESCE((SELECT min(ms.post_id) FROM meme_sources ms WHERE ms.channel_id = channels.id), 0),
    history_posts = added + duplicates + skipped + filtered + failed,
    backfill_limit = 0;
ALTER TABLE channels ALTER COLUMN backfill_limit SET DEFAULT 0;
