import os
from functools import lru_cache
from typing import List

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer


MODEL_NAME = os.getenv("MODEL_NAME", "nomic-ai/nomic-embed-text-v1.5")
MODEL_CACHE = os.getenv("MODEL_CACHE", "/models")

hf_token = os.getenv("HF_TOKEN")
if hf_token:
    os.environ["HUGGINGFACE_HUB_TOKEN"] = hf_token

app = FastAPI(title="RAG Bot Query Embedder")


class EmbedRequest(BaseModel):
    text: str


class EmbedResponse(BaseModel):
    embedding: List[float]
    model: str
    dimension: int


@lru_cache(maxsize=1)
def get_model() -> SentenceTransformer:
    return SentenceTransformer(MODEL_NAME, cache_folder=MODEL_CACHE)


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL_NAME}


@app.post("/embed", response_model=EmbedResponse)
def embed(request: EmbedRequest):
    text = request.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    vector = get_model().encode(
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
