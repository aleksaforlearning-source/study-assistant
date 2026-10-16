import os

from dotenv import load_dotenv
from groq import Groq

import hashlib
from enum import Enum

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from pydantic import BaseModel

from db import get_connection
from pdf_parser import parse_pdf
from topic_extractor import extract_topics
from build_hierarchy import build_hierarchy

from embedder import embed_source, embed_texts

app = FastAPI(title="Study Assistant")

load_dotenv()

groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
ASK_MODEL = "openai/gpt-oss-20b"

ASK_SYSTEM_PROMPT = """You help a student understand their own lecture material.

Answer in two clearly separated and labelled parts.

PART 1 — From the lecture:
- Use only what the fragments say
- Cite the page after each claim, like [page 29]
- If the fragments do not answer the question, say so plainly here

PART 2 — My explanation:
- Explain the same idea in simpler words, with an analogy or a tiny example
- Never contradict PART 1
- Never introduce notation or terms the course does not use
- If you are not confident, say so instead of guessing
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


class TopicPatch(BaseModel):
    title: str | None = None
    parent_id: int | None = None

class AskIn(BaseModel):
    question: str
    limit: int = 5
    explain: bool = True


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
    other = "other"
    
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

def find_chunks(subject_id: int, query: str, limit: int) -> list[dict]:
    query_vector = str(embed_texts([query])[0])

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.id, c.page_number, c.text, s.filename, "
                "c.embedding <=> %s::vector AS distance "
                "FROM chunks c JOIN sources s ON s.id = c.source_id "
                "WHERE s.subject_id = %s AND c.embedding IS NOT NULL "
                "ORDER BY c.embedding <=> %s::vector LIMIT %s",
                (query_vector, subject_id, query_vector, limit),
            )
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
                "SELECT t.id, t.title, t.parent_id, p.title, t.subject_id "
                "FROM topics t LEFT JOIN topics p ON p.id = t.parent_id "
                "WHERE t.id = %s",
                (topic_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"topic {topic_id} not found")

            cur.execute(
                "SELECT c.id, c.page_number, c.text, s.filename, s.type "
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
        "chunks": chunks,
        "notes": notes,
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

    chunks = find_chunks(subject_id, question, body.limit)

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
                "count(DISTINCT tc.topic_id) FILTER (WHERE t.status = 'review') AS review "
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
                }
                for r in cur.fetchall()
            ]