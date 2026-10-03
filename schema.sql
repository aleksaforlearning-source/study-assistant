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

CREATE TABLE IF NOT EXISTS exam_questions (
    id            SERIAL PRIMARY KEY,
    topic_id      INTEGER NOT NULL REFERENCES topics(id),
    source_id     INTEGER NOT NULL REFERENCES sources(id),
    question_text TEXT NOT NULL
);