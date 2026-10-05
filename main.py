from fastapi import FastAPI, UploadFile, File, Form
from pydantic import BaseModel
from pypdf import PdfReader

from db import get_connection

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
    reader = PdfReader(file.file)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sources (subject_id, type, filename) "
                "VALUES (%s, %s, %s) RETURNING id",
                (subject_id, source_type, file.filename),
            )
            source_id = cur.fetchone()[0]

            saved = 0
            for page_number, page in enumerate(reader.pages, start=1):
                text = (page.extract_text() or "").strip()
                if not text:
                    continue
                cur.execute(
                    "INSERT INTO chunks (source_id, text, page_number) "
                    "VALUES (%s, %s, %s)",
                    (source_id, text, page_number),
                )
                saved += 1

    return {"source_id": source_id, "filename": file.filename, "chunks": saved}