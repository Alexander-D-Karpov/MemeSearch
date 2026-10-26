ALTER TABLE memes ADD COLUMN speech_checked BOOLEAN NOT NULL DEFAULT false;
UPDATE memes SET speech_checked = true WHERE transcript <> '' OR kind <> 'video';
CREATE INDEX memes_speech_todo_idx ON memes (id DESC) WHERE kind = 'video' AND status = 'done' AND NOT speech_checked;
