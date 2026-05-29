"""
ingestion/run.py
Full ingestion pipeline: parse -> chunk -> embed -> store.
Run once to index all chat history into Qdrant.
Author: ThinkInSystems (Hemanth Aragonda)
"""
import sys
import time
import numpy as np
from pathlib import Path

# Add parent folder to path so imports work
sys.path.insert(0, str(Path(__file__).parent.parent))

from ingestion.parser  import parse_all_exports
from ingestion.chunker import chunk_records

# ── Config ───────────────────────────────────────────────────
EXPORT_DIR   = "chat_logs"
COLLECTION   = "tpm_unite_history"
EMBED_DIM    = 768       # Nomic v1.5 output dimension
BATCH_EMBED  = 32        # reduce to 8 if you run out of memory
BATCH_UPSERT = 256

def load_models():
    """Load embedding model — downloads ~500MB on first run."""
    print("Loading Nomic Embed v1.5...")
    print("(First run downloads ~500MB — takes 2-5 minutes)")
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(
        "nomic-ai/nomic-embed-text-v1.5",
        trust_remote_code=True,
    )
    print("Model loaded.\n")
    return model

def connect_qdrant():
    """Connect to local Qdrant instance."""
    from qdrant_client import QdrantClient
    client = QdrantClient("localhost", port=6333)
    try:
        client.get_collections()
        print("Qdrant connected.\n")
    except Exception:
        print("ERROR: Cannot connect to Qdrant.")
        print("Make sure Docker is running and you ran:")
        print("docker run -d --name qdrant -p 6333:6333 qdrant/qdrant")
        sys.exit(1)
    return client

def setup_collection(client):
    """Create Qdrant collection with indexes."""
    from qdrant_client.models import (
        VectorParams, Distance, PayloadSchemaType
    )
    # Drop existing and recreate fresh
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
    # DateTime index for "after 2022" queries
    client.create_payload_index(
        collection_name=COLLECTION,
        field_name="start_ts",
        field_schema=PayloadSchemaType.DATETIME,
    )
    # Keyword index for channel filtering
    client.create_payload_index(
        collection_name=COLLECTION,
        field_name="channel",
        field_schema=PayloadSchemaType.KEYWORD,
    )
    print(f"Collection '{COLLECTION}' ready.\n")

def embed_chunks(model, chunks):
    """Embed all chunks in batches. Shows progress."""
    # IMPORTANT: Nomic requires "search_document:" prefix
    texts = ["search_document: " + c["text"] for c in chunks]
    total    = len(texts)
    all_embs = []

    print(f"Embedding {total} chunks...")
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
    """Upload chunks + embeddings to Qdrant."""
    from qdrant_client.models import PointStruct
    total = len(chunks)
    print(f"\nStoring {total} chunks in Qdrant...")

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

    print(f"\nDone. {total} chunks stored.")


if __name__ == "__main__":
    t0 = time.time()
    print("=" * 50)
    print("TPM Unite RAG Bot — Ingestion Pipeline")
    print("=" * 50)

    print("\n── Step 1: Parsing exports ──")
    records = parse_all_exports(EXPORT_DIR)

    print("\n── Step 2: Chunking ──")
    chunks = chunk_records(records)

    print("\n── Step 3: Connecting to Qdrant ──")
    client = connect_qdrant()
    setup_collection(client)

    print("\n── Step 4: Loading embedding model ──")
    model = load_models()

    print("\n── Step 5: Embedding chunks ──")
    embeddings = embed_chunks(model, chunks)

    print("\n── Step 6: Storing in Qdrant ──")
    upsert_chunks(client, chunks, embeddings)

    mins = round((time.time() - t0) / 60, 1)
    print(f"\n{'=' * 50}")
    print(f"Ingestion complete in {mins} minutes")
    print(f"Chunks indexed: {len(chunks)}")
    print(f"Collection:     {COLLECTION}")
    print(f"{'=' * 50}")