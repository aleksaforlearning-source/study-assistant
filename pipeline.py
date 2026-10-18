import traceback

from db import get_connection
from topic_extractor import extract_topics
from build_hierarchy import build_hierarchy
from embedder import embed_source


def set_stage(job_id: int, stage: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET stage = %s, updated_at = now() WHERE id = %s",
                (stage, job_id),
            )


def finish(job_id: int, status: str, message: str | None = None) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET status = %s, message = %s, updated_at = now() WHERE id = %s",
                (status, message, job_id),
            )

def run_pipeline(job_id: int, source_id: int, subject_id: int, block_id: int) -> None:
    try:
        set_stage(job_id, "topics")
        extract_topics(source_id)

        set_stage(job_id, "embeddings")
        embed_source(source_id)

        set_stage(job_id, "hierarchy")
        build_hierarchy(subject_id, block_id)

        finish(job_id, "done")
    except Exception as error:
        print(traceback.format_exc())
        finish(job_id, "failed", f"{type(error).__name__}: {error}")
