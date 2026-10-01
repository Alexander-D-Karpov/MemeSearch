CREATE TABLE tag_stats (
    tag TEXT PRIMARY KEY,
    n   INT  NOT NULL
);
INSERT INTO tag_stats (tag, n)
SELECT t, count(*) FROM memes, unnest(tags) t WHERE status = 'done' AND NOT hidden GROUP BY t;

CREATE INDEX memes_template_lower_idx ON memes (lower(template)) WHERE template <> '';
CREATE INDEX memes_people_idx ON memes USING gin (people);
