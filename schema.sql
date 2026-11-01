CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS subjects (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    exam_date   DATE
);

CREATE TABLE IF NOT EXISTS sources (
    id          SERIAL PRIMARY KEY,
    subject_id  INTEGER NOT NULL REFERENCES subjects(id),
    type        TEXT NOT NULL,
    filename    TEXT,
    uploaded_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS topics (
    id          SERIAL PRIMARY KEY,
    subject_id  INTEGER NOT NULL REFERENCES subjects(id),
    parent_id   INTEGER REFERENCES topics(id),
    title       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    id          SERIAL PRIMARY KEY,
    source_id   INTEGER NOT NULL REFERENCES sources(id),
    text        TEXT NOT NULL,
    page_number INTEGER
);

CREATE TABLE IF NOT EXISTS topic_chunks (
    topic_id    INTEGER NOT NULL REFERENCES topics(id),
    chunk_id    INTEGER NOT NULL REFERENCES chunks(id),
    PRIMARY KEY (topic_id, chunk_id)
);

CREATE TABLE IF NOT EXISTS techniques (
    id          SERIAL PRIMARY KEY,
    topic_id    INTEGER NOT NULL REFERENCES topics(id),
    source_id   INTEGER REFERENCES sources(id),
    description TEXT NOT NULL,
    origin      TEXT NOT NULL DEFAULT 'ai'
);

CREATE TABLE IF NOT EXISTS notes (
    id          SERIAL PRIMARY KEY,
    topic_id    INTEGER NOT NULL REFERENCES topics(id),
    text        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'own_explanation',
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS struggles (
    id          SERIAL PRIMARY KEY,
    topic_id    INTEGER NOT NULL REFERENCES topics(id),
    event_type  TEXT NOT NULL,
    context     TEXT,
    created_at  TIMESTAMPTZ DEFAULT now(),
    resolved_at TIMESTAMPTZ
);

-- One row per question of an exam file (sources.type past_exam = final, short_exam = test).
CREATE TABLE IF NOT EXISTS exam_questions (
    id          SERIAL PRIMARY KEY,
    source_id   INTEGER NOT NULL REFERENCES sources(id),
    number      TEXT,                       -- as printed: "1", "2b"
    text        TEXT NOT NULL,              -- Markdown with LaTeX
    page        INTEGER,
    position    INTEGER NOT NULL DEFAULT 0  -- order within the exam
);

-- A question can touch several topics. origin: ai (from analyse) or manual.
CREATE TABLE IF NOT EXISTS exam_question_topics (
    question_id INTEGER NOT NULL REFERENCES exam_questions(id),
    topic_id    INTEGER NOT NULL REFERENCES topics(id),
    origin      TEXT NOT NULL DEFAULT 'ai',
    PRIMARY KEY (question_id, topic_id)
);

ALTER TABLE sources ADD COLUMN IF NOT EXISTS content_hash TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS sources_hash_idx ON sources (subject_id, content_hash);
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS is_weak BOOLEAN DEFAULT false;
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS drawings_count INTEGER DEFAULT 0;
ALTER TABLE topics ADD COLUMN IF NOT EXISTS parent_origin TEXT NOT NULL DEFAULT 'ai';
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS embedding vector(384);
CREATE INDEX IF NOT EXISTS chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE TABLE IF NOT EXISTS blocks (id SERIAL PRIMARY KEY, subject_id INT NOT NULL REFERENCES subjects(id), name TEXT NOT NULL, position INT NOT NULL DEFAULT 0);
ALTER TABLE sources ADD COLUMN IF NOT EXISTS block_id INT REFERENCES blocks(id);
ALTER TABLE sources ADD COLUMN IF NOT EXISTS pdf BYTEA;
ALTER TABLE topics ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'new';
ALTER TABLE topics ADD COLUMN IF NOT EXISTS summary TEXT;
ALTER TABLE topics ADD COLUMN IF NOT EXISTS summary_origin TEXT NOT NULL DEFAULT 'ai';

CREATE TABLE IF NOT EXISTS jobs (id SERIAL PRIMARY KEY, source_id INT NOT NULL REFERENCES sources(id), stage TEXT NOT NULL DEFAULT 'queued', status TEXT NOT NULL DEFAULT 'running', message TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
-- Formula pages are re-read from the slide image in the background.
-- formula_status: NULL = no formulas, pending, done, failed. pdf_text keeps the original extracted text.
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS formula_status TEXT;
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS pdf_text TEXT;

-- Unclear places marked in a summary or note: context is the quoted text, comment is optional.
ALTER TABLE struggles ADD COLUMN IF NOT EXISTS comment TEXT;

-- A note can sit on a spot of the text (anchor = the quoted text, like unclear places);
-- revisit = "come back to this", a light weak-spot signal for Focus.
ALTER TABLE notes ADD COLUMN IF NOT EXISTS anchor TEXT;
ALTER TABLE notes ADD COLUMN IF NOT EXISTS revisit BOOLEAN NOT NULL DEFAULT false;

-- A summary saved empty counted as present in the block but showed "generate" in
-- the topic; empty now means none, and 'ai' lets generating fill it.
UPDATE topics SET summary = NULL, summary_origin = 'ai' WHERE btrim(summary) = '';

-- Subjects are grouped by semester.
CREATE TABLE IF NOT EXISTS semesters (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE subjects ADD COLUMN IF NOT EXISTS semester_id INTEGER REFERENCES semesters(id);

-- Subjects from before semesters existed go into one, the current Semester 3.
INSERT INTO semesters (name)
SELECT 'Semester 3'
WHERE EXISTS (SELECT 1 FROM subjects WHERE semester_id IS NULL)
  AND NOT EXISTS (SELECT 1 FROM semesters);
UPDATE subjects SET semester_id = (SELECT min(id) FROM semesters) WHERE semester_id IS NULL;

-- A code or Markdown file, shown as it is: no PDF, no pages, no topics.
ALTER TABLE sources ADD COLUMN IF NOT EXISTS text_content TEXT;
