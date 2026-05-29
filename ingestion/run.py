"""
ingestion/run.py
Full ingestion pipeline: parse -> chunk -> embed -> store.
Improvements:
  - Qdrant connection checked FIRST before any processing
  - Empty records/chunks guard prevents cryptic numpy errors
  - Time estimate printed before slow embedding step
Author: ThinkInSystems (Hemanth Aragonda)
"""
import sys
import time
import numpy as np
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from ingestion.parser  import parse_all_exports
from ingestion.chunker import chunk_records

# ── Config ───────────────────────────────────────────────────
EXPORT_DIR   = "chat_logs"
COLLECTION   = "tpm_unite_history"
EMBED_DIM    = 768
BATCH_EMBED  = 32
BATCH_UPSERT = 256


def connect_qdrant():
    from qdrant_client import QdrantClient
    client = QdrantClient("localhost", port=6333)
    try:
        client.get_collections()
        print("  Qdrant connected.\n")
    except Exception:
        print("\nERROR: Cannot connect to Qdrant.")
        print("Make sure Docker is running and Qdrant is started:")
        print("  docker start qdrant")
        sys.exit(1)
    return client


def setup_collection(client):
    from qdrant_client.models import (
        VectorParams, Distance, PayloadSchemaType
    )
    try:
        client.delete_collection(COLLECTION)
    except Exception:
        pass
    client.create_collection(
        collection_name=COLLECTION,
        vectors_config=VectorParams(
            size=EMBED_DIM,
            distance=Distance.COSINE,
        ),
    )
    client.create_payload_index(
        collection_name=COLLECTION,
        field_name="start_ts",
        field_schema=PayloadSchemaType.DATETIME,
    )
    client.create_payload_index(
        collection_name=COLLECTION,
        field_name="channel",
        field_schema=PayloadSchemaType.KEYWORD,
    )
    print(f"  Collection '{COLLECTION}' ready.\n")


def load_models():
    print("  Loading Nomic Embed v1.5...")
    print("  (First run downloads ~500MB — takes 2-5 minutes)")
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(
        "nomic-ai/nomic-embed-text-v1.5",
        trust_remote_code=True,
    )
    print("  Model loaded.\n")
    return model


def embed_chunks(model, chunks):
    texts    = ["search_document: " + c["text"] for c in chunks]
    total    = len(texts)
    all_embs = []

    # Estimate time before starting
    est_mins = round((total / 32) * 0.15 / 60, 1)
    print(f"  Embedding {total} chunks "
          f"(estimated ~{est_mins} min on CPU)...")

    for i in range(0, total, BATCH_EMBED):
        batch = texts[i:i + BATCH_EMBED]
        embs  = model.encode(
            batch,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        all_embs.append(embs)
        pct = round(((i + len(batch)) / total) * 100)
        print(f"  {pct}% ({i + len(batch)}/{total})")

    return np.vstack(all_embs)


def upsert_chunks(client, chunks, embeddings):
    from qdrant_client.models import PointStruct
    total = len(chunks)
    print(f"\n  Storing {total} chunks in Qdrant...")

    for i in range(0, total, BATCH_UPSERT):
        b_chunks = chunks[i:i + BATCH_UPSERT]
        b_vecs   = embeddings[i:i + BATCH_UPSERT]
        points   = [
            PointStruct(
                id=i + j,
                vector=b_vecs[j].tolist(),
                payload={
                    "text":          b_chunks[j]["text"],
                    "start_ts":      b_chunks[j]["start_ts"],
                    "end_ts":        b_chunks[j]["end_ts"],
                    "channel":       b_chunks[j]["channel"],
                    "authors":       b_chunks[j]["authors"],
                    "message_count": b_chunks[j]["message_count"],
                    "token_count":   b_chunks[j].get("token_count", 0),
                }
            )
            for j in range(len(b_chunks))
        ]
        client.upsert(collection_name=COLLECTION, points=points)
        pct = round(((i + len(b_chunks)) / total) * 100)
        print(f"  {pct}%")

    print(f"\n  Done. {total} chunks stored.")


if __name__ == "__main__":
    t0 = time.time()
    print("=" * 50)
    print("TPM Unite RAG Bot — Ingestion Pipeline")
    print("=" * 50)

    # ── Check Qdrant FIRST before any processing ──────────────
    print("\n── Step 1: Checking Qdrant connection ──")
    client = connect_qdrant()

    # ── Parse ─────────────────────────────────────────────────
    print("\n── Step 2: Parsing exports ──")
    records = parse_all_exports(EXPORT_DIR)

    # Guard: stop if nothing was parsed
    if not records:
        print("\nERROR: No records parsed.")
        print("Check that chat_logs/ has JSON files and channels")
        print("match the ELIGIBLE_CHANNELS list in parser.py")
        sys.exit(1)

    # ── Chunk ─────────────────────────────────────────────────
    print("\n── Step 3: Chunking ──")
    chunks = chunk_records(records)

    # Guard: stop if no chunks produced
    if not chunks:
        print("\nERROR: No chunks created from records.")
        print("Check MIN_MSGS setting in chunker.py")
        sys.exit(1)

    # ── Setup Qdrant collection ───────────────────────────────
    print("\n── Step 4: Setting up Qdrant collection ──")
    setup_collection(client)

    # ── Load model ────────────────────────────────────────────
    print("\n── Step 5: Loading embedding model ──")
    model = load_models()

    # ── Embed ─────────────────────────────────────────────────
    print("\n── Step 6: Embedding chunks ──")
    embeddings = embed_chunks(model, chunks)

    # ── Store ─────────────────────────────────────────────────
    print("\n── Step 7: Storing in Qdrant ──")
    upsert_chunks(client, chunks, embeddings)

    mins = round((time.time() - t0) / 60, 1)
    print(f"\n{'=' * 50}")
    print(f"  Ingestion complete in {mins} minutes")
    print(f"  Messages parsed:  {len(records)}")
    print(f"  Chunks indexed:   {len(chunks)}")
    print(f"  Collection:       {COLLECTION}")
    print(f"{'=' * 50}")