"""
ingestion/run.py
Full ingestion pipeline: parse -> chunk -> embed -> store.
Usage:
  python ingestion/run.py            # incremental upsert (default)
  python ingestion/run.py --recreate # drop and recreate collection
v7 fixes:
  - Fix 1: stable ID includes split_index — prevents 8 lost vectors
  - Fix 2: CPU time estimate formula corrected
  - Fix 3: span_days stored in Qdrant payload
  - Fix 4: trust_remote_code removed — not needed in ST >= 5.3.0
Author: ThinkInSystems (Hemanth Aragonda)
"""
import os
import sys
import time
import hashlib
import argparse
import numpy as np
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))

from ingestion.parser  import parse_all_exports
from ingestion.chunker import chunk_records

load_dotenv()

# ── Config ───────────────────────────────────────────────────
EXPORT_DIR   = "chat_logs"
COLLECTION   = "tpm_unite_history"
EMBED_DIM    = 768
BATCH_EMBED  = 32    # safe for CPU — increase to 128 on GPU
BATCH_UPSERT = 256

# Fix 4: trust_remote_code no longer needed in sentence-transformers >= 5.3.0
# Removing it eliminates the need to pin a specific model revision
NOMIC_MODEL  = "nomic-ai/nomic-embed-text-v1.5"


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
    # DateTime index for date-range queries
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
    # Keyword index for thread-scoped queries
    client.create_payload_index(
        collection_name=COLLECTION,
        field_name="thread_name",
        field_schema=PayloadSchemaType.KEYWORD,
    )
    # Float index for long-span chunk filtering
    client.create_payload_index(
        collection_name=COLLECTION,
        field_name="span_days",
        field_schema=PayloadSchemaType.FLOAT,
    )
    print(f"  Collection '{COLLECTION}' ready.\n")


def load_models():
    """
    Load Nomic Embed v1.5.
    Fix 4: trust_remote_code removed — not needed in ST >= 5.3.0.
    This eliminates the need for a pinned revision entirely.
    Cache-aware message — only shows download warning on first run.
    """
    hf_token = os.getenv("HF_TOKEN")
    if hf_token:
        os.environ["HUGGINGFACE_HUB_TOKEN"] = hf_token
        print("  HuggingFace token loaded.")
    else:
        print("  WARN: No HF_TOKEN found in .env — "
              "unauthenticated requests may hit rate limits.")

    from sentence_transformers import SentenceTransformer
    print("  Loading Nomic Embed v1.5...")
    print("  (trust_remote_code not required — ST >= 5.3.0)")

    cache_path = Path(".model_cache")
    if not cache_path.exists():
        print("  (First run: downloading ~500MB model...)")
    else:
        print("  (Loading from local cache...)")

    try:
        model = SentenceTransformer(
            NOMIC_MODEL,
            cache_folder=".model_cache",
        )
    except Exception as e:
        print(f"\nERROR: Could not load embedding model: {e}")
        print("If offline, ensure .model_cache/ exists from a prior run.")
        sys.exit(1)

    print("  Model loaded.\n")
    return model


def _stable_id(chunk: dict) -> int:
    """
    Stable 64-bit integer ID from chunk's message IDs + split index.
    Fix 1: split_index included so split pieces don't overwrite each
    other in Qdrant. Previously 8 vectors were lost per run.
    """
    split_index = chunk.get("split_index", 0)
    key = "-".join(sorted(chunk["message_ids"])) + f":{split_index}"
    return int(hashlib.sha256(key.encode()).hexdigest()[:16], 16)


def get_existing_ids(client) -> set:
    """
    Fetch all point IDs already stored in Qdrant.
    Paginates through all points — handles collections of any size.
    Returns empty set on error — triggers full re-embed for safety.
    """
    existing = set()
    offset   = None
    try:
        while True:
            result, offset = client.scroll(
                collection_name=COLLECTION,
                limit=1000,
                offset=offset,
                with_payload=False,
                with_vectors=False,
            )
            existing.update(p.id for p in result)
            if offset is None:
                break
    except Exception as e:
        print(f"  WARN: Could not fetch existing IDs: {e}")
        print("  Falling back to full re-embed for safety.")
        return set()
    print(f"  Found {len(existing)} existing chunks in Qdrant.")
    return existing


def filter_new_chunks(chunks: list, existing_ids: set) -> list:
    """Return only chunks not already in Qdrant."""
    new_chunks = [c for c in chunks
                  if _stable_id(c) not in existing_ids]
    skipped = len(chunks) - len(new_chunks)
    if skipped:
        print(f"  Skipping {skipped} already-indexed chunks.")
    print(f"  Embedding {len(new_chunks)} new chunks.")
    return new_chunks


def _upsert_batch_with_retry(client, points, max_retries: int = 3):
    """Upsert with exponential backoff retry."""
    for attempt in range(max_retries):
        try:
            client.upsert(collection_name=COLLECTION, points=points)
            return
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            wait = 2 ** attempt
            print(f"  WARN: Upsert failed (attempt {attempt + 1}), "
                  f"retrying in {wait}s: {e}")
            time.sleep(wait)


def _upsert_batch(client, batch_chunks: list,
                  batch_vecs: np.ndarray, offset: int):
    """Build Qdrant points and upsert with retry."""
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
                "thread_name":   batch_chunks[j].get("thread_name"),
                "authors":       batch_chunks[j]["authors"],
                "message_count": batch_chunks[j]["message_count"],
                "token_count":   batch_chunks[j].get("token_count", 0),
                "span_days":     batch_chunks[j].get("span_days", 0),
                "split_index":   batch_chunks[j].get("split_index", 0),
            }
        )
        for j in range(len(batch_chunks))
    ]
    _upsert_batch_with_retry(client, points)


def embed_and_upsert(model, client, chunks: list,
                     force_recreate: bool = False):
    """
    Embed and upsert in the same batch loop.
    Memory efficient — no large array held in RAM.
    Incremental mode skips already-indexed chunks.
    """
    if not force_recreate:
        existing_ids = get_existing_ids(client)
        chunks       = filter_new_chunks(chunks, existing_ids)

    if not chunks:
        print("  No new chunks to embed. Collection is up to date.")
        return

    total = len(chunks)
    # Fix 2: corrected estimate — 0.15 min per batch not per second
    est_mins = round((total / BATCH_EMBED) * 0.15, 1)
    print(f"\n  Embedding and storing {total} chunks "
          f"(estimated ~{max(est_mins, 0.1)} min on CPU)...")

    for i in range(0, total, BATCH_EMBED):
        batch = chunks[i:i + BATCH_EMBED]
        texts = ["search_document: " + c["text"] for c in batch]
        embs  = model.encode(
            texts,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        _upsert_batch(client, batch, embs, i)
        pct = round(((i + len(batch)) / total) * 100)
        print(f"  {pct}% ({i + len(batch)}/{total})")

    print(f"\n  Done. {total} chunks embedded and stored.")


if __name__ == "__main__":
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
        print("Mode: FULL RECREATE (--recreate flag set)")
    else:
        print("Mode: INCREMENTAL UPSERT (default)")
    print("=" * 50)

    print("\n── Step 1: Checking Qdrant connection ──")
    client = connect_qdrant()

    print("\n── Step 2: Parsing exports ──")
    records = parse_all_exports(EXPORT_DIR)

    if not records:
        print("\nERROR: No records parsed.")
        print("Check chat_logs/ has DiscordChatExporter JSON files")
        sys.exit(1)

    print("\n── Step 3: Chunking ──")
    chunks = chunk_records(records)

    if not chunks:
        print("\nERROR: No chunks created.")
        sys.exit(1)

    print("\n── Step 4: Setting up Qdrant collection ──")
    setup_collection(client, force_recreate=args.recreate)

    print("\n── Step 5: Loading embedding model ──")
    model = load_models()

    print("\n── Step 6: Embedding and storing chunks ──")
    embed_and_upsert(model, client, chunks,
                     force_recreate=args.recreate)

    mins = round((time.time() - t0) / 60, 1)
    print(f"\n{'=' * 50}")
    print(f"  Ingestion complete in {mins} minutes")
    print(f"  Messages parsed:  {len(records)}")
    print(f"  Chunks indexed:   {len(chunks)}")
    print(f"  Collection:       {COLLECTION}")
    print(f"{'=' * 50}")