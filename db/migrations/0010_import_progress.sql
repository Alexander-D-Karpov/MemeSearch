ALTER TABLE import_jobs DROP CONSTRAINT import_jobs_status_check;
ALTER TABLE import_jobs ADD CONSTRAINT import_jobs_status_check
    CHECK (status IN ('uploading', 'pending', 'running', 'done', 'failed'));
ALTER TABLE import_jobs
    ADD COLUMN stage       TEXT        NOT NULL DEFAULT '',
    ADD COLUMN bytes_total BIGINT      NOT NULL DEFAULT 0,
    ADD COLUMN bytes_done  BIGINT      NOT NULL DEFAULT 0,
    ADD COLUMN updated_at  TIMESTAMPTZ NOT NULL DEFAULT now();
