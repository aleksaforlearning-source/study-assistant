import hashlib
from enum import Enum

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from pydantic import BaseModel

from db import get_connection
from pdf_parser import parse_pdf
from topic_extractor import extract_topics
from build_hierarchy import build_hierarchy

app = FastAPI(title="Study Assistant")


class SubjectIn(BaseModel):
    name: str
    exam_date: str | None = None


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