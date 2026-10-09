import json
import os
import sys

from dotenv import load_dotenv
from groq import Groq, BadRequestError

from db import get_connection

load_dotenv()

client = Groq(api_key=os.getenv("GROQ_API_KEY"))

MODEL = "openai/gpt-oss-20b"
MAX_RETRIES = 3

SYSTEM_PROMPT = """You organize study topics from one course into a two-level tree.

You get a flat list of topic names. Find the pairs where one topic is a specific
part of another: an operation, property, variant or algorithm of a broader concept.

Example from a different course. Given the list
Graph, Depth-First Search, Adjacency Matrix, Sorting, Quicksort:
  Depth-First Search belongs under Graph
  Adjacency Matrix belongs under Graph
  Quicksort belongs under Sorting

Rules:
- List only the pairs you are confident about
- Copy both names EXACTLY from the list, character for character
- Never invent a name that is not in the list
- A topic can appear as a child at most once
- A topic used as a parent must not also appear as a child
- Topics you do not list stay at the top level, that is fine
- Return only valid JSON, no explanation
"""


def build_prompt(titles: list[str]) -> str:
    listed = "\n".join(titles)

    return f"""Topics:

{listed}

Return JSON with a "pairs" array:
{{"pairs": [{{"child": "Depth-First Search", "parent": "Graph"}}, {{"child": "Quicksort", "parent": "Sorting"}}]}}"""


def extract_json(text: str) -> dict:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("в ответе нет JSON")
    return json.loads(text[start:end + 1])


def ask_model(titles: list[str]):
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_prompt(titles)},
    ]

    for attempt in range(1, MAX_RETRIES + 1):
        json_mode = attempt < MAX_RETRIES

        try:
            kwargs = {"model": MODEL, "messages": messages, "temperature": 0}
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}

            response = client.chat.completions.create(**kwargs)
            content = response.choices[0].message.content

            if json_mode:
                return json.loads(content), response
            return extract_json(content), response

        except BadRequestError as error:
            if "json_validate_failed" not in str(error):
                raise
            print(f"  json_validate_failed, попытка {attempt}/{MAX_RETRIES}")

        except (json.JSONDecodeError, ValueError) as error:
            print(f"  не разобрался JSON ({error}), попытка {attempt}/{MAX_RETRIES}")

    raise RuntimeError("не удалось получить валидный JSON от модели")


def load_topics(cur, subject_id: int) -> list[dict]:
    cur.execute(
        "SELECT id, title, parent_id, parent_origin FROM topics "
        "WHERE subject_id = %s ORDER BY title",
        (subject_id,),
    )
    return [
        {"id": r[0], "title": r[1], "parent_id": r[2], "origin": r[3]}
        for r in cur.fetchall()
    ]


def resolve_pairs(raw: list, topics: list[dict], skipped: list) -> list[tuple]:
    by_title = {t["title"]: t["id"] for t in topics}
    title_of = {t["id"]: t["title"] for t in topics}

    final_parent = {
        t["id"]: t["parent_id"]
        for t in topics
        if t["origin"] == "manual" and t["parent_id"] is not None
    }

    accepted = []

    for item in raw:
        if not isinstance(item, dict):
            skipped.append(f"не объект: {item}")
            continue

        child_title = item.get("child")
        parent_title = item.get("parent")

        if not isinstance(child_title, str) or not isinstance(parent_title, str):
            skipped.append(f"не строки: {item}")
            continue

        child_title = child_title.strip()
        parent_title = parent_title.strip()

        if child_title not in by_title:
            skipped.append(f"'{child_title}' нет в базе")
            continue
        if parent_title not in by_title:
            skipped.append(f"родителя '{parent_title}' нет в базе")
            continue

        child_id = by_title[child_title]
        parent_id = by_title[parent_title]

        if child_id == parent_id:
            skipped.append(f"'{child_title}' сам себе родитель")
            continue
        if child_id in final_parent:
            skipped.append(f"у '{child_title}' уже есть родитель")
            continue
        if parent_id in final_parent:
            skipped.append(
                f"'{parent_title}' сам ребёнок "
                f"'{title_of[final_parent[parent_id]]}', пропуск '{child_title}'"
            )
            continue
        if child_id in final_parent.values():
            skipped.append(f"'{child_title}' уже является родителем, не может стать ребёнком")
            continue

        final_parent[child_id] = parent_id
        accepted.append((child_id, parent_id, child_title, parent_title))

    return accepted


def build_hierarchy(subject_id: int) -> dict:
    skipped = []

    with get_connection() as conn:
        with conn.cursor() as cur:
            topics = load_topics(cur, subject_id)

            if not topics:
                return {"subject_id": subject_id, "status": "no_topics"}

            titles = [t["title"] for t in topics]
            print(f"subject {subject_id}: {len(titles)} тем")

            raw, response = ask_model(titles)
            pairs = resolve_pairs(raw.get("pairs", []), topics, skipped)

            conn.commit()

            cur.execute(
                "SELECT count(*) FILTER (WHERE parent_id IS NULL), "
                "count(*) FILTER (WHERE parent_id IS NOT NULL) "
                "FROM topics WHERE subject_id = %s",
                (subject_id,),
            )
            roots, children_total = cur.fetchone()

    for reason in skipped:
        print(f"  пропущено: {reason}")

    usage = response.usage
    return {
        "subject_id": subject_id,
        "topics": len(titles),
        "roots": roots,
        "children": children_total,
        "new_links": len(pairs),
        "skipped": len(skipped),
        "tokens": f"{usage.prompt_tokens} / {usage.completion_tokens}",
        "status": "done",
    }

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("использование: py build_hierarchy.py <subject_id>")
        sys.exit(1)

    stats = build_hierarchy(int(sys.argv[1]))
    print()
    print(json.dumps(stats, indent=2, ensure_ascii=False))