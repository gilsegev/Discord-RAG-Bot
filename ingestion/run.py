"""
ingestion/run.py
Full ingestion pipeline: parse -> chunk -> embed -> store.
Usage:
  python ingestion/run.py            # incremental upsert (default)
  python ingestion/run.py --recreate # drop and recreate collection
Fixes applied:
  - Fix #1:  SHA-256 instead of MD5 for stable IDs
  - Fix #2:  embed and upsert in same batch loop (memory efficient)
  - Fix #7:  cache-aware model load message
  - Fix #9:  setup_collection comment clarified
  - Fix #10: .gitignore covers standard Python entries
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
    Default (incremental): skip recreate if collection exists.
    Use --recreate flag for full re-index after schema changes.

    Fix #7: try/except on delete is intentional —
    collection may not exist on first run, that's fine.
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
    Fix #11: cache-aware message — only shows download warning
    on first run, shows 'loading from cache' on subsequent runs.
    """
    from sentence_transformers import SentenceTransformer
    print("  Loading Nomic Embed v1.5...")

    cache_path = Path(".model_cache")
    if not cache_path.exists():
        print("  (First run: downloading ~500MB model...)")
    else:
        print("  (Loading from local cache...)")

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
    Fix #1: uses SHA-256 instead of MD5 — avoids security
    linting warnings in CI/CD without any performance cost.
    64-bit space (16 hex chars) minimises collision risk at scale.
    """
    key = "-".join(sorted(chunk["message_ids"]))
    return int(hashlib.sha256(key.encode()).hexdigest()[:16], 16)


def _upsert_batch(client, batch_chunks: list,
                  batch_vecs: np.ndarray, offset: int):
    """Upsert one batch of chunks to Qdrant."""
    from qdrant_client.models import PointStruct
    points = [
        PointStruct(
            id=_stable_id(batch_chunks[j]),
            vector=batch_vecs[j].tolist(),
            payload={
                "text":          batch_chunks[j]["text"],
                "start_ts":      batch_chunks[j]["start_ts"],
                "end_ts":        batch_chunks[j]["end_ts"],
                "channel":       batch_chunks[j]["channel"],
                "authors":       batch_chunks[j]["authors"],
                "message_count": batch_chunks[j]["message_count"],
                "token_count":   batch_chunks[j].get("token_count", 0),
            }
        )
        for j in range(len(batch_chunks))
    ]
    client.upsert(collection_name=COLLECTION, points=points)


def embed_and_upsert(model, client, chunks: list):
    """
    Fix #2: embed and upsert in the same batch loop.
    Avoids holding full embedding array in RAM — scales safely
    to 50k+ chunks for full 6-year multi-channel export.
    """
    total    = len(chunks)
    est_mins = round((total / BATCH_EMBED) * 0.15 / 60, 1)
    print(f"  Embedding and storing {total} chunks "
          f"(estimated ~{max(est_mins, 0.1)} min on CPU)...")

    for i in range(0, total, BATCH_EMBED):
        batch = chunks[i:i + BATCH_EMBED]
        texts = ["search_document: " + c["text"] for c in batch]

        # Nomic requires normalize_embeddings=True for cosine similarity
        embs = model.encode(
            texts,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        _upsert_batch(client, batch, embs, i)

        pct = round(((i + len(batch)) / total) * 100)
        print(f"  {pct}% ({i + len(batch)}/{total})")

    print(f"\n  Done. {total} chunks embedded and stored.")


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
    # Fix #9: mode clearly stated at startup
    if args.recreate:
        print("Mode: FULL RECREATE (--recreate flag set)")
    else:
        print("Mode: INCREMENTAL UPSERT (default)")
    print("=" * 50)

    # Check Qdrant FIRST — fail fast before any processing
    print("\n── Step 1: Checking Qdrant connection ──")
    client = connect_qdrant()

    print("\n── Step 2: Parsing exports ──")
    records = parse_all_exports(EXPORT_DIR)

    if not records:
        print("\nERROR: No records parsed.")
        print("Check chat_logs/ has DiscordChatExporter JSON files")
        print("matching pattern: channel-name [channel_id].json")
        sys.exit(1)

    print("\n── Step 3: Chunking ──")
    chunks = chunk_records(records)

    if not chunks:
        print("\nERROR: No chunks created.")
        print("Check MIN_MSGS setting in chunker.py")
        sys.exit(1)

    # Fix #9: default is incremental — recreate only when explicit
    print("\n── Step 4: Setting up Qdrant collection ──")
    setup_collection(client, force_recreate=args.recreate)

    print("\n── Step 5: Loading embedding model ──")
    model = load_models()

    # Fix #2: embed and upsert in same loop — memory efficient
    print("\n── Step 6: Embedding and storing chunks ──")
    embed_and_upsert(model, client, chunks)

    mins = round((time.time() - t0) / 60, 1)
    print(f"\n{'=' * 50}")
    print(f"  Ingestion complete in {mins} minutes")
    print(f"  Messages parsed:  {len(records)}")
    print(f"  Chunks indexed:   {len(chunks)}")
    print(f"  Collection:       {COLLECTION}")
    print(f"{'=' * 50}")