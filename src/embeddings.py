"""
embeddings.py

Semantic code search using Voyage AI's voyage-code-2 model.

Why embeddings beat keyword search:
  - Finds functions with different names but the same purpose
  - Finds callers that use dependency injection (no direct name reference)
  - Finds similar patterns in other parts of the codebase
  - Works across languages (JS code can find related Python logic)

Vector store: pure numpy (no external DB required)
  - For typical repos (< 50K functions), a matrix cosine similarity is
    computed in milliseconds. No database, no telemetry, no Python 3.9
    dependency. ChromaDB would be appropriate at 1M+ vectors.

Architecture:
  1. INDEX PHASE (once per repo, cached):
     - Walk all source files
     - Extract all function bodies via ast_parser
     - Embed each function chunk using voyage-code-2
     - Store as numpy matrix in memory

  2. QUERY PHASE (per PR):
     - Build a query from the diff: changed functions + added lines
     - Embed the query
     - Compute cosine similarity against all indexed vectors
     - Return top-K hits

Voyage AI pricing (voyage-code-2):
  - $0.18 per 1M tokens
  - Index a 100K-line repo: ~25M tokens → ~$4.50 one-time
  - Per-PR query: ~500 tokens → ~$0.0001
"""

import os
import hashlib
import numpy as np
from typing import Optional
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"), override=True)

from src.ast_parser import extract_all_function_names, extract_function_ast
from src.context_retriever import read_file_safe, _walk_source_files, SKIP_DIRS, AST_SUPPORTED

# ── Lazy imports: only fail if user actually calls embed functions ─────────────
def _get_voyage_client():
    import voyageai
    key = os.getenv("VOYAGE_API_KEY")
    if not key:
        raise RuntimeError(
            "VOYAGE_API_KEY not set in .env. "
            "Get one at dash.voyageai.com → API Keys."
        )
    return voyageai.Client(api_key=key)


# ── Index ─────────────────────────────────────────────────────────────────────

class CodeIndex:
    """
    In-memory vector index of function-level code chunks backed by numpy.

    We store embeddings as a (N, D) float32 matrix and compute cosine
    similarity with a single matrix multiplication — no external DB needed.

    For repos with < ~100K functions this is millisecond-fast. Beyond that,
    swap in a proper ANN index (faiss, hnswlib) without changing the interface.

    Usage:
        index = CodeIndex()
        index.build(repo_path)
        results = index.search(query_text, top_k=5)
    """

    VOYAGE_MODEL    = "voyage-code-2"
    EMBED_BATCH     = 64       # Voyage AI max batch size
    MAX_CHUNK_CHARS = 4000     # Skip huge functions (avoid long-context penalty)

    def __init__(self):
        self._chunks:   list  = []           # list of chunk dicts
        self._matrix:   Optional[np.ndarray] = None  # (N, D) float32
        self._repo_path: Optional[str] = None
        self._built:    bool  = False

    def build(self, repo_path: str, force: bool = False) -> int:
        """
        Index all functions in the repository.
        Idempotent — no-op if already built for the same path.
        Returns number of chunks indexed.
        """
        if self._built and self._repo_path == repo_path and not force:
            return 0

        print(f"  Building semantic index for {repo_path}...")
        chunks = _extract_all_chunks(repo_path)
        if not chunks:
            print("  No indexable chunks found.")
            return 0

        print(f"  Extracted {len(chunks)} function chunks. Embedding...")
        voyage = _get_voyage_client()
        texts  = [c["text"] for c in chunks]

        # Batch embed (Voyage AI has a per-request cap)
        all_embeddings = []
        for i in range(0, len(texts), self.EMBED_BATCH):
            batch  = texts[i : i + self.EMBED_BATCH]
            result = voyage.embed(batch, model=self.VOYAGE_MODEL, input_type="document")
            all_embeddings.extend(result.embeddings)

        # Build L2-normalised matrix for cosine similarity via dot product
        mat = np.array(all_embeddings, dtype=np.float32)
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1e-9
        self._matrix    = mat / norms
        self._chunks    = chunks
        self._repo_path = repo_path
        self._built     = True

        print(f"  Index built: {len(chunks)} chunks, matrix shape {self._matrix.shape}")
        return len(chunks)

    def search(self, query_text: str, top_k: int = 5,
               exclude_sources: list = None) -> list:
        """
        Find top-K chunks most semantically similar to query_text.

        Returns list of dicts: [{source, func_name, text, similarity}]
        """
        if not self._built or self._matrix is None:
            return []

        voyage = _get_voyage_client()
        result = voyage.embed([query_text], model=self.VOYAGE_MODEL, input_type="query")
        q = np.array(result.embeddings[0], dtype=np.float32)
        q /= max(np.linalg.norm(q), 1e-9)

        # Cosine similarity = dot product of L2-normalised vectors
        scores   = self._matrix @ q                    # (N,)
        top_idxs = np.argsort(scores)[::-1]            # highest first

        exclude_set = set(exclude_sources or [])
        hits = []
        for idx in top_idxs:
            chunk = self._chunks[idx]
            if chunk["source"] in exclude_set:
                continue
            hits.append({
                "source":     chunk["source"],
                "func_name":  chunk["func_name"],
                "text":       chunk["text"],
                "similarity": round(float(scores[idx]), 4),
            })
            if len(hits) >= top_k:
                break

        return hits

    def is_built(self) -> bool:
        return self._built


# ── Chunk extraction ──────────────────────────────────────────────────────────

def _extract_all_chunks(repo_path: str) -> list:
    """
    Walk the repo and extract one chunk per function.
    Each chunk = {"id", "source", "func_name", "language", "text"}
    """
    chunks = []

    for rel_path, abs_path in _walk_source_files(repo_path):
        ext = rel_path.rsplit('.', 1)[-1].lower() if '.' in rel_path else ''
        if ext not in AST_SUPPORTED:
            continue

        source = read_file_safe(abs_path)
        if not source:
            continue

        func_names = extract_all_function_names(source, ext)
        for func_name in func_names:
            body = extract_function_ast(source, func_name, ext)
            if not body or len(body) > CodeIndex.MAX_CHUNK_CHARS:
                continue

            # Stable ID: hash of (path + function name)
            chunk_id = hashlib.md5(f"{rel_path}::{func_name}".encode()).hexdigest()
            chunks.append({
                "id":        chunk_id,
                "source":    rel_path,
                "func_name": func_name,
                "language":  ext,
                "text":      f"# {rel_path}: {func_name}\n{body}",
            })

    return chunks


# ── Integration with retrieve_context ─────────────────────────────────────────

def build_semantic_query(diff_files: list) -> str:
    """
    Build a natural-language + code query from the diff.
    We include the added lines and changed function names to anchor the search.
    """
    parts = ["Find code related to these changes:"]

    for diff_file in diff_files:
        if diff_file.changed_functions:
            parts.append(f"Functions modified: {', '.join(diff_file.changed_functions)}")
        added = '\n'.join(diff_file.all_added_lines[:30])
        if added:
            parts.append(f"Added code in {diff_file.file_path}:\n{added}")

    return '\n'.join(parts)


def retrieve_semantic_context(
    diff_files: list,
    repo_path: str,
    index: CodeIndex,
    top_k: int = 4,
) -> list:
    """
    Use semantic search to find context items that keyword search might miss.
    Returns a list of ContextItems with priority 2.5 (between call_site and test).
    """
    from src.context_retriever import ContextItem

    if not index.is_built():
        return []

    changed_files = [f.file_path for f in diff_files]
    query = build_semantic_query(diff_files)

    hits = index.search(query, top_k=top_k, exclude_sources=changed_files)

    items = []
    for hit in hits:
        items.append(ContextItem(
            source=hit["source"],
            content=hit["text"],
            reason=(
                f"Semantically similar to the changed code "
                f"(similarity={hit['similarity']:.2f}, func=`{hit['func_name']}`)"
            ),
            priority=2,     # same tier as call_sites
            category='semantic_match',
        ))

    return items


# ── Singleton index (reused across calls in the same process) ─────────────────
_global_index = CodeIndex()

def get_global_index() -> CodeIndex:
    """Return the process-level singleton index."""
    return _global_index
