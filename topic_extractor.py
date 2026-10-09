import json
import os
import sys
import time

from dotenv import load_dotenv
from groq import Groq, RateLimitError

from db import get_connection

load_dotenv()

client = Groq(api_key=os.getenv("GROQ_API_KEY"))

MODEL = "openai/gpt-oss-20b"
BATCH_SIZE = 10
SLEEP_BETWEEN_BATCHES = 30
MAX_RETRIES = 3
RETRY_SLEEP = 60
CHUNK_CHAR_LIMIT = 800
MAX_EXISTING_IN_PROMPT = 60

SYSTEM_PROMPT = """You extract study topics from lecture slides.

A topic is a unit of knowledge a student can say "I know this" or "I don't know this" about.

Good topics: "Quicksort", "Heap property", "Big-O notation", "Binary search tree insertion"
Too broad: "Sorting", "Algorithms", "Chapter 3"
Too narrow: "pivot selection in line 3 of quicksort"

Rules:
- 0 to 3 topics per fragment, fewer is better
- If a fragment has no real content, return an empty list
- Topic names in English, 1 to 4 words
- Reuse an existing topic name EXACTLY if it fits
- Prefer singular form: "Heap", not "Heaps"
- Return only valid JSON, no explanation
"""


def normalize(title: str) -> str:
    key = title.strip().lower()
    if key.endswith("s"):
        key = key[:-1]
    return key


def build_prompt(chunks: list[dict], existing_topics: list[str]) -> str:
    fragments = "\n\n".join(
        f"[{i}]\n{c['text'][:CHUNK_CHAR_LIMIT]}"
        for i, c in enumerate(chunks, start=1)
    )
    shown = existing_topics[:MAX_EXISTING_IN_PROMPT]
    existing = ", ".join(shown) if shown else "none yet"

    return f"""Existing topics: {existing}

Fragments:

{fragments}

Return JSON mapping fragment number to list of topics:
{{"1": ["Topic A"], "2": [], "3": ["Topic B", "Topic C"]}}"""


def call_llm(chunks: list[dict], existing_topics: list[str]) -> dict:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": build_prompt(chunks, existing_topics)},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
            return json.loads(response.choices[0].message.content)
        except RateLimitError:
            print(f"  rate limit, попытка {attempt}/{MAX_RETRIES}, ждём {RETRY_SLEEP}s")
            time.sleep(RETRY_SLEEP)
        except json.JSONDecodeError:
            print(f"  модель вернула не JSON, попытка {attempt}/{MAX_RETRIES}")

    raise RuntimeError("не удалось получить ответ от модели")


def get_subject_id(cur, source_id: int) -> int:
    cur.execute("SELECT subject_id FROM sources WHERE id = %s", (source_id,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"source {source_id} не найден")
    return row[0]


def load_existing_topics(cur, subject_id: int) -> dict[str, int]:
    cur.execute("SELECT id, title FROM topics WHERE subject_id = %s", (subject_id,))
    return {normalize(title): topic_id for topic_id, title in cur.fetchall()}


def load_chunks(cur, source_id: int) -> list[dict]:
    cur.execute(
        "SELECT id, page_number, text FROM chunks "
        "WHERE source_id = %s AND NOT is_weak ORDER BY page_number",
        (source_id,),
    )
    return [{"id": r[0], "page": r[1], "text": r[2]} for r in cur.fetchall()]


def get_or_create_topic(cur, subject_id: int, title: str, cache: dict[str, int]) -> int:
    key = normalize(title)
    if key in cache:
        return cache[key]

    cur.execute(
        "INSERT INTO topics (subject_id, title) VALUES (%s, %s) RETURNING id",
        (subject_id, title.strip()),
    )
    topic_id = cur.fetchone()[0]
    cache[key] = topic_id
    return topic_id


def link_topic_to_chunk(cur, topic_id: int, chunk_id: int) -> None:
    cur.execute(
        "INSERT INTO topic_chunks (topic_id, chunk_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (topic_id, chunk_id),
    )


def extract_topics(source_id: int) -> dict:
    with get_connection() as conn:
        with conn.cursor() as cur:
            subject_id = get_subject_id(cur, source_id)
            cache = load_existing_topics(cur, subject_id)
            chunks = load_chunks(cur, source_id)

            if not chunks:
                return {"source_id": source_id, "chunks": 0, "status": "nothing_to_do"}

            print(f"source {source_id}, subject {subject_id}: {len(chunks)} чанков")
            print(f"уже есть темы: {len(cache)}")

            batches = [
                chunks[i:i + BATCH_SIZE]
                for i in range(0, len(chunks), BATCH_SIZE)
            ]

            topics_before = len(cache)
            links = 0
            empty_chunks = 0

            for number, batch in enumerate(batches, start=1):
                print(f"\nбатч {number}/{len(batches)} (страницы {batch[0]['page']}-{batch[-1]['page']})")

                existing_names = sorted({t for t in cache})
                result = call_llm(batch, existing_names)

                for i, chunk in enumerate(batch, start=1):
                    titles = result.get(str(i), [])
                    if not titles:
                        empty_chunks += 1
                        continue

                    for title in titles:
                        if not isinstance(title, str) or not title.strip():
                            continue
                        topic_id = get_or_create_topic(cur, subject_id, title, cache)
                        link_topic_to_chunk(cur, topic_id, chunk["id"])
                        links += 1

                    print(f"  стр {chunk['page']:3d} → {titles}")

                conn.commit()

                if number < len(batches):
                    time.sleep(SLEEP_BETWEEN_BATCHES)

    return {
        "source_id": source_id,
        "chunks": len(chunks),
        "chunks_without_topics": empty_chunks,
        "topics_total": len(cache),
        "topics_new": len(cache) - topics_before,
        "links": links,
        "status": "done",
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("использование: py topic_extractor.py <source_id>")
        sys.exit(1)

    stats = extract_topics(int(sys.argv[1]))
    print()
    print(json.dumps(stats, indent=2, ensure_ascii=False))