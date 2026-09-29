from fastapi import FastAPI

app = FastAPI(title="Study Assistant")


@app.get("/health")
def health_check():
    return {"status": "ok"}