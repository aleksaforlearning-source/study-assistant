import os

from dotenv import load_dotenv
from groq import Groq, RateLimitError

import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote
from datetime import date
from enum import Enum

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks, Response
from pydantic import BaseModel

from db import get_connection
from pdf_parser import files_to_pdf, parse_pdf
from topic_extractor import extract_topics
from build_hierarchy import build_hierarchy

from embedder import embed_source, embed_texts

from fastapi.staticfiles import StaticFiles

from pipeline import fail_interrupted_jobs, run_pipeline

from summarizer import EmptySummaryError, summarize_topic
from exam_analyzer import run_exam_analysis
from math_pages import enqueue_vision_pages, start_worker

@asynccontextmanager
async def lifespan(app: FastAPI):
    interrupted = fail_interrupted_jobs()
    if interrupted:
        print(f"{interrupted} job(s) were cut off by the restart and are marked failed")
    start_worker()
    yield


app = FastAPI(title="Study Assistant", lifespan=lifespan)

load_dotenv()

groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
# The larger model: the assistant is asked rarely, and its answers must really
# explain. Summaries stay on the smaller one, there are many more of them.
ASK_MODEL = os.getenv("GROQ_ASK_MODEL", "openai/gpt-oss-120b")

ASK_SYSTEM_PROMPT = r"""You are a patient tutor helping a student understand their course.
Answer in two clearly labelled parts.

PART 1 — From the lecture:
- One to three sentences: what the lecture fragments say about the question,
  with [page N] after each claim
- Keep the course's own notation and terms
- If the fragments do not cover the question, say so in one sentence

PART 2 — Explanation:
- Always give a real explanation, even when the lecture does not cover the
  question: use your general knowledge, the way a good tutor would
- Answer the question that was asked, not the whole topic. About 150 to 250
  words; go longer only if the question really needs it
- The intuition first (what problem it solves, a mental picture), then the
  mechanism in a few steps
- At most one short example, and only if it helps. Keep it to a few one-line
  arithmetic steps; never do long hand calculations such as multiplying
  matrices: start from small given values whose result is easy to see.
  A wrong number misleads the student
- Headings and tables only when the answer really has separate parts
- Connect it to what the lecture covers when you can; when you go beyond it,
  say so briefly, e.g. "Not in your slides yet:"
- Never contradict the lecture; if it uses a specific convention, commit to it
- Formulas as LaTeX: inline \( ... \), display $$ ... $$
- No code unless the student asks for it
- End with one short line suggesting what to ask next to go deeper

In a follow-up question, build on your earlier answers in this conversation
instead of repeating them.

Answer in the same language the question is asked in.
"""

# The conversation sent along with a follow-up question: enough to keep the
# thread, small enough to keep each request cheap.
MAX_HISTORY_TURNS = 4
MAX_HISTORY_ANSWER_CHARS = 4000
# Reasoning tokens count too; a low limit cut answers off in the middle of a formula.
ASK_MAX_TOKENS = 16000


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
    semester_id: int | None = None


class SemesterIn(BaseModel):
    name: str

# Every table with a topic_id column. Deleting a topic clears these first,
# because the database refuses to delete a row something still points at.
TOPIC_CHILD_TABLES = ("notes", "techniques", "struggles", "exam_question_topics", "topic_chunks")


class SubjectPatch(BaseModel):
    name: str | None = None
    # null clears the date; leaving the field out keeps it as it is
    exam_date: date | None = None
    # moves the subject to another semester
    semester_id: int | None = None


class NoteIn(BaseModel):
    text: str
    kind: str = "own_explanation"
    # the quoted text the note sits on, for a note made from a selection
    anchor: str | None = None
    # "come back to this": counts lightly as a weak spot in Focus
    revisit: bool = False


class NotePatch(BaseModel):
    # only the fields that are sent change
    text: str | None = None
    revisit: bool | None = None


class StruggleIn(BaseModel):
    text: str
    comment: str | None = None


class StruggleResolve(BaseModel):
    explanation: str | None = None
    revisit: bool = False


TOPIC_STATUSES = ("new", "known", "review")

class TopicPatch(BaseModel):
    title: str | None = None
    parent_id: int | None = None
    status: str | None = None
    summary: str | None = None

class AskTurn(BaseModel):
    question: str
    answer: str


class AskIn(BaseModel):
    question: str
    limit: int = 5
    explain: bool = True
    topic_id: int | None = None
    # earlier questions and answers of the same thread, oldest first
    history: list[AskTurn] = []

class BlockIn(BaseModel):
    name: str


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.post("/subjects")
def create_subject(subject: SubjectIn):
    with get_connection() as conn:
        with conn.cursor() as cur:
            if subject.semester_id is not None:
                cur.execute("SELECT 1 FROM semesters WHERE id = %s", (subject.semester_id,))
                if cur.fetchone() is None:
                    raise HTTPException(status_code=404, detail=f"semester {subject.semester_id} not found")
            cur.execute(
                "INSERT INTO subjects (name, exam_date, semester_id) VALUES (%s, %s, %s) RETURNING id",
                (subject.name, subject.exam_date, subject.semester_id),
            )
            new_id = cur.fetchone()[0]
    return {"id": new_id, "name": subject.name, "semester_id": subject.semester_id}


@app.get("/subjects")
def list_subjects():
    with get_connection() as conn:
        with conn.cursor() as cur:
            # topic counts are for the progress on the subjects page
            cur.execute(
                "SELECT s.id, s.name, s.exam_date, "
                "(SELECT count(*) FROM topics t WHERE t.subject_id = s.id), "
                "(SELECT count(*) FROM topics t WHERE t.subject_id = s.id AND t.status = 'known'), "
                "s.semester_id "
                "FROM subjects s ORDER BY s.id"
            )
            rows = cur.fetchall()
    return [
        {"id": row[0], "name": row[1], "exam_date": row[2], "topics": row[3], "known": row[4],
         "semester_id": row[5]}
        for row in rows
    ]


@app.patch("/subjects/{subject_id}")
def update_subject(subject_id: int, patch: SubjectPatch):
    if not patch.model_fields_set:
        raise HTTPException(status_code=400, detail="nothing to update")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM subjects WHERE id = %s", (subject_id,))
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail=f"subject {subject_id} not found")

            if "name" in patch.model_fields_set:
                name = (patch.name or "").strip()
                if not name:
                    raise HTTPException(status_code=400, detail="name cannot be empty")
                cur.execute("UPDATE subjects SET name = %s WHERE id = %s", (name, subject_id))

            if "exam_date" in patch.model_fields_set:
                cur.execute(
                    "UPDATE subjects SET exam_date = %s WHERE id = %s",
                    (patch.exam_date, subject_id),
                )

            if "semester_id" in patch.model_fields_set:
                cur.execute("SELECT 1 FROM semesters WHERE id = %s", (patch.semester_id,))
                if cur.fetchone() is None:
                    raise HTTPException(status_code=404, detail=f"semester {patch.semester_id} not found")
                cur.execute(
                    "UPDATE subjects SET semester_id = %s WHERE id = %s",
                    (patch.semester_id, subject_id),
                )

            cur.execute("SELECT id, name, exam_date, semester_id FROM subjects WHERE id = %s", (subject_id,))
            row = cur.fetchone()

    return {"id": row[0], "name": row[1], "exam_date": row[2], "semester_id": row[3]}


@app.get("/subjects/{subject_id}/delete-preview")
def preview_subject_deletion(subject_id: int):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT s.name, "
                "(SELECT count(*) FROM blocks WHERE subject_id = s.id), "
                "(SELECT count(*) FROM sources WHERE subject_id = s.id), "
                "(SELECT count(*) FROM topics WHERE subject_id = s.id), "
                "(SELECT count(*) FROM notes n JOIN topics t ON t.id = n.topic_id WHERE t.subject_id = s.id) "
                "FROM subjects s WHERE s.id = %s",
                (subject_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"subject {subject_id} not found")

    return {"subject_id": subject_id, "name": row[0], "blocks": row[1],
            "files": row[2], "topics": row[3], "notes": row[4]}


def refuse_while_processing(cur, subject_ids: list[int], what: str) -> None:
    cur.execute(
        "SELECT 1 FROM jobs j JOIN sources s ON s.id = j.source_id "
        "WHERE s.subject_id = ANY(%s) AND j.status = 'running'",
        (subject_ids,),
    )
    if cur.fetchone():
        raise HTTPException(
            status_code=409,
            detail=f"a file of this {what} is still being processed, delete it when that finishes",
        )


def remove_subject(cur, subject_id: int) -> None:
    """Deletes a subject with everything in it."""
    topics = "SELECT id FROM topics WHERE subject_id = %s"
    sources = "SELECT id FROM sources WHERE subject_id = %s"
    args = (subject_id,)

    # From the leaves to the root, so nothing still points at a deleted row.
    for table in TOPIC_CHILD_TABLES:
        cur.execute(f"DELETE FROM {table} WHERE topic_id IN ({topics})", args)
    cur.execute(
        f"DELETE FROM exam_question_topics WHERE question_id IN "
        f"(SELECT id FROM exam_questions WHERE source_id IN ({sources}))",
        args,
    )
    for table in ("techniques", "exam_questions"):
        cur.execute(f"DELETE FROM {table} WHERE source_id IN ({sources})", args)
    cur.execute(f"DELETE FROM topic_chunks WHERE chunk_id IN (SELECT id FROM chunks WHERE source_id IN ({sources}))", args)
    cur.execute("UPDATE topics SET parent_id = NULL WHERE subject_id = %s", args)
    cur.execute("DELETE FROM topics WHERE subject_id = %s", args)
    for table in ("chunks", "jobs"):
        cur.execute(f"DELETE FROM {table} WHERE source_id IN ({sources})", args)
    cur.execute("DELETE FROM sources WHERE subject_id = %s", args)
    cur.execute("DELETE FROM blocks WHERE subject_id = %s", args)
    cur.execute("DELETE FROM subjects WHERE id = %s", args)


@app.delete("/subjects/{subject_id}")
def delete_subject(subject_id: int):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM subjects WHERE id = %s", (subject_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"subject {subject_id} not found")

            refuse_while_processing(cur, [subject_id], "subject")
            remove_subject(cur, subject_id)

    return {"deleted": subject_id, "name": row[0]}


# ---- Semesters: the level above subjects ----

@app.get("/semesters")
def list_semesters():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT se.id, se.name, (SELECT count(*) FROM subjects s WHERE s.semester_id = se.id) "
                "FROM semesters se ORDER BY se.id"
            )
            return [{"id": r[0], "name": r[1], "subjects": r[2]} for r in cur.fetchall()]


@app.post("/semesters")
def create_semester(body: SemesterIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name cannot be empty")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO semesters (name) VALUES (%s) RETURNING id", (name,))
            semester_id = cur.fetchone()[0]
    return {"id": semester_id, "name": name}


@app.patch("/semesters/{semester_id}")
def rename_semester(semester_id: int, body: SemesterIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name cannot be empty")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE semesters SET name = %s WHERE id = %s RETURNING id", (name, semester_id))
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail=f"semester {semester_id} not found")
    return {"id": semester_id, "name": name}


@app.get("/semesters/{semester_id}/delete-preview")
def preview_semester_deletion(semester_id: int):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT se.name, "
                "(SELECT count(*) FROM subjects WHERE semester_id = se.id), "
                "(SELECT count(*) FROM sources so JOIN subjects s ON s.id = so.subject_id WHERE s.semester_id = se.id), "
                "(SELECT count(*) FROM topics t JOIN subjects s ON s.id = t.subject_id WHERE s.semester_id = se.id), "
                "(SELECT count(*) FROM notes n JOIN topics t ON t.id = n.topic_id "
                " JOIN subjects s ON s.id = t.subject_id WHERE s.semester_id = se.id) "
                "FROM semesters se WHERE se.id = %s",
                (semester_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"semester {semester_id} not found")

    return {"semester_id": semester_id, "name": row[0], "subjects": row[1],
            "files": row[2], "topics": row[3], "notes": row[4]}


@app.delete("/semesters/{semester_id}")
def delete_semester(semester_id: int):
    """Deletes the semester with its subjects, each the way a single subject is deleted."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM semesters WHERE id = %s", (semester_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"semester {semester_id} not found")

            cur.execute("SELECT id FROM subjects WHERE semester_id = %s", (semester_id,))
            subject_ids = [r[0] for r in cur.fetchall()]
            refuse_while_processing(cur, subject_ids, "semester")
            for subject_id in subject_ids:
                remove_subject(cur, subject_id)
            cur.execute("DELETE FROM semesters WHERE id = %s", (semester_id,))

    return {"deleted": semester_id, "name": row[0], "subjects_removed": len(subject_ids)}

from enum import Enum


class SourceType(str, Enum):
    lecture = "lecture"
    homework = "homework"
    past_exam = "past_exam"      # the final exam
    short_exam = "short_exam"    # a short test during the semester
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
        "c.embedding <=> %s::vector AS distance, c.source_id "
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
                    "source_id": r[5],
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
                "SELECT id, text, kind, created_at, anchor, revisit FROM notes "
                "WHERE topic_id = %s ORDER BY created_at",
                (topic_id,),
            )
            notes = [
                {"note_id": r[0], "text": r[1], "kind": r[2], "created_at": r[3],
                 "anchor": r[4], "revisit": r[5]}
                for r in cur.fetchall()
            ]

            cur.execute(
                "SELECT id, context, comment, created_at FROM struggles "
                "WHERE topic_id = %s AND event_type = 'unclear' AND resolved_at IS NULL "
                "ORDER BY created_at",
                (topic_id,),
            )
            unclear = [
                {"struggle_id": r[0], "text": r[1], "comment": r[2], "created_at": r[3]}
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
        "unclear": unclear,
        "summary": row[6],
        "summary_origin": row[7],
    }


@app.post("/topics/{topic_id}/struggles")
def mark_unclear(topic_id: int, mark: StruggleIn):
    text = mark.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="marked text cannot be empty")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM topics WHERE id = %s", (topic_id,))
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail=f"topic {topic_id} not found")

            cur.execute(
                "INSERT INTO struggles (topic_id, event_type, context, comment) "
                "VALUES (%s, 'unclear', %s, %s) RETURNING id",
                (topic_id, text, (mark.comment or "").strip() or None),
            )
            struggle_id = cur.fetchone()[0]

    return {"struggle_id": struggle_id, "topic_id": topic_id}


@app.post("/struggles/{struggle_id}/resolve")
def resolve_unclear(struggle_id: int, body: StruggleResolve):
    """Marks a place as understood; an explanation in own words becomes a note."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE struggles SET resolved_at = now() "
                "WHERE id = %s AND resolved_at IS NULL RETURNING topic_id, context",
                (struggle_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"open struggle {struggle_id} not found")
            topic_id, quote = row

            explanation = (body.explanation or "").strip()
            if explanation:
                # The explanation sits on the spot that was unclear, as a note on that text.
                cur.execute(
                    "INSERT INTO notes (topic_id, text, kind, anchor, revisit) "
                    "VALUES (%s, %s, 'own_explanation', %s, %s)",
                    (topic_id, explanation, quote, body.revisit),
                )

    return {"struggle_id": struggle_id, "topic_id": topic_id, "note_saved": bool(explanation)}


@app.delete("/struggles/{struggle_id}")
def delete_struggle(struggle_id: int):
    """For a place marked by mistake; resolving keeps the history, this does not."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM struggles WHERE id = %s RETURNING id", (struggle_id,))
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail=f"struggle {struggle_id} not found")

    return {"deleted": struggle_id}


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
                "INSERT INTO notes (topic_id, text, kind, anchor, revisit) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id, created_at",
                (topic_id, note.text.strip(), note.kind, (note.anchor or "").strip() or None, note.revisit),
            )
            note_id, created_at = cur.fetchone()

    return {"note_id": note_id, "topic_id": topic_id, "created_at": created_at}


@app.patch("/notes/{note_id}")
def update_note(note_id: int, patch: NotePatch):
    fields = patch.model_fields_set
    if not fields:
        raise HTTPException(status_code=400, detail="nothing to update")
    if "text" in fields and not (patch.text or "").strip():
        raise HTTPException(status_code=400, detail="note text cannot be empty")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT topic_id FROM notes WHERE id = %s", (note_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"note {note_id} not found")

            if "text" in fields:
                cur.execute("UPDATE notes SET text = %s WHERE id = %s", (patch.text.strip(), note_id))
            if "revisit" in fields:
                cur.execute("UPDATE notes SET revisit = %s WHERE id = %s", (bool(patch.revisit), note_id))

    return {"note_id": note_id, "topic_id": row[0]}


@app.delete("/notes/{note_id}")
def delete_note(note_id: int):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM notes WHERE id = %s RETURNING topic_id", (note_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"note {note_id} not found")

    return {"deleted": note_id, "topic_id": row[0]}


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
                summary = (patch.summary or "").strip()
                # An emptied summary means none: back to 'ai', so generating can fill it again.
                cur.execute(
                    "UPDATE topics SET summary = %s, summary_origin = %s WHERE id = %s",
                    (summary or None, "manual" if summary else "ai", topic_id),
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

    history = body.history[-MAX_HISTORY_TURNS:]
    # A follow-up such as "and why is that?" says little on its own; together
    # with the previous question the search knows what it is about.
    search_text = f"{history[-1].question} {question}" if history else question
    chunks = find_chunks(subject_id, search_text, body.limit, body.topic_id)

    if not chunks:
        raise HTTPException(status_code=404, detail="no material with embeddings for this subject")

    system_prompt = ASK_SYSTEM_PROMPT if body.explain else ASK_STRICT_PROMPT

    # The thread so far as a normal chat, then the new question with fresh lecture fragments.
    messages = [{"role": "system", "content": system_prompt}]
    for turn in history:
        messages.append({"role": "user", "content": turn.question})
        messages.append({"role": "assistant", "content": turn.answer[:MAX_HISTORY_ANSWER_CHARS]})
    messages.append({"role": "user", "content": build_ask_prompt(question, chunks)})

    try:
        response = groq_client.chat.completions.create(
            model=ASK_MODEL,
            messages=messages,
            temperature=0.2,
            # More thinking before answering: ~14 s instead of ~5 s, but the
            # worked examples stopped containing arithmetic mistakes in tests.
            reasoning_effort="high",
            max_completion_tokens=ASK_MAX_TOKENS,
        )
    except RateLimitError as error:
        # A readable message instead of a 500 the page cannot show.
        retry_after = error.response.headers.get("retry-after")
        wait = f", try again in {int(float(retry_after)) + 1}s" if retry_after else ", try again in a minute"
        raise HTTPException(
            status_code=429,
            detail=f"Groq rate limit reached for the assistant{wait}",
            headers={"Retry-After": retry_after} if retry_after else None,
        )

    choice = response.choices[0]
    if not choice.message.content:
        # The whole budget went into reasoning before any answer was written.
        raise HTTPException(status_code=502, detail="the model ran out of space before answering, try again")

    return {
        "question": question,
        "topic_id": body.topic_id,
        "answer": choice.message.content,
        # "length": the answer hit the token limit and ends mid-sentence
        "truncated": choice.finish_reason == "length",
        "sources": [
            {"page": c["page"], "source_id": c["source_id"], "filename": c["filename"], "distance": c["distance"]}
            for c in chunks
        ],
        "tokens": f"{response.usage.prompt_tokens} / {response.usage.completion_tokens}",
    }
# ---- Exams: uploaded per subject, analysed into questions on request ----

class ExamKind(str, Enum):
    final = "final"
    short = "short"


EXAM_TYPES = {ExamKind.final: SourceType.past_exam, ExamKind.short: SourceType.short_exam}
EXAM_KINDS = {source_type.value: kind.value for kind, source_type in EXAM_TYPES.items()}


class QuestionTopicIn(BaseModel):
    topic_id: int


# Files kept as text and shown as they are: code with highlighting, Markdown rendered.
TEXT_EXTENSIONS = {
    ".py", ".java", ".kt", ".scala", ".c", ".h", ".cpp", ".hpp", ".cc", ".cs", ".go", ".rs",
    ".js", ".ts", ".rb", ".php", ".swift", ".hs", ".ml", ".r", ".m", ".jl", ".sql", ".sh",
    ".md", ".txt", ".tex", ".json", ".yaml", ".yml", ".csv",
}
MAX_TEXT_BYTES = 2_000_000


def read_text_upload(files: list[UploadFile]) -> tuple[str, str] | None:
    """(filename, text) for a single code or Markdown file; None for anything else."""
    if len(files) != 1 or Path(files[0].filename or "").suffix.lower() not in TEXT_EXTENSIONS:
        return None
    data = files[0].file.read()
    if len(data) > MAX_TEXT_BYTES:
        raise HTTPException(status_code=413, detail="this text file is larger than 2 MB")
    return files[0].filename, data.decode("utf-8", errors="replace")


def read_upload(files: list[UploadFile]) -> tuple[bytes, str]:
    """The uploaded PDF, or the photos (and PDFs) put together into one PDF, with its name."""
    try:
        return files_to_pdf([(f.filename, f.content_type, f.file.read()) for f in files])
    except ValueError as error:
        raise HTTPException(status_code=415, detail=str(error))


@app.post("/subjects/{subject_id}/exams")
def upload_exam(subject_id: int, kind: ExamKind = Form(...), file: list[UploadFile] = File(...)):
    data, filename = read_upload(file)
    content_hash = hashlib.sha256(data).hexdigest()

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM subjects WHERE id = %s", (subject_id,))
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail=f"subject {subject_id} not found")

            cur.execute(
                "SELECT id FROM sources WHERE subject_id = %s AND content_hash = %s",
                (subject_id, content_hash),
            )
            existing = cur.fetchone()
            if existing:
                raise HTTPException(status_code=409, detail=f"already uploaded as source {existing[0]}")

            # No block: an exam covers the whole subject. No chunks: analyse reads the page images.
            cur.execute(
                "INSERT INTO sources (subject_id, type, filename, content_hash, pdf) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (subject_id, EXAM_TYPES[kind].value, filename, content_hash, data),
            )
            source_id = cur.fetchone()[0]

    return {"source_id": source_id, "filename": filename, "kind": kind.value}


@app.get("/subjects/{subject_id}/exams")
def list_exams(subject_id: int):
    """Exam files of a subject with their analysis state, questions and linked topics."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, filename, type FROM sources "
                "WHERE subject_id = %s AND type = ANY(%s) ORDER BY id",
                (subject_id, list(EXAM_KINDS)),
            )
            exams = [
                {"source_id": r[0], "filename": r[1], "kind": EXAM_KINDS[r[2]],
                 "analysis": None, "questions": []}
                for r in cur.fetchall()
            ]
            add_questions(cur, exams)

    return exams


@app.get("/subjects/{subject_id}/homework")
def list_homework(subject_id: int):
    """Every block of the subject in order, with its homework files analysed like exams."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name FROM blocks WHERE subject_id = %s ORDER BY position, id",
                (subject_id,),
            )
            blocks = [{"block_id": r[0], "name": r[1], "files": []} for r in cur.fetchall()]
            by_block = {b["block_id"]: b for b in blocks}

            cur.execute(
                "SELECT id, filename, block_id FROM sources "
                "WHERE subject_id = %s AND type = 'homework' AND block_id IS NOT NULL ORDER BY id",
                (subject_id,),
            )
            files = []
            for source_id, filename, block_id in cur.fetchall():
                file = {"source_id": source_id, "filename": filename, "kind": "homework",
                        "analysis": None, "questions": []}
                by_block[block_id]["files"].append(file)
                files.append(file)
            add_questions(cur, files)

    return blocks


def add_questions(cur, files: list[dict]) -> None:
    """Fills in each analysed file's analysis state and its questions with their topics."""
    by_id = {f["source_id"]: f for f in files}
    ids = list(by_id)

    # The latest job of each file is its analysis state.
    cur.execute(
        "SELECT DISTINCT ON (source_id) source_id, id, stage, status, message "
        "FROM jobs WHERE source_id = ANY(%s) ORDER BY source_id, id DESC",
        (ids,),
    )
    for source_id, job_id, stage, status, message in cur.fetchall():
        by_id[source_id]["analysis"] = {
            "job_id": job_id, "stage": stage, "status": status, "message": message,
        }

    cur.execute(
        "SELECT id, source_id, number, text, page FROM exam_questions "
        "WHERE source_id = ANY(%s) ORDER BY source_id, position",
        (ids,),
    )
    questions = {}
    for question_id, source_id, number, text, page in cur.fetchall():
        questions[question_id] = q = {
            "question_id": question_id, "number": number, "text": text,
            "page": page, "topics": [],
        }
        by_id[source_id]["questions"].append(q)

    # Each linked topic with the first block it appears in, for its panel and label.
    cur.execute(
        "SELECT qt.question_id, t.id, t.title, qt.origin, first_block.id, first_block.name "
        "FROM exam_question_topics qt JOIN topics t ON t.id = qt.topic_id "
        "LEFT JOIN LATERAL ("
        "  SELECT b.id, b.name FROM topic_chunks tc "
        "  JOIN chunks c ON c.id = tc.chunk_id JOIN sources s ON s.id = c.source_id "
        "  JOIN blocks b ON b.id = s.block_id "
        "  WHERE tc.topic_id = t.id ORDER BY b.position, b.id LIMIT 1"
        ") first_block ON true "
        "WHERE qt.question_id = ANY(%s) ORDER BY qt.question_id, t.title",
        (list(questions),),
    )
    for question_id, topic_id, title, origin, block_id, block_name in cur.fetchall():
        questions[question_id]["topics"].append({
            "topic_id": topic_id, "title": title, "origin": origin,
            "block_id": block_id, "block_name": block_name,
        })



@app.post("/sources/{source_id}/analyse")
def start_exam_analysis(source_id: int, background_tasks: BackgroundTasks):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT type FROM sources WHERE id = %s", (source_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"source {source_id} not found")
            if row[0] not in EXAM_KINDS and row[0] != SourceType.homework.value:
                raise HTTPException(status_code=400, detail="only exams and homework can be analysed")

            cur.execute("SELECT 1 FROM jobs WHERE source_id = %s AND status = 'running'", (source_id,))
            if cur.fetchone():
                raise HTTPException(status_code=409, detail="this file is already being analysed")

            cur.execute("INSERT INTO jobs (source_id) VALUES (%s) RETURNING id", (source_id,))
            job_id = cur.fetchone()[0]

    background_tasks.add_task(run_exam_analysis, job_id, source_id)
    return {"job_id": job_id}


@app.post("/exam-questions/{question_id}/topics")
def link_question_topic(question_id: int, body: QuestionTopicIn):
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Topic and exam must belong to the same subject.
            cur.execute(
                "SELECT 1 FROM exam_questions q JOIN sources s ON s.id = q.source_id "
                "JOIN topics t ON t.subject_id = s.subject_id "
                "WHERE q.id = %s AND t.id = %s",
                (question_id, body.topic_id),
            )
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="question or topic not found in this subject")

            cur.execute(
                "INSERT INTO exam_question_topics (question_id, topic_id, origin) "
                "VALUES (%s, %s, 'manual') ON CONFLICT DO NOTHING",
                (question_id, body.topic_id),
            )
    return {"question_id": question_id, "topic_id": body.topic_id}


@app.delete("/exam-questions/{question_id}/topics/{topic_id}")
def unlink_question_topic(question_id: int, topic_id: int):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM exam_question_topics WHERE question_id = %s AND topic_id = %s RETURNING 1",
                (question_id, topic_id),
            )
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="this topic is not linked to the question")
    return {"question_id": question_id, "topic_id": topic_id}


# How much each signal raises a topic in Focus. Kept simple and visible on
# purpose: the page shows the reasons, so the order can be checked and tuned.
PRIORITY_WEIGHTS = {
    "finals": 3.0,      # per final exam question on the topic
    "tests": 2.0,       # per short test question on the topic
    "unclear": 2.0,     # per open unclear place
    "review": 2.0,      # status "review"
    "homework": 1.0,    # per homework task on the topic
    "revisit": 1.0,     # per note marked "come back to this": understood, but needs another look
    "weight": 1.0,      # share of the lecture, scaled to 0..1 within the subject
}


@app.get("/subjects/{subject_id}/focus")
def subject_focus(subject_id: int):
    """Topics grouped by block in block order, each block sorted by priority.

    A topic that appears in several blocks is listed once, in the first one."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT t.id, t.title, t.status, b.id, b.name, b.position, "
                "count(DISTINCT tc.chunk_id), min(c.page_number), "
                "(SELECT count(*) FROM struggles st WHERE st.topic_id = t.id "
                " AND st.event_type = 'unclear' AND st.resolved_at IS NULL), "
                "(SELECT count(*) FROM exam_question_topics qt JOIN exam_questions q ON q.id = qt.question_id "
                " JOIN sources es ON es.id = q.source_id WHERE qt.topic_id = t.id AND es.type = 'past_exam'), "
                "(SELECT count(*) FROM exam_question_topics qt JOIN exam_questions q ON q.id = qt.question_id "
                " JOIN sources es ON es.id = q.source_id WHERE qt.topic_id = t.id AND es.type = 'short_exam'), "
                "(SELECT count(*) FROM exam_question_topics qt JOIN exam_questions q ON q.id = qt.question_id "
                " JOIN sources es ON es.id = q.source_id WHERE qt.topic_id = t.id AND es.type = 'homework'), "
                "(SELECT count(*) FROM notes n WHERE n.topic_id = t.id AND n.revisit) "
                "FROM topics t "
                "JOIN topic_chunks tc ON tc.topic_id = t.id "
                "JOIN chunks c ON c.id = tc.chunk_id "
                "JOIN sources s ON s.id = c.source_id "
                "JOIN blocks b ON b.id = s.block_id "
                "WHERE t.subject_id = %s "
                "GROUP BY t.id, b.id",
                (subject_id,),
            )
            rows = cur.fetchall()

    # One row per (topic, block): keep the earliest block, add up the weight.
    topics = {}
    for (topic_id, title, status, block_id, block_name, position,
         weight, first_page, unclear, finals, tests, homework, revisit) in rows:
        topic = topics.get(topic_id)
        if topic is None:
            topics[topic_id] = topic = {
                "id": topic_id, "title": title, "status": status, "weight": 0,
                "unclear": unclear, "finals": finals, "tests": tests, "homework": homework,
                "revisit": revisit,
                "block": (position, block_id, block_name), "first_page": first_page,
            }
        topic["weight"] += weight
        if (position, block_id) < topic["block"][:2]:
            topic["block"] = (position, block_id, block_name)
            topic["first_page"] = first_page

    max_weight = max((t["weight"] for t in topics.values()), default=1)
    for t in topics.values():
        t["priority"] = round(
            PRIORITY_WEIGHTS["finals"] * t["finals"]
            + PRIORITY_WEIGHTS["tests"] * t["tests"]
            + PRIORITY_WEIGHTS["unclear"] * t["unclear"]
            + PRIORITY_WEIGHTS["review"] * (t["status"] == "review")
            + PRIORITY_WEIGHTS["homework"] * t["homework"]
            + PRIORITY_WEIGHTS["revisit"] * t["revisit"]
            + PRIORITY_WEIGHTS["weight"] * t["weight"] / max_weight,
            2,
        )

    blocks = {}
    for t in topics.values():
        position, block_id, block_name = t.pop("block")
        block = blocks.setdefault(block_id, {
            "block_id": block_id, "name": block_name, "position": position,
            "study": [], "known": [],
        })
        block["known" if t["status"] == "known" else "study"].append(t)

    for block in blocks.values():
        block["study"].sort(key=lambda t: (-t["priority"], t["first_page"] or 0))
        block["known"].sort(key=lambda t: t["first_page"] or 0)

    return sorted(blocks.values(), key=lambda b: (b["position"], b["block_id"]))


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
                "t.summary IS NOT NULL AS has_summary, "
                "(SELECT count(*) FROM struggles st WHERE st.topic_id = t.id "
                " AND st.event_type = 'unclear' AND st.resolved_at IS NULL) AS unclear, "
                "array_agg(DISTINCT c.page_number ORDER BY c.page_number) AS pages "
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
                    "unclear": r[7],
                    "pages": r[8],
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
            for table in TOPIC_CHILD_TABLES:
                cur.execute(f"DELETE FROM {table} WHERE topic_id = %s", (topic_id,))
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


def remove_source(cur, source_id: int) -> int:
    """Deletes a file with its chunks and the topics only it had; returns how many topics went."""
    # Collected before the links go, while it is still known where topics came from.
    topic_ids = topics_only_from(cur, source_id)

    cur.execute(
        "DELETE FROM topic_chunks WHERE chunk_id IN "
        "(SELECT id FROM chunks WHERE source_id = %s)",
        (source_id,),
    )
    if topic_ids:
        cur.execute("UPDATE topics SET parent_id = NULL WHERE parent_id = ANY(%s)", (topic_ids,))
        for table in TOPIC_CHILD_TABLES:
            cur.execute(f"DELETE FROM {table} WHERE topic_id = ANY(%s)", (topic_ids,))
        cur.execute("DELETE FROM topics WHERE id = ANY(%s)", (topic_ids,))

    # An exam file: its questions' topic links go before the questions.
    cur.execute(
        "DELETE FROM exam_question_topics WHERE question_id IN "
        "(SELECT id FROM exam_questions WHERE source_id = %s)",
        (source_id,),
    )
    for table in ("techniques", "exam_questions", "chunks", "jobs"):
        cur.execute(f"DELETE FROM {table} WHERE source_id = %s", (source_id,))
    cur.execute("DELETE FROM sources WHERE id = %s", (source_id,))
    return len(topic_ids)


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

            topics_removed = remove_source(cur, source_id)

    return {"deleted": source_id, "filename": row[0], "topics_removed": topics_removed}


@app.patch("/blocks/{block_id}")
def rename_block(block_id: int, body: BlockIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name cannot be empty")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE blocks SET name = %s WHERE id = %s RETURNING id", (name, block_id))
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail=f"block {block_id} not found")

    return {"block_id": block_id, "name": name}


# Topics only this block's files have: they go with the block.
BLOCK_ONLY_TOPICS = (
    "SELECT DISTINCT tc.topic_id FROM topic_chunks tc "
    "JOIN chunks c ON c.id = tc.chunk_id JOIN sources s ON s.id = c.source_id "
    "WHERE s.block_id = %s AND NOT EXISTS ("
    "  SELECT 1 FROM topic_chunks tc2 JOIN chunks c2 ON c2.id = tc2.chunk_id "
    "  JOIN sources s2 ON s2.id = c2.source_id "
    "  WHERE tc2.topic_id = tc.topic_id AND s2.block_id IS DISTINCT FROM %s)"
)


@app.get("/blocks/{block_id}/delete-preview")
def preview_block_deletion(block_id: int):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM blocks WHERE id = %s", (block_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"block {block_id} not found")

            cur.execute("SELECT count(*) FROM sources WHERE block_id = %s", (block_id,))
            files = cur.fetchone()[0]
            cur.execute(f"SELECT count(*) FROM ({BLOCK_ONLY_TOPICS}) t", (block_id, block_id))
            topics = cur.fetchone()[0]
            cur.execute(
                f"SELECT count(*) FROM notes WHERE topic_id IN ({BLOCK_ONLY_TOPICS})",
                (block_id, block_id),
            )
            notes = cur.fetchone()[0]

    return {"block_id": block_id, "name": row[0], "files": files, "topics": topics, "notes": notes}


@app.delete("/blocks/{block_id}")
def delete_block(block_id: int):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM blocks WHERE id = %s", (block_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"block {block_id} not found")

            cur.execute(
                "SELECT 1 FROM jobs j JOIN sources s ON s.id = j.source_id "
                "WHERE s.block_id = %s AND j.status = 'running'",
                (block_id,),
            )
            if cur.fetchone():
                raise HTTPException(
                    status_code=409,
                    detail="a file of this block is still being processed, delete it when that finishes",
                )

            # File by file, the same way a single file is deleted.
            cur.execute("SELECT id FROM sources WHERE block_id = %s", (block_id,))
            source_ids = [r[0] for r in cur.fetchall()]
            topics_removed = sum(remove_source(cur, source_id) for source_id in source_ids)
            cur.execute("DELETE FROM blocks WHERE id = %s", (block_id,))

    return {"deleted": block_id, "name": row[0], "topics_removed": topics_removed}


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
    file: list[UploadFile] = File(...),
):
    text_file = read_text_upload(file)
    data, filename = (text_file[1].encode(), text_file[0]) if text_file else read_upload(file)
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

            # Code or Markdown: kept to be shown as it is, no pages and no topics.
            if text_file:
                cur.execute(
                    "INSERT INTO sources (subject_id, block_id, type, filename, content_hash, text_content) "
                    "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                    (subject_id, block_id, source_type.value, filename, content_hash, text_file[1]),
                )
                return {"job_id": None, "source_id": cur.fetchone()[0], "filename": filename,
                        "chunks": 0, "weak_chunks": 0}

            chunks = parse_pdf(data)

            cur.execute(
                "INSERT INTO sources (subject_id, block_id, type, filename, content_hash, pdf) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (subject_id, block_id, source_type.value, filename, content_hash, data),
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
        # Fix formulas and describe pictures now, so a later analyse reads clean text.
        # Own solutions are never analysed by default, so they cost no vision tokens.
        background_tasks.add_task(enqueue_vision_pages, source_id)

    return {
        "job_id": job_id,
        "source_id": source_id,
        "filename": filename,
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

    # filename*= carries any characters; a plain filename="..." header only
    # takes Latin-1, and a name like "Prüfung" or "Экзамен" would crash it.
    return Response(
        content=bytes(row[1]),
        media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(row[0])}"},
    )


class SourcePatch(BaseModel):
    filename: str


@app.patch("/sources/{source_id}")
def rename_source(source_id: int, patch: SourcePatch):
    """Only the name shown in the app changes; the stored file stays as it is.
    The extension is kept (".pdf", or ".py" for code) when the new name has none."""
    name = patch.filename.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name cannot be empty")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT filename FROM sources WHERE id = %s", (source_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"source {source_id} not found")
            extension = Path(row[0] or "").suffix or ".pdf"
            if not name.lower().endswith(extension.lower()):
                name += extension
            cur.execute("UPDATE sources SET filename = %s WHERE id = %s", (name, source_id))

    return {"source_id": source_id, "filename": name}


@app.get("/blocks/{block_id}/files")
def list_block_files(block_id: int):
    with get_connection() as conn:
        with conn.cursor() as cur:
            # With the file's latest job, so the page can keep showing the
            # progress of a lecture that is still processing after a reload.
            cur.execute(
                "SELECT s.id, s.filename, s.type, (s.pdf IS NOT NULL), j.id, j.stage, j.status, "
                "(s.text_content IS NOT NULL) "
                "FROM sources s LEFT JOIN LATERAL ("
                "  SELECT id, stage, status FROM jobs WHERE source_id = s.id ORDER BY id DESC LIMIT 1"
                ") j ON true "
                "WHERE s.block_id = %s ORDER BY s.id",
                (block_id,),
            )
            return [
                {"source_id": r[0], "filename": r[1], "type": r[2], "has_pdf": r[3], "has_text": r[7],
                 "job": {"job_id": r[4], "stage": r[5], "status": r[6]} if r[4] else None}
                for r in cur.fetchall()
            ]


@app.get("/sources/{source_id}/text")
def get_source_text(source_id: int):
    """A code or Markdown file, for the viewer."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT filename, text_content FROM sources WHERE id = %s", (source_id,))
            row = cur.fetchone()
    if row is None or row[1] is None:
        raise HTTPException(status_code=404, detail="no text file stored for this source")
    return {"source_id": source_id, "filename": row[0], "text": row[1]}


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