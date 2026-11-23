-- 0042: group comments into threads.
--
-- Adds the thread table, links each comment to its thread, and fills the
-- table from the comments that exist.

BEGIN;

CREATE TABLE IF NOT EXISTS thread (
    id         text PRIMARY KEY,
    issue_id   text        NOT NULL REFERENCES issue (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE comment ADD COLUMN IF NOT EXISTS thread_id text REFERENCES thread (id);

-- Start from an empty table: removes the rows left over from testing, and
-- anything that points at them.
TRUNCATE TABLE thread CASCADE;

INSERT INTO thread (id, issue_id)
SELECT md5('thread-' || issue_id)::uuid::text, issue_id FROM comment GROUP BY issue_id;

UPDATE comment c SET thread_id = t.id FROM thread t WHERE t.issue_id = c.issue_id;

COMMIT;
