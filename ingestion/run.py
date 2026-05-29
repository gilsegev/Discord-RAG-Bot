"""
ingestion/run.py
Full ingestion pipeline: parse -> chunk -> embed -> store.
Usage:
  python ingestion/run.py            # incremental upsert
  python ingestion/run.py --recreate # drop and recreate collection
Author: ThinkInSystems (Hemanth Aragonda)
"""
import sys
import time
import hashlib
import argparse
import numpy as np
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from ingestion.parser  import parse_all_exports
from ingestion.chunker import chunk_records

# ── Config ───────────────────────────────────────────────────
EXPORT_DIR   = "chat_logs"
COLLECTION   = "tpm_unite_history"
EMBED_DIM    = 768
BATCH_EMBED  = 32    # safe for CPU — increase to 128 on GPU
BATCH_UPSERT = 256


def connect_qdrant():
    """Connect to local Qdrant — fails fast with clear message."""
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


def setup_collection(client, force_recreate: bool = False):
    """
    Create Qdrant collection with datetime and keyword indexes.
    force_recreate=False (default): skip if collection exists.
    force_recreate=True: drop existing and recreate from scratch.
    Use --recreate flag for full re-index runs.
    """
    from qdrant_client.models import (
        VectorParams, Distance, PayloadSchemaType
    )

    exists = any(
        c.name == COLLECTION
        for c in client.get_collections().collections
    )

    if exists and not force_recreate:
        print(f"  Collection '{COLLECTION}' exists. "
              f"Running incremental upsert.\n")
        return

    if exists:
        print(f"  Dropping existing collection '{COLLECTION}'...")
        client.delete_collection(COLLECTION)

    client.create_collection(
        collection_name=COLLECTION,
        vectors_config=VectorParams(
            size=EMBED_DIM,
            distance=Distance.COSINE,
        ),
    )
    # DateTime index for date-range queries e.g. "after 2022"
    client.create_payload_index(
        collection_name=COLLECTION,
        field_name="start_ts",
        field_schema=PayloadSchemaType.DATETIME,
    )
    # Keyword index for channel-scoped queries
    client.create_payload_index(
        collection_name=COLLECTION,
        field_name="channel",
        field_schema=PayloadSchemaType.KEYWORD,
    )
    print(f"  Collection '{COLLECTION}' ready.\n")


def load_models():
    """
    Load Nomic Embed v1.5.
    Cached in .model_cache/ to avoid re-download on every run.
    """
    print("  Loading Nomic Embed v1.5...")
    print("  (First run downloads ~500MB — subsequent runs use cache)")
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(
        "nomic-ai/nomic-embed-text-v1.5",
        trust_remote_code=True,
        cache_folder=".model_cache",
    )
    print("  Model loaded.\n")
    return model


def _stable_id(chunk: dict) -> int:
    """
    Stable 64-bit integer ID from chunk's message IDs.
    Uses 16 hex chars (64-bit space) to minimise collision risk
    at scale. Deterministic across re-runs for safe incremental upsert.
    """
    key = "-".join(sorted(chunk["message_ids"]))
    return int(hashlib.md5(key.encode()).hexdigest()[:16], 16)


def embed_chunks(model, chunks: list) -> np.ndarray:
    """
    Embed all chunks in batches.
    Nomic requires 'search_document:' prefix on chunks.
    Queries use 'search_query:' prefix — different from doc prefix.
    """
    texts    = ["search_document: " + c["text"] for c in chunks]
    total    = len(texts)
    all_embs = []

    est_mins = round((total / 32) * 0.15 / 60, 1)
    print(f"  Embedding {total} chunks "
          f"(estimated ~{max(est_mins, 0.1)} min on CPU)...")

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


def upsert_chunks(client, chunks: list, embeddings: np.ndarray):
    """
    Upload chunks + embeddings to Qdrant.
    Stable hash IDs mean re-runs safely upsert without corruption.
    """
    from qdrant_client.models import PointStruct
    total = len(chunks)
    print(f"\n  Storing {total} chunks in Qdrant...")

    for i in range(0, total, BATCH_UPSERT):
        b_chunks = chunks[i:i + BATCH_UPSERT]
        b_vecs   = embeddings[i:i + BATCH_UPSERT]
        points   = [
            PointStruct(
                id=_stable_id(b_chunks[j]),
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
    # ── CLI args ──────────────────────────────────────────────
    arg_parser = argparse.ArgumentParser(
        description="TPM Unite RAG Bot — Ingestion Pipeline"
    )
    arg_parser.add_argument(
        "--recreate",
        action="store_true",
        help="Drop and recreate the Qdrant collection from scratch"
    )
    args = arg_parser.parse_args()

    t0 = time.time()
    print("=" * 50)
    print("TPM Unite RAG Bot — Ingestion Pipeline")
    if args.recreate:
        print("Mode: FULL RECREATE")
    else:
        print("Mode: INCREMENTAL UPSERT")
    print("=" * 50)

    print("\n── Step 1: Checking Qdrant connection ──")
    client = connect_qdrant()

    print("\n── Step 2: Parsing exports ──")
    records = parse_all_exports(EXPORT_DIR)

    if not records:
        print("\nERROR: No records parsed.")
        print("Check chat_logs/ has JSON files and channels")
        print("match ELIGIBLE_CHANNELS in parser.py")
        sys.exit(1)

    print("\n── Step 3: Chunking ──")
    chunks = chunk_records(records)

    if not chunks:
        print("\nERROR: No chunks created.")
        print("Check MIN_MSGS setting in chunker.py")
        sys.exit(1)

    print("\n── Step 4: Setting up Qdrant collection ──")
    setup_collection(client, force_recreate=args.recreate)

    print("\n── Step 5: Loading embedding model ──")
    model = load_models()

    print("\n── Step 6: Embedding chunks ──")
    embeddings = embed_chunks(model, chunks)

    print("\n── Step 7: Storing in Qdrant ──")
    upsert_chunks(client, chunks, embeddings)

    mins = round((time.time() - t0) / 60, 1)
    print(f"\n{'=' * 50}")
    print(f"  Ingestion complete in {mins} minutes")
    print(f"  Messages parsed:  {len(records)}")
    print(f"  Chunks indexed:   {len(chunks)}")
    print(f"  Collection:       {COLLECTION}")
    print(f"{'=' * 50}")