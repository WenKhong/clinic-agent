from fastapi import FastAPI

app = FastAPI(title="Clinic Agent")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}