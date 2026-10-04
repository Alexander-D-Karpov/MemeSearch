ALTER TABLE memes ADD COLUMN sounds TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE memes ADD COLUMN song TEXT NOT NULL DEFAULT '';

CREATE OR REPLACE FUNCTION memes_refresh_search() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    head TEXT;
BEGIN
    head := concat_ws(' ',
        NEW.title,
        NEW.template,
        NEW.song,
        array_to_string(NEW.tags, ' '),
        array_to_string(NEW.people, ' '));
    NEW.search_text := concat_ws(E'\n',
        NEW.title,
        NEW.ocr_text,
        NEW.description,
        NEW.transcript,
        NEW.song,
        array_to_string(NEW.sounds, ' '),
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

DROP TRIGGER memes_search_update ON memes;
CREATE TRIGGER memes_search_update
    BEFORE UPDATE OF title, description, ocr_text, transcript, template, tags, objects, people, caption, sounds, song
    ON memes
    FOR EACH ROW EXECUTE FUNCTION memes_refresh_search();
