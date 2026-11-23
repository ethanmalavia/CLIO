"""
Non-interactive Chroma DB build for Cloud Build (run by deploy/cloudbuild-chromadb.yaml).

Embeds every document in DOCS_PATH exactly as rag/rag_manager.py's `add` command
does (same loader, metadata, splitter and embedding function, imported from it),
with two differences needed in the cloud:
  - metadata comes from DOCS_PATH/metadata.json instead of the interactive review;
  - requests are paced to EMBED_REQUESTS_PER_MINUTE (each chunk counts as one
    request; the free tier allows 100/minute) and EMBED_TOKENS_PER_MINUTE (the
    free tier allows 30,000/minute), in small batches, and retried on 429.
"""
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "rag"))
import rag_manager as rm  # noqa: E402
from langchain_text_splitters import RecursiveCharacterTextSplitter  # noqa: E402

PER_MINUTE        = int(os.getenv("EMBED_REQUESTS_PER_MINUTE", "90"))
TOKENS_PER_MINUTE = int(os.getenv("EMBED_TOKENS_PER_MINUTE", "24000"))
BATCH_SIZE        = min(rm.BATCH_SIZE, PER_MINUTE, 20)


def estimated_tokens(texts) -> int:
    # Conservative: number-heavy tables run close to 2 characters per token.
    return sum(len(t) for t in texts) // 2 + 1


def add_with_retry(collection, ids, texts, metas):
    for attempt in range(8):
        try:
            collection.add(ids=ids, documents=texts, metadatas=metas)
            return
        except Exception as error:
            if "429" not in str(error) or attempt == 7:
                raise
            print(f"    rate limited, waiting 65 s (attempt {attempt + 1})", flush=True)
            time.sleep(65)


def main():
    client, ef, docs_dir = rm._get_client(), rm._get_embedding_function(), rm._get_docs_dir()
    store = rm._load_metadata_store(docs_dir)
    files = rm._discover_files(docs_dir)
    missing = [f.name for f in files if f.name not in store]
    if missing:
        sys.exit(f"No reviewed metadata for: {', '.join(missing)}")

    splitter = RecursiveCharacterTextSplitter(chunk_size=rm.CHUNK_SIZE, chunk_overlap=rm.CHUNK_OVERLAP)
    collection = client.get_or_create_collection(name=rm._default_collection(), embedding_function=ef)

    for file_path in files:
        chunks = splitter.split_documents(rm._load_file(file_path, store[file_path.name]))
        print(f"{file_path.name}: {len(chunks)} chunks", flush=True)
        for i in range(0, len(chunks), BATCH_SIZE):
            batch = chunks[i:i + BATCH_SIZE]
            texts = [c.page_content for c in batch]
            started = time.monotonic()
            add_with_retry(collection, [str(uuid.uuid4()) for _ in batch], texts,
                           [c.metadata for c in batch])
            # Stay under both per-minute limits on average.
            budget = max(60 * len(batch) / PER_MINUTE, 60 * estimated_tokens(texts) / TOKENS_PER_MINUTE)
            time.sleep(max(0.0, budget - (time.monotonic() - started)))
            print(f"  embedded {min(i + BATCH_SIZE, len(chunks))}/{len(chunks)}", flush=True)

    print(f"Collection '{collection.name}' contains {collection.count()} chunks.")


if __name__ == "__main__":
    main()
