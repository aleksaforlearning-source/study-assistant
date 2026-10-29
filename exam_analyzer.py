"""Splits an exam PDF into questions and links each to the subject's topics.

Each page is read the cheapest way that still gets it right: pages with math
fonts, and scans with no text layer, go as an image to the vision model (the
text layer breaks formulas); plain text pages go as text to the text model,
which also has its own, separate rate limit. Either way the model gets the
subject's topic list and returns the questions on the page with the ids of the
topics they test.

The answer format is plain marked-up text, not JSON: in JSON a backslash starts
an escape, so LaTeX like \frac or \nabla would silently turn into a form feed or
a newline.
"""

import os
import re
import traceback

import pymupdf

from db import get_connection
from math_pages import MIN_MATH_CHARS, ask_model, ask_vision, clean_transcription, math_char_count, page_image_url
from pipeline import finish, set_stage

TEXT_MODEL = os.getenv("GROQ_TEXT_MODEL", "openai/gpt-oss-20b")
MAX_TOPICS_PER_QUESTION = 3
# Fewer characters than this in the text layer means a scan or a picture: read the image.
MIN_TEXT_CHARS = 80

PROMPT = r"""This is page {page} of {pages} of an exam in the course "{subject}"{how}.

Find every exam question or task on this page and write each one like this:

=== QUESTION 2b
topics: 12, 15
continues: no
The full question as Markdown, copied exactly. Every formula as LaTeX:
inline \( ... \), display $$ ... $$.

Rules:
- After "=== QUESTION" write the number as printed ("1", "2b"), or nothing if there is none
- "topics": ids from the course topic list below that the question really tests,
  most specific first, at most 3. Write "topics: none" if nothing fits
- "continues: yes" only for the rest of a question that started on the previous page
- Skip the cover page, instructions, point tables and empty answer space
- If the page has no question at all, answer only: NO QUESTIONS

Course topics (id | title | block of the course where it is taught):
{topics}
{page_text}"""

QUESTION_HEADER = re.compile(r"^=== QUESTION[ \t]*(.*)$", re.M)
META_LINE = re.compile(r"^(topics|continues)\s*:", re.I)


def subject_topics(subject_id: int) -> list[tuple]:
    """(id, title, block name) for every topic, in the first block it appears in."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT t.id, t.title, (array_agg(b.name ORDER BY b.position, b.id))[1] "
                "FROM topics t "
                "JOIN topic_chunks tc ON tc.topic_id = t.id "
                "JOIN chunks c ON c.id = tc.chunk_id "
                "JOIN sources s ON s.id = c.source_id "
                "JOIN blocks b ON b.id = s.block_id "
                "WHERE t.subject_id = %s "
                "GROUP BY t.id ORDER BY min(b.position), t.id",
                (subject_id,),
            )
            return cur.fetchall()


def parse_page(answer: str) -> list[dict]:
    # split() with a capturing group gives [before, number1, body1, number2, body2, ...]
    parts = QUESTION_HEADER.split(answer)
    questions = []
    for number, body in zip(parts[1::2], parts[2::2]):
        lines = body.strip("\n").split("\n")
        meta = {}
        while lines and META_LINE.match(lines[0]):
            key, _, value = lines.pop(0).partition(":")
            meta[key.strip().lower()] = value.strip()

        text = clean_transcription("\n".join(lines).strip())
        if not text:
            continue
        questions.append({
            "number": number.strip() or None,
            "text": text,
            "continues": meta.get("continues", "no").lower().startswith("y"),
            "topic_ids": [int(x) for x in re.findall(r"\d+", meta.get("topics", ""))],
        })
    return questions


def analyse_exam(source_id: int, on_progress=None) -> dict:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT s.pdf, s.subject_id, sub.name FROM sources s "
                "JOIN subjects sub ON sub.id = s.subject_id WHERE s.id = %s",
                (source_id,),
            )
            row = cur.fetchone()
    if row is None or row[0] is None:
        raise ValueError("no PDF stored for this exam")
    pdf, subject_id, subject_name = row

    topics = subject_topics(subject_id)
    if not topics:
        raise ValueError("this subject has no topics yet, upload its lectures first")
    known_ids = {t[0] for t in topics}
    topic_lines = "\n".join(f"{tid} | {title} | {block}" for tid, title, block in topics)

    doc = pymupdf.open(stream=pdf, filetype="pdf")
    questions, unreadable = [], []
    read_as = {"text": 0, "image": 0}
    for index in range(len(doc)):
        if on_progress:
            on_progress(index, len(doc))
        page = doc[index]
        text = page.get_text("text").strip()
        details = dict(page=index + 1, pages=len(doc), subject=subject_name, topics=topic_lines)

        if math_char_count(page) < MIN_MATH_CHARS and len(text) >= MIN_TEXT_CHARS:
            read_as["text"] += 1
            prompt = PROMPT.format(**details, how=", given as its extracted text",
                                   page_text=f"\nPage text:\n{text}\n")
            # Generous budget: this model reasons before answering.
            answer = ask_model(TEXT_MODEL, prompt, label=f"exam {source_id}", max_tokens=8192)
        else:
            read_as["image"] += 1
            prompt = PROMPT.format(**details, how="", page_text="")
            answer = ask_vision(prompt, page_image_url(page), label=f"exam {source_id}")

        if answer is None:
            unreadable.append(index + 1)
            continue

        for q in parse_page(answer):
            # Only ids that exist in this subject; the model can invent numbers.
            q["topic_ids"] = [t for t in dict.fromkeys(q["topic_ids"]) if t in known_ids]
            if q["continues"] and questions:
                previous = questions[-1]
                previous["text"] += "\n\n" + q["text"]
                previous["topic_ids"] = list(dict.fromkeys(previous["topic_ids"] + q["topic_ids"]))
            else:
                q["page"] = index + 1
                questions.append(q)
    doc.close()

    if unreadable and not questions:
        raise RuntimeError(f"could not read pages {unreadable}")

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Links fixed by hand survive a new analysis: they are put back on the
            # new question with the same number. Unnumbered questions cannot be matched.
            cur.execute(
                "SELECT q.number, qt.topic_id FROM exam_question_topics qt "
                "JOIN exam_questions q ON q.id = qt.question_id "
                "WHERE q.source_id = %s AND qt.origin = 'manual' AND q.number IS NOT NULL",
                (source_id,),
            )
            manual = {}
            for number, topic_id in cur.fetchall():
                manual.setdefault(number, []).append(topic_id)

            # Everything else is replaced by the new analysis.
            cur.execute(
                "DELETE FROM exam_question_topics WHERE question_id IN "
                "(SELECT id FROM exam_questions WHERE source_id = %s)",
                (source_id,),
            )
            cur.execute("DELETE FROM exam_questions WHERE source_id = %s", (source_id,))
            for position, q in enumerate(questions):
                cur.execute(
                    "INSERT INTO exam_questions (source_id, number, text, page, position) "
                    "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                    (source_id, q["number"], q["text"], q["page"], position),
                )
                question_id = cur.fetchone()[0]
                for topic_id in q["topic_ids"][:MAX_TOPICS_PER_QUESTION]:
                    cur.execute(
                        "INSERT INTO exam_question_topics (question_id, topic_id) VALUES (%s, %s)",
                        (question_id, topic_id),
                    )
                for topic_id in manual.get(q["number"], []):
                    cur.execute(
                        "INSERT INTO exam_question_topics (question_id, topic_id, origin) "
                        "VALUES (%s, %s, 'manual') "
                        "ON CONFLICT (question_id, topic_id) DO UPDATE SET origin = 'manual'",
                        (question_id, topic_id),
                    )

    return {"questions": len(questions), "unreadable_pages": unreadable, "read_as": read_as}


def run_exam_analysis(job_id: int, source_id: int) -> None:
    """Background task: progress and the result go to the jobs row the page polls."""
    try:
        result = analyse_exam(
            source_id,
            on_progress=lambda done, total: set_stage(job_id, f"page {done + 1}/{total}"),
        )
        read_as = result["read_as"]
        message = f"{result['questions']} questions ({read_as['text']} pages as text, {read_as['image']} as images)"
        if result["unreadable_pages"]:
            message += f", pages {result['unreadable_pages']} could not be read"
        finish(job_id, "done", message)
    except Exception as error:
        print(traceback.format_exc())
        finish(job_id, "failed", f"{type(error).__name__}: {error}")
