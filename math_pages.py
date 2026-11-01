"""Re-reads slides from the page image, in the background: formulas and pictures.

PDF text extraction breaks math: ∀ comes out as ’, ε as Á, fractions lose
their bar, ≠ loses its stroke. And it does not see pictures at all: a tree or
a graph drawn on a slide leaves at most its labels. On upload a lecture is read
from the text layer as before; pages that use math fonts and weak pages (mostly
a picture, see pdf_parser) are only marked as pending. A single worker thread
then sends those pages as images to a vision model and replaces their text with
a Markdown + LaTeX transcription that also describes the pictures, as fast as
the Groq limits allow. The queue lives in the chunks table, so it survives restarts.
"""

import base64
import os
import re
import threading
import time

import pymupdf
from dotenv import load_dotenv
from groq import APIConnectionError, APIError, Groq, InternalServerError, RateLimitError

from db import get_connection
from embedder import embed_source

load_dotenv()
client = Groq(api_key=os.getenv("GROQ_API_KEY"))

VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.8-27b")

# Math fonts of LaTeX (Computer Modern, Latin Modern, AMS, newtx) and PowerPoint.
MATH_FONT = re.compile(r"CMMI|CMSY|CMEX|LMMath|MSAM|MSBM|NewTXMI|txsy|txmia|txex|CambriaMath|Math", re.I)
MIN_MATH_CHARS = 5
RENDER_DPI = 150
IDLE_SECONDS = 30
# Overloaded server, dropped connection or an empty answer: worth trying again.
MAX_TRANSIENT_RETRIES = 5

PROMPT = r"""Transcribe this lecture slide into Markdown.
- Copy the text exactly, do not summarize or explain
- Write every formula and every math symbol as LaTeX: inline \( ... \), display $$ ... $$
- Use real subscripts, superscripts, fractions: \(x_0\), \(\mathbb{R}^d\), \(\frac{f}{g}\)
- Keep headings as Markdown headings and lists as lists
- For every diagram, tree, graph, chart, table drawn as a picture or photo, add
  a line "[figure: ...]" that says what it shows so it can be understood without
  seeing it: the kind of figure, its structure (e.g. which node is the child of
  which, which vertices are connected, the axes and the curve's shape), labels and values
- Skip the footer (course name, chapter, page number) and logos
- Output only the transcription"""


def math_char_count(page: pymupdf.Page) -> int:
    count = 0
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                if MATH_FONT.search(span["font"]):
                    count += len(span["text"].strip())
    return count


def enqueue_vision_pages(source_id: int) -> int:
    """Marks the pages of a source that use math fonts or are mostly a picture
    as pending. Returns how many."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pdf FROM sources WHERE id = %s", (source_id,))
            row = cur.fetchone()
            if row is None or row[0] is None:
                return 0

            cur.execute(
                "SELECT id, page_number, is_weak FROM chunks "
                "WHERE source_id = %s AND formula_status IS NULL",
                (source_id,),
            )
            chunks = cur.fetchall()

            doc = pymupdf.open(stream=row[0], filetype="pdf")
            pending = [
                chunk_id for chunk_id, page_number, is_weak in chunks
                if is_weak or math_char_count(doc[page_number - 1]) >= MIN_MATH_CHARS
            ]
            doc.close()

            cur.execute(
                "UPDATE chunks SET formula_status = 'pending' WHERE id = ANY(%s)",
                (pending,),
            )
    return len(pending)


def clean_transcription(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    text = re.sub(r"^```(?:markdown)?\s*\n(.*)\n```$", r"\1", text, flags=re.S).strip()

    # The page renders \( \) and $$ $$; the model sometimes still writes $x$ inline.
    blocks = []
    text = re.sub(r"\$\$.+?\$\$", lambda m: blocks.append(m.group(0)) or f"\x00{len(blocks) - 1}\x00", text, flags=re.S)
    text = re.sub(r"(?<!\\)\$(.+?)(?<!\\)\$", r"\\(\1\\)", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: blocks[int(m.group(1))], text)


def page_image_url(page: pymupdf.Page) -> str:
    png = page.get_pixmap(dpi=RENDER_DPI).tobytes("png")
    return "data:image/png;base64," + base64.b64encode(png).decode()


def ask_vision(prompt: str, image_url: str, label: str = "vision") -> str | None:
    """One prompt + one page image to the vision model."""
    content = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": image_url}},
    ]
    return ask_model(VISION_MODEL, content, label)


def ask_model(model: str, content, label: str, max_tokens: int = 4096) -> str | None:
    """One user message (text, or text + image) to a Groq model. Waits out rate
    limits as long as needed and retries temporary errors a few times; returns
    None if there is still no answer. Shared by formula pages and exam analysis."""
    transient = 0

    while True:
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": content}],
                temperature=0,
                max_completion_tokens=max_tokens,
            )
        except RateLimitError as error:
            # The daily token limit refills slowly, so this can be 15 minutes or more.
            wait = float(error.response.headers.get("retry-after") or 60)
            print(f"{label}: rate limit, waiting {wait:.0f}s")
            time.sleep(wait)
            continue
        except (InternalServerError, APIConnectionError) as error:
            problem = error
        except APIError as error:
            print(f"{label}: vision model failed: {error}")
            return None
        else:
            text = re.sub(r"<think>.*?</think>", "", response.choices[0].message.content or "", flags=re.S).strip()
            if text:
                return text
            problem = "empty answer"

        transient += 1
        if transient > MAX_TRANSIENT_RETRIES:
            print(f"{label}: giving up after {MAX_TRANSIENT_RETRIES} retries: {problem}")
            return None
        print(f"{label}: {problem}, retry {transient} in {30 * transient}s")
        time.sleep(30 * transient)


def transcribe_page(page: pymupdf.Page) -> str | None:
    text = ask_vision(PROMPT, page_image_url(page), label="slides")
    # "or None": a transcription that is empty after cleaning must not replace the text
    return (clean_transcription(text) or None) if text else None


def next_pending() -> tuple | None:
    # Newest lecture first: it is the one being studied now.
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.id, c.source_id, c.page_number FROM chunks c "
                "WHERE c.formula_status = 'pending' "
                "ORDER BY c.source_id DESC, c.page_number LIMIT 1"
            )
            return cur.fetchone()


def process_page(chunk_id: int, source_id: int, page_number: int) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pdf FROM sources WHERE id = %s", (source_id,))
            pdf = cur.fetchone()[0]

    doc = pymupdf.open(stream=pdf, filetype="pdf")
    text = transcribe_page(doc[page_number - 1])
    doc.close()

    if text is None:
        mark_failed(chunk_id)
        return

    with get_connection() as conn:
        with conn.cursor() as cur:
            # pdf_text keeps the original once; dropping the vector makes embed_source redo it.
            # Read from the image, a weak page is a full page now.
            cur.execute(
                "UPDATE chunks SET pdf_text = COALESCE(pdf_text, text), text = %s, "
                "embedding = NULL, formula_status = 'done', is_weak = false WHERE id = %s",
                (text, chunk_id),
            )
            link_to_nearest_topics(cur, chunk_id, source_id, page_number)

    embed_source(source_id)
    print(f"slides: source {source_id} page {page_number} done")


def link_to_nearest_topics(cur, chunk_id: int, source_id: int, page_number: int) -> None:
    """A weak page was skipped when the lecture's topics were found, so no topic
    has it. It gets the topics of the nearest page that has some (the earlier
    one on a tie): a picture shows what the slides around it are about."""
    cur.execute("SELECT 1 FROM topic_chunks WHERE chunk_id = %s", (chunk_id,))
    if cur.fetchone():
        return
    cur.execute(
        "INSERT INTO topic_chunks (topic_id, chunk_id) "
        "SELECT DISTINCT tc.topic_id, %s FROM topic_chunks tc JOIN chunks c ON c.id = tc.chunk_id "
        "WHERE c.source_id = %s AND c.page_number = ("
        "  SELECT c2.page_number FROM chunks c2 JOIN topic_chunks tc2 ON tc2.chunk_id = c2.id "
        "  WHERE c2.source_id = %s AND c2.id <> %s "
        "  ORDER BY abs(c2.page_number - %s), c2.page_number LIMIT 1"
        ") ON CONFLICT DO NOTHING",
        (chunk_id, source_id, source_id, chunk_id, page_number),
    )


def mark_failed(chunk_id: int) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE chunks SET formula_status = 'failed' WHERE id = %s", (chunk_id,))


def worker() -> None:
    while True:
        job = None
        try:
            job = next_pending()
            if job is None:
                time.sleep(IDLE_SECONDS)
                continue
            process_page(*job)
        except Exception as error:
            # Mark the page so one broken page does not block the whole queue.
            print(f"slides: worker error: {error}")
            if job is not None:
                mark_failed(job[0])
            time.sleep(IDLE_SECONDS)


def start_worker() -> None:
    threading.Thread(target=worker, name="formula-worker", daemon=True).start()
