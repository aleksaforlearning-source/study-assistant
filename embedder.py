import json
import sys

from fastembed import TextEmbedding

from db import get_connection

MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIMENSIONS = 384
BATCH_SIZE = 32

_model = None


def get_model() -> TextEmbedding:
    global _model
    if _model is None:
        print(f"загружаю {MODEL_NAME} (первый раз скачается ~130 MB)")
        _model = TextEmbedding(model_name=MODEL_NAME)
    return _model


def embed_texts(texts: list[str]) -> list[list[float]]:
    model = get_model()
    return [vector.tolist() for vector in model.embed(texts, batch_size=BATCH_SIZE)]


def embed_source(source_id: int) -> dict:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, text FROM chunks WHERE source_id = %s AND embedding IS NULL ORDER BY id",
                (source_id,),
            )
            rows = cur.fetchall()

            if not rows:
                return {"source_id": source_id, "embedded": 0, "status": "nothing_to_do"}

            print(f"source {source_id}: {len(rows)} чанков без вектора")

            chunk_ids = [r[0] for r in rows]
            texts = [r[1] for r in rows]

            vectors = embed_texts(texts)

            for chunk_id, vector in zip(chunk_ids, vectors):
                if len(vector) != DIMENSIONS:
                    raise ValueError(
                        f"модель вернула {len(vector)} чисел, колонка ждёт {DIMENSIONS}"
                    )
                cur.execute(
                    "UPDATE chunks SET embedding = %s::vector WHERE id = %s",
                    (str(vector), chunk_id),
                )
                print(f"  chunk {chunk_id}")

            conn.commit()

            cur.execute(
                "SELECT count(*) FILTER (WHERE embedding IS NOT NULL), count(*) "
                "FROM chunks WHERE source_id = %s",
                (source_id,),
            )
            with_vector, total = cur.fetchone()

    return {
        "source_id": source_id,
        "embedded": len(rows),
        "with_vector": with_vector,
        "total": total,
        "status": "done",
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("использование: py embedder.py <source_id>")
        sys.exit(1)

    stats = embed_source(int(sys.argv[1]))
    print()
    print(json.dumps(stats, indent=2, ensure_ascii=False))