import json
import os
import sys
import time

from dotenv import load_dotenv
from groq import Groq, RateLimitError, BadRequestError
from db import get_connection

load_dotenv()

client = Groq(api_key=os.getenv("GROQ_API_KEY"))

MODEL = "openai/gpt-oss-20b"
BATCH_SIZE = 10
SLEEP_BETWEEN_BATCHES = 45
MAX_RETRIES = 3
RETRY_SLEEP = 60
CHUNK_CHAR_LIMIT = 1200
MAX_EXISTING_IN_PROMPT = 60

SYSTEM_PROMPT = """You extract study topics from lecture slides.

A topic is a unit of knowledge a student can say "I know this" or "I don't know this" about.

Good topics: "Quicksort", "Heap Property", "Big-O Notation", "Binary Search Tree"
Too broad: "Sorting", "Algorithms", "Chapter 3"
Too narrow: "pivot selection in line 3 of quicksort"

Rules:
- 0 to 2 topics per fragment, fewer is better
- Consecutive fragments usually elaborate ONE topic. Return the same name for all of
  them instead of inventing variations. "Heap Insertion" already covers its special
  cases, its examples and its step-by-step walkthrough - do not name those separately.
- Never merge two concepts into one name.
  Wrong: "Selection Sort Priority Queue"
  Right: ["Selection Sort", "Priority Queue"]
- Reuse an existing topic name EXACTLY, character for character, if it fits
- Title Case and singular: "Heap", not "heaps"
- 1 to 3 words per name
- Empty list for a fragment with no real content
- Return only valid JSON, no explanation
"""


def normalize(title: str) -> str:
    key = title.strip().lower()
    if key.endswith("s") and not key.endswith(("ss", "us", "is")):
        key = key[:-1]
    return key

def title_case(title: str) -> str:
    words = title.strip().split()
    return " ".join(
        word if any(c.isupper() for c in word) else word.capitalize()
        for word in words
    )


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
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_prompt(chunks, existing_topics)},
    ]

    for attempt in range(1, MAX_RETRIES + 1):
        json_mode = attempt < MAX_RETRIES

        try:
            kwargs = {"model": MODEL, "messages": messages, "temperature": 0, "max_completion_tokens": 4096}
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}

            response = client.chat.completions.create(**kwargs)
            content = response.choices[0].message.content

            if json_mode:
                return json.loads(content)
            return extract_json(content)

        except RateLimitError:
            print(f"  rate limit, попытка {attempt}/{MAX_RETRIES}, ждём {RETRY_SLEEP}s")
            time.sleep(RETRY_SLEEP)

        except BadRequestError as error:
            if "json_validate_failed" not in str(error):
                raise
            print(f"  json_validate_failed, попытка {attempt}/{MAX_RETRIES}")

        except (json.JSONDecodeError, ValueError) as error:
            print(f"  не разобрался JSON ({error}), попытка {attempt}/{MAX_RETRIES}")

    raise RuntimeError("не удалось получить ответ от модели")



def extract_json(text: str) -> dict:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("в ответе нет JSON")
    return json.loads(text[start:end + 1])

def get_subject_id(cur, source_id: int) -> int:
    cur.execute("SELECT subject_id FROM sources WHERE id = %s", (source_id,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"source {source_id} не найден")
    return row[0]


def load_existing_topics(cur, subject_id: int) -> dict[str, tuple[int, str]]:
    cur.execute("SELECT id, title FROM topics WHERE subject_id = %s", (subject_id,))
    return {normalize(title): (topic_id, title) for topic_id, title in cur.fetchall()}


def load_chunks(cur, source_id: int) -> list[dict]:
    cur.execute(
        "SELECT id, page_number, text FROM chunks "
        "WHERE source_id = %s AND NOT is_weak ORDER BY page_number",
        (source_id,),
    )
    return [{"id": r[0], "page": r[1], "text": r[2]} for r in cur.fetchall()]


def get_or_create_topic(
    cur, subject_id: int, title: str, cache: dict[str, tuple[int, str]]
) -> int:
    key = normalize(title)
    if key in cache:
        return cache[key][0]

    clean = title_case(title)
    cur.execute(
        "INSERT INTO topics (subject_id, title) VALUES (%s, %s) RETURNING id",
        (subject_id, clean),
    )
    topic_id = cur.fetchone()[0]
    cache[key] = (topic_id, clean)
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

                existing_names = sorted(title for _, title in cache.values())
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