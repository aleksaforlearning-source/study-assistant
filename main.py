from fastapi import FastAPI

from db import get_connection

app = FastAPI(title="Study Assistant")


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/db-check")
def db_check():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version();")
            version = cur.fetchone()[0]
    return {"database": version}