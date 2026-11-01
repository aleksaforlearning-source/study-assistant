import threading
import traceback
from collections import defaultdict

from db import get_connection
from topic_extractor import extract_topics
from build_hierarchy import build_hierarchy
from embedder import embed_source
from math_pages import enqueue_vision_pages


def set_stage(job_id: int, stage: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET stage = %s, updated_at = now() WHERE id = %s",
                (stage, job_id),
            )


def fail_interrupted_jobs() -> int:
    """At server start: a job still marked running was cut off by the restart
    (background tasks live in the server process), so it will never finish."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET status = 'failed', "
                "message = 'interrupted by a server restart, start it again', "
                "updated_at = now() WHERE status = 'running'"
            )
            return cur.rowcount


def finish(job_id: int, status: str, message: str | None = None) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET status = %s, message = %s, updated_at = now() WHERE id = %s",
                (status, message, job_id),
            )

# One lecture per subject at a time. Topic extraction reads the subject's
# existing topics once at its start; two lectures processed side by side would
# not see each other's new topics and could both create the same one. Lectures
# of different subjects still run in parallel.
_subject_locks = defaultdict(threading.Lock)
_locks_guard = threading.Lock()


def subject_lock(subject_id: int) -> threading.Lock:
    with _locks_guard:
        return _subject_locks[subject_id]


def run_pipeline(job_id: int, source_id: int, subject_id: int, block_id: int) -> None:
    lock = subject_lock(subject_id)
    if lock.locked():
        set_stage(job_id, "waiting for another upload")
    with lock:
        _run_pipeline(job_id, source_id, subject_id, block_id)


def _run_pipeline(job_id: int, source_id: int, subject_id: int, block_id: int) -> None:
    try:
        set_stage(job_id, "topics")
        extract_topics(source_id)

        set_stage(job_id, "embeddings")
        embed_source(source_id)

        set_stage(job_id, "hierarchy")
        build_hierarchy(subject_id, block_id)

        # Only marks pages; the worker re-reads formulas and pictures in the background.
        set_stage(job_id, "slides")
        enqueue_vision_pages(source_id)

        finish(job_id, "done")
    except Exception as error:
        print(traceback.format_exc())
        finish(job_id, "failed", f"{type(error).__name__}: {error}")
