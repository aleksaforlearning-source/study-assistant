import os

from dotenv import load_dotenv
from groq import Groq, RateLimitError

import hashlib
from contextlib import asynccontextmanager
from enum import Enum

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks, Response
from pydantic import BaseModel

from db import get_connection
from pdf_parser import parse_pdf
from topic_extractor import extract_topics
from build_hierarchy import build_hierarchy

from embedder import embed_source, embed_texts

from fastapi.staticfiles import StaticFiles

from pipeline import run_pipeline

from summarizer import EmptySummaryError, summarize_topic
from math_pages import enqueue_math_pages, start_worker

@asynccontextmanager
async def lifespan(app: FastAPI):
    start_worker()
    yield


app = FastAPI(title="Study Assistant", lifespan=lifespan)

load_dotenv()

groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
ASK_MODEL = "openai/gpt-oss-20b"

ASK_SYSTEM_PROMPT = r"""You help a student understand their own lecture material.
Answer in two clearly separated and labelled parts.

PART 1 — From the lecture:
- Use only what the fragments say
- Cite the page after each claim, like [page 29]
- If the fragments do not answer the question, say so plainly here
- Write math in plain text, never LaTeX: O(log n), not \(O(\log n)\)

PART 2 — My explanation:
- Explain the same idea in simpler words, with an analogy or a tiny example
- Never contradict PART 1
- Never introduce notation or terms the course does not use
- If you are not confident, say so instead of guessing
- If the fragments show which convention the course uses, commit to it; do not
  offer both alternatives
- Keep it to a few sentences

Answer in the same language the question is asked in.
"""


ASK_STRICT_PROMPT = """You answer a student's question using only the lecture fragments provided.

Rules:
- Use only information from the fragments. If they do not contain the answer, say so plainly.
- Never add facts that are not in the fragments
- After each claim, cite the page in square brackets, like [page 29]
- Answer in the same language the question is asked in
- Be concise
"""


class SubjectIn(BaseModel):
    name: str
    exam_date: str | None = None

class NoteIn(BaseModel):
    text: str
    kind: str = "own_explanation"


class NotePatch(BaseModel):
    text: str


TOPIC_STATUSES = ("new", "known", "review")

class TopicPatch(BaseModel):
    title: str | None = None
    parent_id: int | None = None
    status: str | None = None
    summary: str | None = None

class AskIn(BaseModel):
    question: str
    limit: int = 5
    explain: bool = True
    topic_id: int | None = None

class BlockIn(BaseModel):
    name: str


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.post("/subjects")
def create_subject(subject: SubjectIn):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO subjects (name, exam_date) VALUES (%s, %s) RETURNING id",
                (subject.name, subject.exam_date),
            )
            new_id = cur.fetchone()[0]
    return {"id": new_id, "name": subject.name}


@app.get("/subjects")
def list_subjects():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, exam_date FROM subjects ORDER BY id")
            rows = cur.fetchall()
    return [
        {"id": row[0], "name": row[1], "exam_date": row[2]}
        for row in rows
    ]

from enum import Enum


class SourceType(str, Enum):
    lecture = "lecture"
    homework = "homework"
    past_exam = "past_exam"
    concept = "concept"
    my_solution = "my_solution"
    other = "other"


# Only lectures build topics on upload; the rest is stored and analysed on request.
AUTO_PROCESSED_TYPES = {SourceType.lecture}
    
@app.post("/subjects/{subject_id}/sources")
def upload_source(
    subject_id: int,
    source_type: SourceType = Form(...),
    file: UploadFile = File(...),
):
    data = file.file.read()

    if file.content_type != "application/pdf":
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type: {file.content_type}. Only PDF for now.",
        )

    content_hash = hashlib.sha256(data).hexdigest()

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM sources WHERE subject_id = %s AND content_hash = %s",
                (subject_id, content_hash),
            )
            existing = cur.fetchone()
            if existing:
                return {
                    "source_id": existing[0],
                    "filename": file.filename,
                    "status": "already_uploaded",
                }

            chunks = parse_pdf(data)

            cur.execute(
                "INSERT INTO sources (subject_id, type, filename, content_hash) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (subject_id, source_type.value, file.filename, content_hash),
            )
            source_id = cur.fetchone()[0]

            for chunk in chunks:
                cur.execute(
                    "INSERT INTO chunks (source_id, text, page_number, is_weak, drawings_count) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (source_id, chunk["text"], chunk["page_number"],
                     chunk["is_weak"], chunk["drawings_count"]),
                )

    weak_count = sum(1 for c in chunks if c["is_weak"])
    return {
        "source_id": source_id,
        "filename": file.filename,
        "chunks": len(chunks),
        "weak_chunks": weak_count,
        "status": "processed",
    }

def find_chunks(
    subject_id: int,
    query: str,
    limit: int,
    topic_id: int | None = None,
) -> list[dict]:
    query_vector = str(embed_texts([query])[0])

    head = (
        "SELECT c.id, c.page_number, c.text, s.filename, "
        "c.embedding <=> %s::vector AS distance "
        "FROM chunks c JOIN sources s ON s.id = c.source_id "
    )
    tail = "ORDER BY c.embedding <=> %s::vector LIMIT %s"

    if topic_id is None:
        sql = head + "WHERE s.subject_id = %s AND c.embedding IS NOT NULL " + tail
        params = (query_vector, subject_id, query_vector, limit)
    else:
        sql = (
            head
            + "JOIN topic_chunks tc ON tc.chunk_id = c.id "
            + "WHERE s.subject_id = %s AND c.embedding IS NOT NULL "
            + "AND tc.topic_id = %s " + tail
        )
        params = (query_vector, subject_id, topic_id, query_vector, limit)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [
                {
                    "chunk_id": r[0],
                    "page": r[1],
                    "text": r[2],
                    "filename": r[3],
                    "distance": round(r[4], 4),
                }
                for r in cur.fetchall()
            ]
        
@app.post("/sources/{source_id}/topics")
def start_topic_extraction(source_id: int, background_tasks: BackgroundTasks):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT subject_id FROM sources WHERE id = %s", (source_id,))
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail=f"source {source_id} not found")

    background_tasks.add_task(extract_topics, source_id)

    return {
        "source_id": source_id,
        "status": "started",
        "note": "takes 2-4 minutes, watch the uvicorn log",
    }


@app.post("/subjects/{subject_id}/hierarchy")
def rebuild_hierarchy(subject_id: int):
    stats = build_hierarchy(subject_id)

    if stats.get("status") == "no_topics":
        raise HTTPException(status_code=400, detail="no topics for this subject yet")

    return stats


@app.get("/subjects/{subject_id}/priority")
def get_priority(subject_id: int):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(p.id, t.id) AS root_id, COALESCE(p.title, t.title) AS root_title, "
                "count(tc.chunk_id) AS chunks "
                "FROM topics t "
                "LEFT JOIN topics p ON p.id = t.parent_id "
                "LEFT JOIN topic_chunks tc ON tc.topic_id = t.id "
                "WHERE t.subject_id = %s "
                "GROUP BY COALESCE(p.id, t.id), COALESCE(p.title, t.title) "
                "ORDER BY chunks DESC",
                (subject_id,),
            )
            branches = [
                {"topic_id": r[0], "title": r[1], "chunks": r[2]}
                for r in cur.fetchall()
            ]

            cur.execute(
                "SELECT t.parent_id, t.id, t.title, count(tc.chunk_id) AS chunks "
                "FROM topics t LEFT JOIN topic_chunks tc ON tc.topic_id = t.id "
                "WHERE t.subject_id = %s AND t.parent_id IS NOT NULL "
                "GROUP BY t.parent_id, t.id, t.title "
                "ORDER BY chunks DESC",
                (subject_id,),
            )
            children_rows = cur.fetchall()

    children = {}
    for parent_id, topic_id, title, chunks in children_rows:
        children.setdefault(parent_id, []).append(
            {"topic_id": topic_id, "title": title, "chunks": chunks}
        )

    for branch in branches:
        branch["children"] = children.get(branch["topic_id"], [])

    return {"subject_id": subject_id, "branches": branches}

@app.get("/topics/{topic_id}")
def get_topic(topic_id: int):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT t.id, t.title, t.parent_id, p.title, t.subject_id, t.status, "
                "t.summary, t.summary_origin "                
                "FROM topics t LEFT JOIN topics p ON p.id = t.parent_id "
                "WHERE t.id = %s",
                (topic_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"topic {topic_id} not found")

            cur.execute(
                "SELECT c.id, c.page_number, c.text, s.filename, s.type, s.id "
                "FROM chunks c JOIN topic_chunks tc ON tc.chunk_id = c.id "
                "JOIN sources s ON s.id = c.source_id "
                "WHERE tc.topic_id = %s ORDER BY s.id, c.page_number",
                (topic_id,),
            )
            chunks = [
                {
                    "chunk_id": r[0],
                    "page": r[1],
                    "text": r[2],
                    "filename": r[3],
                    "source_type": r[4],
                    "source_id": r[5],
                }
                
                for r in cur.fetchall()
            ]

            cur.execute(
                "SELECT id, text, kind, created_at FROM notes "
                "WHERE topic_id = %s ORDER BY created_at",
                (topic_id,),
            )
            notes = [
                {"note_id": r[0], "text": r[1], "kind": r[2], "created_at": r[3]}
                for r in cur.fetchall()
            ]

    return {
        "topic_id": row[0],
        "title": row[1],
        "parent_id": row[2],
        "parent_title": row[3],
        "subject_id": row[4],
        "status": row[5],
        "chunks": chunks,
        "notes": notes,
        "summary": row[6],
        "summary_origin": row[7],
    }


@app.post("/topics/{topic_id}/notes")
def add_note(topic_id: int, note: NoteIn):
    if not note.text.strip():
        raise HTTPException(status_code=400, detail="note text cannot be empty")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM topics WHERE id = %s", (topic_id,))
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail=f"topic {topic_id} not found")

            cur.execute(
                "INSERT INTO notes (topic_id, text, kind) VALUES (%s, %s, %s) RETURNING id, created_at",
                (topic_id, note.text.strip(), note.kind),
            )
            note_id, created_at = cur.fetchone()

    return {"note_id": note_id, "topic_id": topic_id, "created_at": created_at}


@app.patch("/notes/{note_id}")
def update_note(note_id: int, patch: NotePatch):
    if not patch.text.strip():
        raise HTTPException(status_code=400, detail="note text cannot be empty")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE notes SET text = %s WHERE id = %s RETURNING topic_id",
                (patch.text.strip(), note_id),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"note {note_id} not found")

    return {"note_id": note_id, "topic_id": row[0]}


@app.patch("/topics/{topic_id}")
def update_topic(topic_id: int, patch: TopicPatch):
    fields = patch.model_fields_set

    if not fields:
        raise HTTPException(status_code=400, detail="nothing to update")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT subject_id FROM topics WHERE id = %s", (topic_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"topic {topic_id} not found")
            subject_id = row[0]

            if "title" in fields:
                if not patch.title or not patch.title.strip():
                    raise HTTPException(status_code=400, detail="title cannot be empty")
                cur.execute(
                    "UPDATE topics SET title = %s WHERE id = %s",
                    (patch.title.strip(), topic_id),
                )
            if "status" in fields:
                if patch.status not in TOPIC_STATUSES:
                    raise HTTPException(
                        status_code=400,
                        detail=f"status must be one of {TOPIC_STATUSES}",
                    )
                cur.execute(
                    "UPDATE topics SET status = %s WHERE id = %s",
                    (patch.status, topic_id),
                )
            if "summary" in fields:
                cur.execute(
                    "UPDATE topics SET summary = %s, summary_origin = 'manual' WHERE id = %s",
                    (patch.summary, topic_id),
                )            
            if "parent_id" in fields:
                if patch.parent_id is not None:
                    if patch.parent_id == topic_id:
                        raise HTTPException(status_code=400, detail="topic cannot be its own parent")

                    cur.execute(
                        "SELECT parent_id, subject_id FROM topics WHERE id = %s",
                        (patch.parent_id,),
                    )
                    parent = cur.fetchone()

                    if parent is None:
                        raise HTTPException(status_code=404, detail=f"parent {patch.parent_id} not found")
                    if parent[1] != subject_id:
                        raise HTTPException(status_code=400, detail="parent belongs to another subject")
                    if parent[0] is not None:
                        raise HTTPException(status_code=400, detail="parent already has a parent, tree is two levels")

                    cur.execute("SELECT 1 FROM topics WHERE parent_id = %s LIMIT 1", (topic_id,))
                    if cur.fetchone() is not None:
                        raise HTTPException(
                            status_code=400,
                            detail="this topic has subtopics, move them out first, tree is two levels",
                        )

                cur.execute(
                    "UPDATE topics SET parent_id = %s, parent_origin = 'manual' WHERE id = %s",
                    (patch.parent_id, topic_id),
                )

    return get_topic(topic_id)

@app.post("/sources/{source_id}/embeddings")
def start_embedding(source_id: int, background_tasks: BackgroundTasks):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM sources WHERE id = %s", (source_id,))
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail=f"source {source_id} not found")

    background_tasks.add_task(embed_source, source_id)
    return {"source_id": source_id, "status": "started"}


@app.get("/subjects/{subject_id}/search")
def search(subject_id: int, q: str, limit: int = 5):
    if not q.strip():
        raise HTTPException(status_code=400, detail="query cannot be empty")

    chunks = find_chunks(subject_id, q, limit)

    for chunk in chunks:
        chunk["text"] = chunk["text"][:300]

    return {"query": q, "results": chunks}

def build_ask_prompt(question: str, chunks: list[dict]) -> str:
    fragments = "\n\n".join(
        f"[page {c['page']}]\n{c['text']}" for c in chunks
    )

    return f"""Lecture fragments:

{fragments}

Question: {question}"""


@app.post("/subjects/{subject_id}/ask")
def ask(subject_id: int, body: AskIn):
    question = body.question.strip()

    if not question:
        raise HTTPException(status_code=400, detail="question cannot be empty")

    chunks = find_chunks(subject_id, question, body.limit, body.topic_id)
    
    if not chunks:
        raise HTTPException(status_code=404, detail="no material with embeddings for this subject")

    system_prompt = ASK_SYSTEM_PROMPT if body.explain else ASK_STRICT_PROMPT

    response = groq_client.chat.completions.create(
        model=ASK_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": build_ask_prompt(question, chunks)},
        ],
        temperature=0.2,
    )

    return {
        "question": question,
        "topic_id": body.topic_id,
        "answer": response.choices[0].message.content,
        "sources": [
            {"page": c["page"], "filename": c["filename"], "distance": c["distance"]}
            for c in chunks
        ],
        "tokens": f"{response.usage.prompt_tokens} / {response.usage.completion_tokens}",
    }
@app.get("/subjects/{subject_id}/blocks")
def list_blocks(subject_id: int):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT b.id, b.name, b.position, "
                "count(DISTINCT s.id) AS files, "
                "count(DISTINCT tc.topic_id) AS topics, "
                "count(DISTINCT tc.chunk_id) AS weight, "
                "count(DISTINCT tc.topic_id) FILTER (WHERE t.status = 'new') AS fresh, "
                "count(DISTINCT tc.topic_id) FILTER (WHERE t.status = 'known') AS known, "
                "count(DISTINCT tc.topic_id) FILTER (WHERE t.status = 'review') AS review, "
                "count(DISTINCT c.id) FILTER (WHERE c.formula_status IS NOT NULL) AS formula_pages, "
                "count(DISTINCT c.id) FILTER (WHERE c.formula_status IN ('done', 'failed')) AS formula_done "
                "FROM blocks b "
                "LEFT JOIN sources s ON s.block_id = b.id "
                "LEFT JOIN chunks c ON c.source_id = s.id "
                "LEFT JOIN topic_chunks tc ON tc.chunk_id = c.id "
                "LEFT JOIN topics t ON t.id = tc.topic_id "
                "WHERE b.subject_id = %s "
                "GROUP BY b.id ORDER BY b.position",
                (subject_id,),
            )
            return [
                {
                    "block_id": r[0],
                    "name": r[1],
                    "position": r[2],
                    "files": r[3],
                    "topics": r[4],
                    "weight": r[5],
                    "status": {"new": r[6], "known": r[7], "review": r[8]},
                    "formulas": {"pages": r[9], "done": r[10]},
                }
                for r in cur.fetchall()
            ]

@app.get("/blocks/{block_id}/topics")
def list_block_topics(block_id: int):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT t.id, t.title, t.parent_id, t.status, "
                "count(DISTINCT tc.chunk_id) AS weight, "
                "min(c.page_number) AS first_page, "
                "t.summary IS NOT NULL AS has_summary "
                "FROM topics t "
                "JOIN topic_chunks tc ON tc.topic_id = t.id "
                "JOIN chunks c ON c.id = tc.chunk_id "
                "JOIN sources s ON s.id = c.source_id "
                "WHERE s.block_id = %s "
                "GROUP BY t.id ORDER BY min(c.page_number), t.title",
                (block_id,),
            )
            return [
                {
                    "id": r[0],
                    "title": r[1],
                    "parent_id": r[2],
                    "status": r[3],
                    "weight": r[4],
                    "first_page": r[5],
                    "has_summary": r[6],
                }
                for r in cur.fetchall()
            ]
        
@app.delete("/topics/{topic_id}")
def delete_topic(topic_id: int):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT title FROM topics WHERE id = %s", (topic_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"topic {topic_id} not found")
            title = row[0]

            cur.execute("UPDATE topics SET parent_id = NULL WHERE parent_id = %s", (topic_id,))
            cur.execute("DELETE FROM notes WHERE topic_id = %s", (topic_id,))
            cur.execute("DELETE FROM topic_chunks WHERE topic_id = %s", (topic_id,))
            cur.execute("DELETE FROM topics WHERE id = %s", (topic_id,))

    return {"deleted": topic_id, "title": title}

def topics_only_from(cur, source_id: int) -> list[int]:
    """Topics whose every linked chunk belongs to this source."""
    cur.execute(
        "SELECT DISTINCT tc.topic_id FROM topic_chunks tc "
        "JOIN chunks c ON c.id = tc.chunk_id WHERE c.source_id = %s "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM topic_chunks tc2 JOIN chunks c2 ON c2.id = tc2.chunk_id "
        "  WHERE tc2.topic_id = tc.topic_id AND c2.source_id <> %s)",
        (source_id, source_id),
    )
    return [r[0] for r in cur.fetchall()]


@app.get("/sources/{source_id}/delete-preview")
def preview_source_deletion(source_id: int):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT filename FROM sources WHERE id = %s", (source_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"source {source_id} not found")

            topic_ids = topics_only_from(cur, source_id)
            cur.execute("SELECT count(*) FROM notes WHERE topic_id = ANY(%s)", (topic_ids,))
            notes = cur.fetchone()[0]

    return {"source_id": source_id, "filename": row[0], "topics": len(topic_ids), "notes": notes}


@app.delete("/sources/{source_id}")
def delete_source(source_id: int):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT filename FROM sources WHERE id = %s", (source_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"source {source_id} not found")

            cur.execute(
                "SELECT 1 FROM jobs WHERE source_id = %s AND status = 'running'", (source_id,)
            )
            if cur.fetchone():
                raise HTTPException(
                    status_code=409,
                    detail="this file is still being processed, delete it when that finishes",
                )

            # Collected before the links go, while it is still known where topics came from.
            topic_ids = topics_only_from(cur, source_id)

            cur.execute(
                "DELETE FROM topic_chunks WHERE chunk_id IN "
                "(SELECT id FROM chunks WHERE source_id = %s)",
                (source_id,),
            )
            if topic_ids:
                cur.execute("UPDATE topics SET parent_id = NULL WHERE parent_id = ANY(%s)", (topic_ids,))
                for table in ("notes", "techniques", "struggles", "exam_questions"):
                    cur.execute(f"DELETE FROM {table} WHERE topic_id = ANY(%s)", (topic_ids,))
                cur.execute("DELETE FROM topics WHERE id = ANY(%s)", (topic_ids,))

            for table in ("techniques", "exam_questions", "chunks", "jobs"):
                cur.execute(f"DELETE FROM {table} WHERE source_id = %s", (source_id,))
            cur.execute("DELETE FROM sources WHERE id = %s", (source_id,))

    return {"deleted": source_id, "filename": row[0], "topics_removed": len(topic_ids)}


app.mount("/app", StaticFiles(directory="static", html=True), name="static")

@app.post("/subjects/{subject_id}/blocks")
def create_block(subject_id: int, body: BlockIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name cannot be empty")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM subjects WHERE id = %s", (subject_id,))
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail=f"subject {subject_id} not found")

            cur.execute(
                "INSERT INTO blocks (subject_id, name, position) VALUES (%s, %s, "
                "COALESCE((SELECT max(position) + 1 FROM blocks WHERE subject_id = %s), 1)) "
                "RETURNING id, position",
                (subject_id, name, subject_id),
            )
            block_id, position = cur.fetchone()

    return {"block_id": block_id, "name": name, "position": position}


@app.post("/blocks/{block_id}/upload")
def upload_to_block(
    block_id: int,
    background_tasks: BackgroundTasks,
    source_type: SourceType = Form(...),
    file: UploadFile = File(...),
):
    if file.content_type != "application/pdf":
        raise HTTPException(
            status_code=415,
            detail=f"only PDF for now, got {file.content_type}",
        )

    data = file.file.read()
    content_hash = hashlib.sha256(data).hexdigest()

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT subject_id FROM blocks WHERE id = %s", (block_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"block {block_id} not found")
            subject_id = row[0]

            cur.execute(
                "SELECT id FROM sources WHERE subject_id = %s AND content_hash = %s",
                (subject_id, content_hash),
            )
            existing = cur.fetchone()
            if existing:
                raise HTTPException(
                    status_code=409,
                    detail=f"already uploaded as source {existing[0]}",
                )

            chunks = parse_pdf(data)

            cur.execute(
                "INSERT INTO sources (subject_id, block_id, type, filename, content_hash, pdf) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (subject_id, block_id, source_type.value, file.filename, content_hash, data),
            )
            source_id = cur.fetchone()[0]

            for chunk in chunks:
                cur.execute(
                    "INSERT INTO chunks (source_id, text, page_number, is_weak, drawings_count) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (source_id, chunk["text"], chunk["page_number"],
                     chunk["is_weak"], chunk["drawings_count"]),
                )

            job_id = None
            if source_type in AUTO_PROCESSED_TYPES:
                cur.execute(
                    "INSERT INTO jobs (source_id) VALUES (%s) RETURNING id",
                    (source_id,),
                )
                job_id = cur.fetchone()[0]

    # Runs after the response, when the chunks above are committed and visible.
    if job_id is not None:
        background_tasks.add_task(run_pipeline, job_id, source_id, subject_id, block_id)
    elif source_type != SourceType.my_solution:
        # Fix formulas now, so a later analyse reads clean text. Own solutions are
        # never analysed by default, so they cost no vision tokens.
        background_tasks.add_task(enqueue_math_pages, source_id)

    return {
        "job_id": job_id,
        "source_id": source_id,
        "filename": file.filename,
        "chunks": len(chunks),
        "weak_chunks": sum(1 for c in chunks if c["is_weak"]),
    }


@app.get("/jobs/{job_id}")
def get_job(job_id: int):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, source_id, stage, status, message FROM jobs WHERE id = %s",
                (job_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"job {job_id} not found")

    return {
        "job_id": row[0],
        "source_id": row[1],
        "stage": row[2],
        "status": row[3],
        "message": row[4],
    }
@app.post("/topics/{topic_id}/summary")
def generate_summary(topic_id: int):
    try:
        return summarize_topic(topic_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except EmptySummaryError as error:
        raise HTTPException(status_code=502, detail=str(error))
    except RateLimitError as error:
        retry_after = error.response.headers.get("retry-after")
        raise HTTPException(
            status_code=429,
            detail="Groq rate limit reached, try again later",
            headers={"Retry-After": retry_after} if retry_after else None,
        )
@app.get("/sources/{source_id}/pdf")
def get_source_pdf(source_id: int):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT filename, pdf FROM sources WHERE id = %s", (source_id,))
            row = cur.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail=f"source {source_id} not found")
    if row[1] is None:
        raise HTTPException(status_code=404, detail="no PDF stored for this file")

    return Response(
        content=bytes(row[1]),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{row[0]}"'},
    )


@app.get("/blocks/{block_id}/files")
def list_block_files(block_id: int):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, filename, type, (pdf IS NOT NULL) "
                "FROM sources WHERE block_id = %s ORDER BY id",
                (block_id,),
            )
            return [
                {"source_id": r[0], "filename": r[1], "type": r[2], "has_pdf": r[3]}
                for r in cur.fetchall()
            ]


@app.post("/sources/{source_id}/pdf")
def attach_pdf(source_id: int, file: UploadFile = File(...)):
    data = file.file.read()
    content_hash = hashlib.sha256(data).hexdigest()

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT content_hash FROM sources WHERE id = %s", (source_id,))
            row = cur.fetchone()

            if row is None:
                raise HTTPException(status_code=404, detail=f"source {source_id} not found")
            if row[0] != content_hash:
                raise HTTPException(
                    status_code=400,
                    detail="this is a different file than the one originally uploaded",
                )

            cur.execute("UPDATE sources SET pdf = %s WHERE id = %s", (data, source_id))
            conn.commit()

    return {"source_id": source_id, "bytes": len(data)}