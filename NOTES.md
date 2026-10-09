# Engineering notes

Working notes for `study-assistant`: how to run it, how the data is shaped, which
decisions were made and why, and what is currently broken.

For what the project is and why it exists, see `README.md`.

---

## Running locally

Three steps, in this order:

```
1. Start Docker Desktop
2. docker compose up -d
3. uvicorn main:app --reload
```

Step 2 brings up PostgreSQL 16 with pgvector in a container (host port `5433`).
Step 3 serves the API on `http://localhost:8000`, with Swagger UI at `/docs`.

Open a SQL shell:

```
docker exec -it study-assistant-db psql -U studyuser -d studyassistant
```

Apply the schema (every statement uses `IF NOT EXISTS`, so this is safe to re-run):

```
Get-Content schema.sql | docker exec -i study-assistant-db psql -U studyuser -d studyassistant
```

Extract topics for one uploaded file, where the argument is a `sources.id`:

```
py topic_extractor.py 3
```

### Startup failures

| Symptom | Cause | Fix |
|---|---|---|
| `failed to connect to the docker API` | Docker Desktop not running | Start it, wait for the engine |
| `psycopg.errors.ConnectionTimeout` | Container not up | `docker compose up -d` |
| `column "..." does not exist` | `schema.sql` newer than the live DB | Re-apply the schema |
| `model '...' does not exist` | Hardcoded model name is stale | `py list_models.py` to query the live list |

---

## Data model

Nine tables; four are in active use. Each table has its own independent `SERIAL`
counter, so `sources.id = 1`, `chunks.id = 1` and `topics.id = 1` are unrelated rows.
Sequences never roll back, so ids are identity, not position or count.

### `subjects`

One row per course.

```
id | name                           | exam_date
 1 | Algorithms and Data Structures | ...
```

### `sources`

One row per uploaded file.

```
id | subject_id | type    | filename              | content_hash
 3 |          1 | lecture | AD1-05 HeapsPQ-AI.pdf | a3f8...
```

`content_hash` is a SHA-256 of the raw bytes, with a unique index on
`(subject_id, content_hash)`. Re-uploading the same file returns the existing
`source_id` instead of ingesting it twice.

### `chunks`

One row per PDF page. Chunk size is not fixed — it is however much text the slide
carried. Measured on `AD1-05 HeapsPQ-AI.pdf`: 47 pages, mean 467 characters,
min 215, max 938.

```
id  | source_id | page_number | text                     | is_weak | drawings_count
104 |         3 |           1 | "Priority Queues and..." | false   | 12
```

`is_weak = true` marks a page with little text but heavy vector graphics — 15 of 47
here. Those pages are stored but withheld from the text pipeline; they are waiting
on a vision pass.

### `topics`

One row per topic. `parent_id` exists for a module/sub-topic hierarchy but is
**not populated yet** — topics are inserted flat.

```
id | subject_id | parent_id | title
 1 |          1 | NULL      | Priority Queue
```

### `topic_chunks`

One row per "this topic appears on this page". Two integers, no text.

```
topic_id | chunk_id
      12 |      114
      12 |      115
      12 |      117
```

A join table is required because the relation is many-to-many in both directions:
one topic spans several pages, and one page carries several topics.

The primary key is composite — `PRIMARY KEY (topic_id, chunk_id)` — because no
single column is unique; only the pair is. Nothing is auto-generated here, which is
why there is no `SERIAL`.

### Not yet used

`techniques`, `notes`, `struggles`, `exam_questions` — created for the next stages
(solution techniques, the student's own explanations, "didn't get it" events,
questions recovered from past exams).

---

## Ingestion pipeline

`pdf_parser.parse_pdf(file_bytes) -> list[dict]`

1. `pymupdf4llm.to_markdown(page_chunks=True, use_ocr=False)` — one markdown
   fragment per page, preserving heading structure
2. **Boilerplate removal** — count every distinct line across all pages; a line
   appearing on 60%+ of pages is a header/footer and is dropped
3. **Near-duplicate removal** — `difflib.SequenceMatcher` ratio > 0.9 against the
   previous page drops build-up animation frames
4. **Weak-page flag** — `len(text) < 300 AND (drawings >= 30 OR picture marker)`

Upload then hashes the bytes, inserts one `sources` row and one `chunks` row per
surviving page.

---

## Topic extraction

`topic_extractor.extract_topics(source_id) -> dict`

```
1. Resolve subject_id            SELECT from sources
2. Load known topics             SELECT from topics WHERE subject_id = ...  -> cache
3. Load workable chunks          SELECT from chunks WHERE source_id = ... AND NOT is_weak
4. Split into batches of 10      32 chunks -> 4 batches (10 + 10 + 10 + 2)
5. Per batch:
     build one prompt from the fragments
     call Groq, parse JSON
     per topic: look up in cache, or INSERT into topics
     INSERT into topic_chunks
     commit
     sleep
6. Return counts
```

`cache` maps a normalized title to its `topics.id`, so a topic first seen in batch 1
is reused in batch 4 without a round trip to the database. `normalize()` lowercases
and strips a trailing `s` (guarded against `ss`, `us`, `is`) so that
`Priority Queues` and `Priority Queue` resolve to one row. The key is internal and
never displayed.

The model is shown fragment numbers `[1]`, `[2]`, `[3]` — never real `chunk_id`s.
The number-to-id mapping is held locally, so a hallucinated index cannot become a
dangling foreign key.

### Parameters

```python
MODEL = "openai/gpt-oss-20b"
BATCH_SIZE = 10
SLEEP_BETWEEN_BATCHES = 30
MAX_RETRIES = 3
RETRY_SLEEP = 60
CHUNK_CHAR_LIMIT = 800
MAX_EXISTING_IN_PROMPT = 60
```

Groq free tier: 30 RPM, **8000 TPM**, 1000 RPD. Measured per batch of 10 chunks:
**1841 prompt + 943 completion = 2784 tokens**. Four batches back to back would be
11136 tokens and would hit the per-minute ceiling on the third, hence the sleep.

`temperature=0` is required, not stylistic: the same slide must produce the same
topic name or deduplication is meaningless.

`response_format={"type": "json_object"}` guarantees syntax, not structure, so
`JSONDecodeError` is still caught and retried.

`CHUNK_CHAR_LIMIT` truncates only what the model sees. Full page text stays in
`chunks.text`, so any prompt change can be replayed without re-ingesting.

`MAX_EXISTING_IN_PROMPT` caps only the "topics already known" hint list. Every chunk
is always processed; hitting the cap risks a duplicate name, never a dropped topic.

---

## Design decisions

**Generic boilerplate detection over pattern matching.** The first footer-stripping
pass matched the professor's name with a regex. It worked on exactly one lecture.
Replaced with frequency counting, which carries over to any deck without
per-course configuration.

**Dedup before topic extraction, not after.** Animated slides export as several
near-identical pages. Left in, a single topic would have been linked to three
chunks instead of one and would have ranked three times heavier in the priority
query — breaking the one feature the product exists for.

**Counts are derived, never stored.** Pages-per-topic comes from `count()` over
`topic_chunks`, not a column. A stored counter has to be maintained on every insert,
delete and re-ingest, and drifts silently the first time one path forgets.

**Content hash for idempotent upload.** Uploading the same PDF twice is the obvious
user mistake; a unique index on `(subject_id, content_hash)` makes it a no-op rather
than a duplicate ingest.

**`ON CONFLICT DO NOTHING` on link insert.** A duplicate pair would otherwise abort
the whole transaction, not just the row. With it, the whole extraction run is
re-runnable.

**Commit per batch.** A run takes 2-3 minutes; a failure in batch 4 keeps the first
three.

**Model list queried, not remembered.** `list_models.py` exists because a hardcoded
model name silently went stale. If the API rejects a name, ask the API.

---

## Known issues

### Title casing flips at the batch boundary

```
12 | Selection Sort Priority Queue
13 | insertion sort priority queue
```

`existing_names = sorted({t for t in cache})` iterates the dict, which yields
**keys** — and keys are normalized, i.e. lowercased. Batch 1 ran against an empty
cache and the model used its own capitalization; batches 2-4 were shown lowercase
keys and copied that format.

Not merely cosmetic. A key such as `proces scheduling` (from `Process Scheduling`)
reads as a typo; the model may "correct" it to `Processes Scheduling`, which
normalizes to `processe scheduling`, misses the cache, and creates a second row.
A duplicated topic splits one 6-page topic into two 3-page topics, and both then
rank as less important than they are.

Fix: store `(id, title)` in the cache and pass real titles to the prompt.

```python
def load_existing_topics(cur, subject_id: int) -> dict[str, tuple[int, str]]:
    cur.execute("SELECT id, title FROM topics WHERE subject_id = %s", (subject_id,))
    return {normalize(title): (topic_id, title) for topic_id, title in cur.fetchall()}
```

```python
existing_names = sorted(title for _, title in cache.values())
```

### Topics are over-fragmented

37 topics from a single lecture; 15-25 was expected.

```
15 | heap insertion
18 | heap insertion special cases
19 | heap insertion new root
26 | heap insertion position
```

One topic split four ways. Plus word-order variants of the same concept:
`priority queue heap implementation`, `heap priority queue`,
`priority queue heap operations`, `heap priority queue operations`.

The model sees ten pages and looks for something new on each, so three consecutive
pages about heap insertion yield three names rather than one name three times.
This is a prompt problem, not a code problem.

### Hint list is selected alphabetically

`sorted(...)[:60]` always shows topics starting with `A`, never `Z`. The criterion
is arbitrary. When the cap starts mattering, select by linked-chunk count instead —
course-wide concepts are the ones worth not duplicating.

```sql
SELECT t.title FROM topics t JOIN topic_chunks tc ON tc.topic_id = t.id WHERE t.subject_id = %s GROUP BY t.id, t.title ORDER BY count(*) DESC LIMIT %s
```

### Dense pages are truncated

`CHUNK_CHAR_LIMIT = 800` cuts the richest pages (max measured 938), and a dense page
is exactly where a definition or formula lives. Raising to 1200 covers every page in
the current corpus; `SLEEP_BETWEEN_BATCHES` then needs 45s to stay under 8000 TPM.

The better long-term fix is splitting an oversized chunk into two prompt fragments
that both point at one `chunk_id`, rather than truncating.

### Pages 25 and 26 are near-duplicates that survived dedup

Ratio fell just under the 0.9 threshold.

### Chunk count moved from 44 to 47 between runs

Cause not established. Not investigated.

---

## Verification queries

Topics by weight:

```sql
SELECT t.id, t.title, count(tc.chunk_id) AS chunks FROM topics t LEFT JOIN topic_chunks tc ON tc.topic_id = t.id GROUP BY t.id, t.title ORDER BY chunks DESC;
```

Pages that received no topic — the main quality signal:

```sql
SELECT c.page_number, LEFT(c.text, 60) FROM chunks c LEFT JOIN topic_chunks tc ON tc.chunk_id = c.id WHERE c.source_id = 3 AND tc.chunk_id IS NULL ORDER BY c.page_number;
```

Chunk size distribution:

```sql
SELECT count(*), avg(length(text))::int, min(length(text)), max(length(text)) FROM chunks WHERE source_id = 3;
```

Reset one subject's topics to re-extract from scratch — links first, then topics,
or the foreign key refuses:

```sql
DELETE FROM topic_chunks WHERE topic_id IN (SELECT id FROM topics WHERE subject_id = 1);
```

```sql
DELETE FROM topics WHERE subject_id = 1;
```

---

## Next

- [ ] Apply the casing fix, reset topics, re-extract
- [ ] Revise `SYSTEM_PROMPT` to stop over-fragmenting; target 15-25 topics per lecture
- [ ] Pick an embedding provider, then `ALTER TABLE chunks ADD COLUMN embedding vector(N)`
- [ ] API endpoint for extraction — 2-3 minutes is too long for a synchronous
      request, so this needs background execution
- [ ] The priority query: rank topics by chunk count, struggle events and exam
      question hits
- [ ] Golden dataset of ~30 questions to measure retrieval quality
- [ ] Merge slides sharing one `##` heading into a single chunk; needs
      `page_start`/`page_end` on `chunks` so the original slide stays reachable
- [ ] Vision pass over `is_weak` chunks