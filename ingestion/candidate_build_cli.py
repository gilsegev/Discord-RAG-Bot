"""Build an isolated candidate corpus; this command has no serving cutover path."""
import argparse
import os
import json

import psycopg
from qdrant_client import QdrantClient

from ingestion.candidate_rebuild import (
    CandidateRebuildError, admit_frozen_cutoff, create_candidate_collection,
    embed_candidate, load_captures, plan_candidate, seed_candidate_metadata,
)
from ingestion.parser import parse_all_exports


def build_candidate(connection, qdrant, exports, collection, embedder_url, requested_cutoff=None):
    observed, frozen_at = admit_frozen_cutoff(connection, requested_cutoff)
    plan = plan_candidate(parse_all_exports(exports), load_captures(connection, observed),
                          candidate_collection=collection, frozen_capture_sequence=observed,
                          frozen_at=frozen_at)
    create_candidate_collection(qdrant, collection)
    try:
        embed_candidate(embedder_url, qdrant, plan)
        seed_candidate_metadata(connection, plan)
        connection.commit()
    except Exception:
        connection.rollback()
        qdrant.delete_collection(collection)
        raise
    return {key: value for key, value in plan.items() if not key.startswith("_")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection")
    parser.add_argument("--requested-cutoff", type=int)
    parser.add_argument("--exports", default=os.getenv("EXPORT_DIR", "chat_logs"))
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"), required=False)
    parser.add_argument("--qdrant-url", default=os.getenv("QDRANT_URL", "http://localhost:6333"))
    parser.add_argument("--embedder-url", default=os.getenv("EMBEDDER_URL", "http://localhost:8000"))
    parser.add_argument("--authorize-regression", metavar="CANDIDATE_ID")
    args = parser.parse_args()
    if not args.database_url:
        raise CandidateRebuildError("DATABASE_URL is required")
    with psycopg.connect(args.database_url) as connection:
        if args.authorize_regression:
            row = connection.execute("SELECT * FROM rag_authorize_candidate_regression(%s)", (args.authorize_regression,)).fetchone()
            connection.commit()
            print(json.dumps({"authorization_id": str(row[0]), "candidate_id": row[1], "qdrant_collection": row[2],
                              "target_corpus_version_id": row[3], "target_manifest_digest": row[4],
                              "target_capture_cutoff_sequence": row[5]}))
            return
        if not args.collection:
            raise CandidateRebuildError("--collection is required for a build")
        result = build_candidate(connection, QdrantClient(url=args.qdrant_url), args.exports,
                                 args.collection, args.embedder_url, args.requested_cutoff)
    print(result["candidate_id"])


if __name__ == "__main__":
    main()
