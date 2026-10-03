# study-assistant

Exam preparation backend that ranks course topics by how often they are
examined and how badly you know them.

## Why

Lecture slides are passive, homework walkthroughs get lost in chat history,
and before an exam there is no system — you go through the slides in order
instead of going by what actually matters.

This service collects lectures, homework and past exams, extracts topics
from them, and ranks those topics by a priority score.

## How it works

Three source types go in — `lecture`, `homework`, `past_exam`. Each is split
into chunks, embedded, and linked to topics. Topics are two-level: broad ones
come from the syllabus, specific ones are extracted from slides.

Priority is computed from four signals:

- how often the topic appears in past exams
- how many solution techniques from homework map to it
- how often you got stuck on it
- whether the lecturer flagged it as important

## Stack

- Python, FastAPI
- PostgreSQL with pgvector
- Docker

## Running locally

```bash
docker compose up -d
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Get-Content schema.sql | docker exec -i study-assistant-db psql -U studyuser -d studyassistant
uvicorn main:app --reload
```

Open http://127.0.0.1:8000/docs

Copy `.env.example` to `.env` and fill in your own values.

## Status

Working: database schema, subject endpoints, connection layer.
Not built yet: PDF ingestion, chunking, embeddings, topic extraction,
priority query.

## Known limitations

- Formulas, pseudocode and diagrams on slides are not extracted — text layer only
- Scanned exams are not supported
- Topic deduplication may fail on synonyms across languages

## Roadmap

- Web interface
- Screenshots in notes
- Markdown export
- Topic graph visualisation
