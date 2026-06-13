import os
import time
from typing import Any, Dict, List

from fastapi import FastAPI
from pydantic import BaseModel, Field
from sentence_transformers import CrossEncoder

MODEL_NAME = os.getenv("MODEL_NAME", "cross-encoder/ms-marco-MiniLM-L-6-v2")
MODEL_CACHE = os.getenv("MODEL_CACHE", "/models")
os.environ.setdefault("HF_HOME", MODEL_CACHE)
os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", MODEL_CACHE)
os.environ.setdefault("TRANSFORMERS_CACHE", MODEL_CACHE)

app = FastAPI(title="Discord RAG Bot Reranker")
model: CrossEncoder | None = None


class Candidate(BaseModel):
    id: str
    text: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RerankRequest(BaseModel):
    query: str
    candidates: List[Candidate]


@app.on_event("startup")
def load_model() -> None:
    global model
    model = CrossEncoder(MODEL_NAME)


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "model": MODEL_NAME}


@app.post("/rerank")
def rerank(request: RerankRequest) -> Dict[str, Any]:
    if model is None:
        return {
            "model": MODEL_NAME,
            "latency_ms": 0,
            "results": [],
            "error": "model_not_loaded",
        }

    if not request.candidates:
        return {"model": MODEL_NAME, "latency_ms": 0, "results": []}

    started = time.time()
    pairs = [(request.query, candidate.text) for candidate in request.candidates]
    scores = model.predict(pairs).tolist()  # type: ignore[union-attr]

    results = []
    for candidate, score in zip(request.candidates, scores):
        results.append(
            {
                "id": candidate.id,
                "reranker_score": float(score),
                "metadata": candidate.metadata,
            }
        )

    results.sort(key=lambda item: item["reranker_score"], reverse=True)
    return {
        "model": MODEL_NAME,
        "latency_ms": int((time.time() - started) * 1000),
        "results": results,
    }
