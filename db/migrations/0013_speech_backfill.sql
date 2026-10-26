ALTER TABLE memes ADD COLUMN speech_checked BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX memes_speech_todo_idx ON memes (id DESC)
    WHERE kind = 'video' AND status = 'done' AND transcript = '' AND NOT speech_checked;
