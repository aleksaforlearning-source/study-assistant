# Engineering notes

Working notes for `study-assistant`: how to run it, how the data is shaped, which
decisions were made and why, and what is currently broken.

For what the project is and why it exists, see `README.md`.

---

## Running locally

```
1. Start Docker Desktop
2. docker compose up -d
3. uvicorn main:app --reload
```

API on `http://localhost:8000`, Swagger UI at `/docs`.

SQL shell:

```
docker exec -it study-assistant-db psql -U studyuser -d studyassistant
```

Apply the schema (every statement uses `IF NOT EXISTS`, safe to re-run) — **from
PowerShell, not from inside psql**:

```
Get-Content schema.sql | docker exec -i study-assistant-db psql -U studyuser -d studyassistant
```

### Scripts

```
py topic_extractor.py <source_id>     extract topics from one uploaded file
py build_hierarchy.py <subject_id>    organize a subject's topics into a tree
py embedder.py <source_id>            compute embeddings for one file's chunks
py list_models.py                     list the models the provider currently serves
```

### Endpoints

```
POST   /subjects                       create a subject
GET    /subjects                       list subjects
POST   /subjects/{id}/sources          upload a PDF
POST   /sources/{id}/topics            extract topics (background, 2-4 min)
POST   /sources/{id}/embeddings        compute embeddings (background)
POST   /subjects/{id}/hierarchy        rebuild the topic tree (~5 s)
GET    /subjects/{id}/priority         the tree with branch weights — the main screen
GET    /subjects/{id}/search?q=...     semantic search over the subject's slides
POST   /subjects/{id}/ask              grounded answer with page citations
GET    /topics/{id}                    one topic: its slides and notes
POST   /topics/{id}/notes              add an explanation of your own
PATCH  /topics/{id}                    rename or re-parent a topic
```

### Startup failures

| Symptom | Cause | Fix |
|---|---|---|
| `failed to connect to the docker API` | Docker Desktop not running | Start it, wait for the engine |
| `psycopg.errors.ConnectionTimeout` | Container not up | `docker compose up -d` |
| `column "..." does not exist` | `schema.sql` newer than the live DB | Re-apply the schema |
| `model '...' does not exist` | Hardcoded model name is stale | `py list_models.py` |
| `json_validate_failed` | Provider rejected the model's output | Retry is built in; if it repeats, look at the prompt |
| psql prompt shows `-#` not `=#` | Unterminated statement | Ctrl+C |
| `syntax error at or near "Get"` | PowerShell command typed into psql | `\q`, then run it in PowerShell |

---

## Data model

Nine tables. Each has its own independent `SERIAL` counter, so `sources.id = 1`,
`chunks.id = 1` and `topics.id = 1` are unrelated rows. Sequences never roll back,
so an id is identity, not position or count.

### `subjects`

One row per course.

### `sources`

One row per uploaded file. `type` is one of `lecture`, `homework`, `past_exam`,
`concept`, `other`.

`content_hash` is a SHA-256 of the raw bytes, with a unique index on
`(subject_id, content_hash)`. The pair, not the hash alone: the same file must be
loadable into two subjects — that is what made the prompt A/B test possible — but
not twice into one.

### `chunks`

One row per PDF page.

```
id  | source_id | page_number | text | is_weak | drawings_count | embedding
104 |         3 |           1 | ...  | false   | 12             | vector(384)
```

`is_weak = true` marks a page with little text and heavy vector graphics. Those
pages are withheld from topic extraction but **are** embedded — a weak page still
carries a caption, and a bad match simply ranks low.

### `topics`

`parent_id` references `topics(id)` — a self-referencing foreign key, so the column
supports arbitrary depth. Current content is two levels because the prompt asks for
two, not because the schema limits it.

`parent_origin` is `ai` or `manual`. Both the hierarchy script and the PATCH
endpoint only ever touch `ai` rows, so a human decision is never overwritten by a
later model run.

### `topic_chunks`

One row per "this topic appears on this page". Composite primary key
`(topic_id, chunk_id)` — no single column is unique, only the pair.

### `notes`

The student's own explanations, attached to a topic. `kind` defaults to
`own_explanation`.

### Not yet used

`techniques`, `struggles`, `exam_questions`.

---

## Pipeline

### Ingestion — `pdf_parser.parse_pdf(file_bytes)`

1. `pymupdf4llm.to_markdown(page_chunks=True, use_ocr=False)` — one markdown
   fragment per page, headings preserved
2. **Boilerplate removal** — a line appearing on 60%+ of pages is a header/footer
3. **Near-duplicate removal** — `difflib` ratio > 0.9 against the previous page
   drops build-up animation frames
4. **Weak-page flag** — `len(text) < 300 AND (drawings >= 30 OR picture marker)`

Measured on `AD1-05 HeapsPQ-AI.pdf`: ~59 pages in, 47 chunks out, 15 weak,
32 workable. Mean 467 characters, min 215, max 938.

### Topic extraction — `topic_extractor.extract_topics(source_id)`

Batches of 10 chunks, one LLM call each, 45 s between batches to stay under Groq's
8000 TPM. Measured: 1841 prompt + 943 completion tokens per batch.

`cache` maps a normalized title to `(topics.id, original title)` so a topic seen in
batch 1 is reused in batch 4 without a round trip, and the prompt is shown real
titles rather than internal keys.

`normalize()` lowercases and strips a trailing `s` (guarded against `ss`, `us`,
`is`) so `Priority Queues` and `Priority Queue` resolve to one row. The key is
internal and never displayed. `title_case()` fixes display casing on insert, so it
does not depend on what the model emitted.

The model sees fragment numbers `[1]`, `[2]`, `[3]`, never real `chunk_id`s, so a
hallucinated index cannot become a dangling foreign key.

### Hierarchy — `build_hierarchy.build_hierarchy(subject_id)`

One call, all of a subject's topic names in, parent/child pairs out. Chunks are not
touched, so the pass is cheap to repeat: ~400 prompt + ~800 completion tokens for
15 topics.

The model returns only pairs it is confident about; anything unmentioned stays a
root. Five checks run before any write: both names exist, child is not its own
parent, the child has no parent already, the proposed parent is not itself a child,
the proposed child is not already someone's parent. The last two make a cycle
impossible and enforce two levels.

### Retrieval — `embedder.py` + `find_chunks()`

`BAAI/bge-small-en-v1.5` via `fastembed`, 384 dimensions, **run locally on the CPU**.
First run downloads ~67 MB; after that a batch of 47 chunks takes seconds.

Vectors are stored as `vector(384)` and searched with pgvector's `<=>` (cosine
distance) against an HNSW index. Distance runs 0 (same meaning) to 1 (unrelated);
in practice a good hit sits around 0.17-0.30.

### Generation — `POST /subjects/{id}/ask`

Top-k chunks are placed in the prompt with their page numbers, and the model answers
with `[page N]` citations. Two modes:

- `explain: false` — only what the fragments say
- `explain: true` — the same, plus a separately labelled block with the model's own
  analogy

---

## Design decisions

**Embeddings local, generation remote.** Two failed attempts at finding a free
embedding API (Mistral wanted an upgrade, Cerebras wanted a card) ended the search:
embedding is a 33M-parameter model and runs fine on a laptop CPU, so it has no
business being a network dependency with a quota. Generation needs a 20B model and
stays on Groq.

**Provenance is separated in the answer, not merged.** The two-part answer exists
because the first test produced evidence for it: the model's analogy described a
max-heap ("each card is heavier than the ones below it") while the lecture builds a
min-heap. The analogy was useful and the direction was wrong. Labelled blocks make
that visible in a second; one merged paragraph would have hidden it. Same principle
as `parent_origin` in the schema — where information came from is stored next to it.

**Generation before hybrid search.** A ranking defect was measured first: for "time
complexity of building a heap", the only page that answers it ranked **5th of 5**
(0.1899) behind four near-identical walkthrough pages (0.1736-0.1838). That ordering
hurts a human scanning a list, but the generation step reads all five and cited the
right page twice. Fixing retrieval ranking was therefore deprioritized below
shipping generation — and below building a golden dataset, since tuning retrieval
on three hand-run queries is guessing.

**Broad tags became useful once the tree existed.** In the flat model, `Heap` being
attached to 15 of 32 pages looked like a defect: it outranked everything and buried
the actionable topics. Under a hierarchy the same links are the branch's weight, and
the specifics live underneath. The hierarchy was therefore built *before*
suppressing broad tags — suppression would have destroyed the signal.

**Hierarchy derived from meaning, not page overlap.** Parent/child could have been
inferred mechanically — `Upheap`'s pages are a subset of `Heap`'s. That proxy was
rejected: co-occurrence on a slide is not containment of concepts, and it breaks as
soon as two unrelated topics share a page.

**Structure enforced in code, meaning asked of the model.** Two-level depth, name
validity, cycle prevention and display casing are Python. The model is only asked
what code cannot determine. Asking a 20B model to also maintain invariants produced
invalid output; moving them into code fixed it permanently.

**Prompt changes are validated on more than one document type.** A prompt tuned to
pull many concepts out of a dense Core Concept sheet (1 topic → 14) regressed badly
on slides: compound names like `Selection Sort Using a Priority Queue` returned, and
`Sequence` and `Key` attached themselves to nearly every page. It was reverted. Two
documents of different shapes now act as a minimal test set.

**Generic boilerplate detection over pattern matching.** The first footer-stripping
pass matched the professor's name with a regex and worked on exactly one lecture.
Replaced with frequency counting.

**Counts are derived, never stored.** Pages-per-topic comes from `count()` over
`topic_chunks`. A stored counter drifts the first time one path forgets to update it.

**Report measured state, not inferred state.** `build_hierarchy` once returned
`new_links: 8` alongside `children: 0`, because the counts were computed from local
variables while the write loop had gone missing. Counts now come from a `SELECT`
after the commit, and that contradiction is what exposed the bug.

**Print inside the loop that does the work.** The missing write loop was found
because eight links were counted and zero arrows were printed. A log line placed
before or after the loop would not have caught it.

---

## Known issues

### Retrieval ranks near-identical chunks arbitrarily

Measured: five chunks of the same Bottom-Up Heap Construction walkthrough scored
0.1736-0.1899 — a spread of two hundredths. They share a heading, the same list of
15 example numbers and the same footer; the one line that distinguishes page 55
(`runtime of O(n)`) is a small fraction of the text and barely moves the vector.

Three fixes, cheapest first: hybrid search (Postgres has `tsvector` and `ts_rank`
built in, no new dependency); reranking with a cross-encoder; merging a walkthrough
into one chunk. None is worth doing before there is a golden dataset to measure
against.

### Footers containing a page number survive boilerplate removal

`**Algorithms and Data Structures 1, 340200 //  42**` differs on every page because
of the number, so the frequency counter never sees a repeat. Stripping digits before
counting would fix it. Not done: the fix requires re-ingesting every source, and the
trailing noise does not measurably affect topic extraction.

### An LLM analogy can contradict the course's conventions

Observed: a min-heap described as a max-heap in the `explain: true` block. The
two-part answer makes it visible rather than preventing it. The prompt forbids
contradicting the fragments and forbids introducing notation the course does not
use; neither is a guarantee.

### A parent holds more direct links than all its children

`Heap` had 15 direct chunks against 11 across eight children, because the model tags
the broad topic alongside the specific one. Opening `Heap` in a UI would show half
the lecture. Revisit once there is an interface to judge it against.

### Two levels may be too few

`Selection Sort` and `Insertion Sort` belong under `Priority Queue Sort`, which is
itself a child. The schema supports deeper nesting; whether a three-level tree is
navigable is a UI question.

### `json_validate_failed` is intermittent and not fully diagnosed

Groq returns a 400 with an empty `failed_generation`. Correlates with longer, denser
batches. Smaller batches did **not** reduce it, so the original "the request is too
hard" theory is unconfirmed. Handled by retrying twice and then falling back to a
call without `response_format`, extracting the JSON from the text.

A plausible contributor — a JSON array inside the system prompt confusing the
constrained decoder — is also unconfirmed: the pre-existing prompt contained one and
worked. System prompts are now written without bracket syntax as a precaution, not
as a fix.

### Background task failures are invisible to the client

`POST /sources/{id}/topics` returns 202 immediately; if the task then raises, the
traceback appears only in the uvicorn log. A real fix needs a job table or a task
queue.

### `ask` has no retry

A Groq hiccup returns a 500 and the request has to be repeated by hand.

### Hint list is selected alphabetically

`sorted(...)[:60]` always shows topics starting with `A`, never `Z`. Harmless at 20
topics. Replace with selection by linked-chunk count when it starts mattering.

---

## Current data

Two subjects hold the same algorithms lecture — subject 1 and subject 4 — because
the prompt comparison needed a baseline that was not destroyed. Subject 4 is the
better result and should become the keeper.

| | subject 1 | subject 4 |
|---|---|---|
| topics | 20 | 15 |
| roots / children | 5 / 15 | 7 / 8 |
| compound names | none | none |
| casing | mixed, fixed by hand | consistent |

Subject 2 holds `Core Concept_0.pdf` from Mathematics for AI III — 3 pages, no weak
chunks, formulas survive as garbled but readable text.

---

## Verification queries

The tree, roots first:

```sql
SELECT p.title AS parent, t.title AS child, count(tc.chunk_id) AS chunks FROM topics t LEFT JOIN topics p ON p.id = t.parent_id LEFT JOIN topic_chunks tc ON tc.topic_id = t.id WHERE t.subject_id = 4 GROUP BY p.title, t.title ORDER BY p.title NULLS FIRST, chunks DESC;
```

Branch weights:

```sql
SELECT COALESCE(p.title, t.title) AS topic, count(tc.chunk_id) AS chunks FROM topics t LEFT JOIN topics p ON p.id = t.parent_id LEFT JOIN topic_chunks tc ON tc.topic_id = t.id WHERE t.subject_id = 4 GROUP BY COALESCE(p.title, t.title) ORDER BY chunks DESC;
```

Pages that received no topic — the main quality signal:

```sql
SELECT c.page_number, LEFT(c.text, 60) FROM chunks c LEFT JOIN topic_chunks tc ON tc.chunk_id = c.id WHERE c.source_id = 5 AND tc.chunk_id IS NULL ORDER BY c.page_number;
```

Embedding coverage:

```sql
SELECT source_id, count(*) FILTER (WHERE embedding IS NOT NULL) AS embedded, count(*) AS total FROM chunks GROUP BY source_id ORDER BY source_id;
```

Reset one subject's topics (links first, or the foreign key refuses):

```sql
DELETE FROM topic_chunks WHERE topic_id IN (SELECT id FROM topics WHERE subject_id = 4);
```

```sql
DELETE FROM topics WHERE subject_id = 4;
```

Reset only the tree, keeping topics and links:

```sql
UPDATE topics SET parent_id = NULL WHERE subject_id = 4 AND parent_origin = 'ai';
```

---

## Next

- [ ] **Golden dataset** — 20-30 questions with the pages that answer them, so
      retrieval quality becomes a number. Everything below depends on this
- [ ] **Hybrid search** — combine `<=>` with Postgres full-text ranking. Measure
      against the golden dataset, do not ship on intuition
- [ ] Ingest past exam questions and homework concept sheets — the second priority
      signal, and the feature the product exists for
- [ ] The priority query proper: branch weight, struggle events and exam-question
      hits combined
- [ ] A minimal interface. Swagger is not something you revise from
- [ ] Job status in the database, so background failures are visible
- [ ] Merge slides sharing one `##` heading into a single chunk; needs
      `page_start`/`page_end` so the original slide stays reachable
- [ ] Vision pass over `is_weak` chunks
- [ ] `is_weak` threshold is miscalibrated: pages 24/28/29 (178-276 chars) are
  excluded from topic extraction, yet they outrank the included pages
  (317-380 chars) in semantic search — 0.379-0.384 vs 0.413-0.438 on
  "how do I add an element". 15 of 47 chunks are retrievable but have no
  topic, so filtering `/ask` by `topic_id` silently drops them. Narrowing
  stays opt-in until the threshold is re-tuned against a golden dataset.