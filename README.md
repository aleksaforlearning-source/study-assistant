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

### Formulas

PDF text extraction breaks math: `∀ε > 0` comes out as `’Á > 0`, fractions
lose their bar, `≠` loses its stroke. Pages that use math fonts (Computer
Modern, Latin Modern, AMS, newtx, Cambria Math) are rendered to an image and
transcribed by a vision model on Groq into Markdown with LaTeX. Pages without
math fonts are read from the text layer as before, so text-only lectures cost
no extra requests.

This runs in the background: an upload is usable right away with the text
layer, and a worker thread in the server re-reads the queued formula pages one
by one, newest lecture first. The queue is stored in the database, so it
continues after a restart, and the original text is kept in `chunks.pdf_text`.
Each page costs about 2,500 tokens; Groq's free tier allows 200,000 tokens a
day for the vision model, so roughly 80 formula pages a day, after which the
worker waits for the limit to refill. Lectures uploaded before this existed
can be queued with `python fix_formulas.py <source_id>`.

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

- Pseudocode and diagrams on slides are not extracted — text layer only
- Formulas inside a slide's normal text font (not a math font) are not detected
- Scanned exams are not supported
- Topic deduplication may fail on synonyms across languages

## Roadmap

- Web interface
- Screenshots in notes
- Markdown export
- Topic graph visualisation
