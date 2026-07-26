import os
import threading
from typing import List

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer


MODEL_NAME = os.getenv("MODEL_NAME", "nomic-ai/nomic-embed-text-v1.5")
MODEL_CACHE = os.getenv("MODEL_CACHE", "/models")
MODEL_MAX_CONCURRENCY = max(1, int(os.getenv("MODEL_MAX_CONCURRENCY", "1")))

hf_token = os.getenv("HF_TOKEN")
if hf_token:
    os.environ["HUGGINGFACE_HUB_TOKEN"] = hf_token

app = FastAPI(title="RAG Bot Query Embedder")
model: SentenceTransformer | None = None
model_semaphore = threading.BoundedSemaphore(MODEL_MAX_CONCURRENCY)


class EmbedRequest(BaseModel):
    text: str


class EmbedResponse(BaseModel):
    embedding: List[float]
    model: str
    dimension: int


@app.on_event("startup")
def load_model() -> None:
    global model
    model = SentenceTransformer(MODEL_NAME, cache_folder=MODEL_CACHE)


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL_NAME}


@app.post("/embed", response_model=EmbedResponse)
def embed(request: EmbedRequest):
    text = request.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    if model is None:
        raise HTTPException(status_code=503, detail="model is not loaded")

    with model_semaphore:
        vector = model.encode(
            text,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
    embedding = vector.tolist()
    return {
        "embedding": embedding,
        "model": MODEL_NAME,
        "dimension": len(embedding),
    }
