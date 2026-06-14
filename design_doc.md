# Design Document: Intelligent PR Context Retrieval

## Problem

Naive AI code review has two failure modes:

| Approach | Problem |
|---|---|
| Send only the diff | AI misses caller impact, type definitions, test coverage — produces shallow reviews |
| Send full codebase | 100K+ tokens per PR — expensive, slow, and degrades review quality through context dilution |

The goal is to **maximize signal while minimizing token cost**.

---

## System Architecture

```
GitHub PR / local diff
         │
         ▼
  ┌─────────────┐
  │ diff_parser │  ← Parse unified diff into structured DiffFile objects.
  │             │    Extract changed files, functions, added/removed lines.
  └──────┬──────┘
         │ DiffFile[]
         ▼
  ┌─────────────────────────────────────────────────────────┐
  │                   context_retriever                      │
  │                                                          │
  │  ┌─────────────┐  ┌──────────┐  ┌──────────┐           │
  │  │ ast_parser  │  │  LSP     │  │embeddings│           │
  │  │ (tree-sitter│  │(pyright) │  │(Voyage AI│           │
  │  │  precise    │  │ symbol   │  │ semantic │           │
  │  │  extraction)│  │ lookup)  │  │ search)  │           │
  │  └─────────────┘  └──────────┘  └──────────┘           │
  └──────────────────────┬──────────────────────────────────┘
                         │ ContextItem[]
                         ▼
                  ┌─────────────┐
                  │   ranker    │  ← Sort by priority tier (1–5)
                  └──────┬──────┘
                         │
                         ▼
                  ┌─────────────┐
                  │token_budget │  ← Trim to token limit, estimate cost
                  └──────┬──────┘
                         │ selected items
                         ▼
                  ┌─────────────┐
                  │  reviewer   │  ← Assemble prompt with cache_control markers,
                  │  (Claude)   │    call Anthropic API, return review + cost stats
                  └─────────────┘
```

---

## Context Retrieval Strategy

Context is retrieved in five categories for every file touched in the PR. All five run in parallel; results are merged, deduplicated, ranked, and budget-trimmed before being sent to the LLM.

### 1. Function Body (Priority 1) — via tree-sitter AST
**What:** The complete source of each function modified in the PR.

**Why:** The diff shows only changed lines, not surrounding logic. Without the full function body, the LLM cannot assess missed return paths, changed invariants, or whether the edit is consistent with the function's overall contract.

**How:** We use **tree-sitter** to parse the source file into an AST and extract the exact node boundaries of the target function. This is significantly more precise than our previous regex + indentation approach:
- Won't match function names inside strings, comments, or decorator arguments
- Returns exact start/end line numbers from the AST node, not a heuristic block extraction
- Handles decorated functions (Python `@property`, `@staticmethod`) correctly by returning the outer decorated node
- Supports Python, TypeScript, JavaScript, and Go with the same interface; falls back to regex for Java, Ruby, Rust, C#

### 2. Call Sites (Priority 2) — via tree-sitter + LSP
**What:** Places in the codebase that call the modified function.

**Why:** The most common source of silent breakage. A changed function signature, return type, or error behavior can break every caller without a compile error (especially in dynamic languages).

**How (two layers):**
- **tree-sitter AST call detection** (default): Walk each file's AST looking for `call_expression` nodes whose callee matches the function name. Unlike regex, this won't match the function definition itself, or occurrences inside string literals or comments.
- **LSP symbol resolution** (`--lsp` flag): Launch `pyright-langserver` as a subprocess, open the changed file, and issue a `textDocument/references` request at the function definition's position. This resolves the *symbol* rather than matching text — it correctly handles re-exports, subclass overrides, and cases where two different functions happen to share a name.

LSP is the more precise option but adds ~1–2s startup latency per review. For CI integration, it's worth it. For interactive use, tree-sitter is fast enough.

### 3. Test Coverage (Priority 3)
**What:** Test files that cover the changed module.

**Why:** Tests are the behavioral contract. They tell the LLM what the function is *supposed* to do, making it easier to catch regressions and flag untested code paths introduced by the PR.

**How:** Match test files by base filename and parent directory name (e.g., `payments/processor.py` → `test_payments.py` or `test_processor.py`). We also include the entire test file (not just individual test functions) so the LLM sees the full suite, including setup/teardown fixtures that reveal intent.

### 4. Semantic Matches (Priority 2) — via Voyage AI embeddings
**What:** Functions elsewhere in the codebase that are semantically similar to the changed code, even if they share no keyword overlap.

**Why:** Keyword search misses:
- Functions with different names doing the same thing (`validate_credentials` vs `authenticate`)
- Callers that inject the function via a parameter (dependency injection)
- Similar patterns in other modules that may need the same fix

**How:** We use **Voyage AI's `voyage-code-2`** model, which is specifically trained on code. At index time, every function body in the repo is embedded into a 1536-dimensional vector and stored in an in-memory numpy matrix. At query time, the diff is embedded and we compute cosine similarity against all indexed vectors via a single matrix multiplication. Top-K results are returned as context items.

Vector store: pure numpy (no external database). For repos up to ~100K functions, matrix multiplication is sub-100ms. Beyond that, swap in `faiss` or `hnswlib` without changing the interface.

**Cost:** Voyage AI `voyage-code-2` costs $0.18/1M tokens — about 17× cheaper than Claude input tokens. Indexing a 100K-line repo costs roughly $4.50 one-time; each PR query costs ~$0.0001.

### 5. Type / Class Definitions (Priority 4)
**What:** Class, interface, or dataclass definitions referenced in the changed lines.

**Why:** Understanding the shape of data is critical for catching type-related bugs. Knowing that `LoginResult` has an optional `session` field (which is `None` on failure) is context the LLM needs to reason about null-safety.

**How:** CamelCase identifier extraction from changed lines (heuristic for class names), followed by AST-aware definition search across the repo.

### 6. Import Declarations (Priority 5) — via tree-sitter AST
**What:** Import statements from the modified file.

**Why:** A new import could introduce a security risk, a circular dependency, or a heavy dependency. Import lists also make the module's dependency graph legible to the reviewer.

**How:** tree-sitter import node extraction, which is more reliable than regex (won't match commented-out imports or imports inside strings).

---

## Ranking and Prioritization

Items are sorted by priority tier, with ties broken by content length (shorter = higher information density per token):

| Priority | Category | Rationale |
|---|---|---|
| 1 | `function_body` | Always include — no review is complete without it |
| 2 | `lsp_reference` | Precise symbol reference — resolved, not matched |
| 2 | `semantic_match` | Conceptually related code that keyword search misses |
| 2 | `call_site` | AST-detected caller — high breakage risk |
| 3 | `test` | Behavioral contract of the changed function |
| 4 | `type_def` | Data shape context |
| 5 | `import` | Dependency graph — lowest signal, but cheap |

---

## Cost Optimization

### Token Budget
Each PR gets a fixed token budget (default: 8,000 tokens) for retrieved context, separate from the diff itself. Items are added in priority order; once the budget is exhausted, remaining items are excluded and noted in the prompt so the LLM knows what it didn't see.

### Prompt Caching
We use **Anthropic's prompt caching** feature to reduce input costs on repeated calls. The prompt is structured as two blocks:

```
[System prompt]          ← cache_control: ephemeral (TTL: 5 min)
[Retrieved context]      ← cache_control: ephemeral (large, reusable)
[PR diff]                ← not cached (unique per PR)
```

Cache pricing (Claude Sonnet):
- Normal input: $3.00 / 1M tokens
- Cache write: $3.75 / 1M tokens (paid once per 5-minute window)
- Cache read: $0.30 / 1M tokens — **90% cheaper**

In practice, the system prompt (constant) and retrieved context (large) are cache-written on the first call and cache-read on all subsequent calls within the 5-minute window. For a batch of 10 PRs reviewed in quick succession, input cost drops by ~85%.

### Fast Token Estimation
We use a character-count heuristic (1 token ≈ 4 chars) for pre-filtering budget decisions — fast and accurate enough without an API round-trip per item. The cost report uses actual token counts from the API response.

### Deduplication
Before ranking, context items are deduplicated by `(source_file, content[:100])` to avoid sending the same snippet twice when multiple changed functions share callers or tests.

---

## GitHub Integration

The system integrates with the GitHub API to fetch PR diffs and metadata directly:

```bash
python main.py --github owner/repo/pull/123 --repo /path/to/local/checkout
```

- `fetch_pr_diff`: Downloads the unified diff via the GitHub v3 API (`Accept: application/vnd.github.v3.diff`)
- `fetch_pr_metadata`: Fetches title, description, author, branch names, change counts
- `fetch_file_from_github`: Fetches individual file contents when no local checkout is available
- `post_review_comment`: Posts the AI review back to the PR as a GitHub review (APPROVE / REQUEST_CHANGES / COMMENT)

Authentication: GitHub personal access token with `repo` scope. Rate limit: 5,000 requests/hour (authenticated).

---

## Failure Modes and Tradeoffs

| Failure Mode | Cause | Mitigation |
|---|---|---|
| Missed callers | Dynamic dispatch, duck typing, string-based method invocation | LSP resolves more cases than regex; document remainder as known limitation |
| Wrong function extracted | `@@` hunk header shows outer class, not modified function | We also scan added/removed lines for function definition patterns |
| Test file not found | Non-standard naming convention | Fallback: match on parent directory name; future: git history co-change analysis |
| Budget exceeded, key context dropped | Large test file or many callers | Priority ordering ensures function body always fits first |
| Type detection false positives | CamelCase heuristic matches non-type words | Capped at 5 candidates; false positives waste tokens but don't corrupt the review |
| LSP timeout | Large repo takes > 20s to index | Falls back to AST-based call detection silently |
| Embedding index stale | Repo changed since last index build | Index is rebuilt per-process; production would key on `(file_path, git_sha)` |

### Key Tradeoff: Recall vs. Precision
We favor **precision** over **recall** at every layer. An irrelevant 2,000-token test file can displace a critical call site from the budget. We use multiple retrieval methods (AST, LSP, semantic) specifically to improve recall *without* sacrificing precision — each method only contributes high-confidence results.

---

## Scaling Considerations

### Repo Size
The current implementation walks the full file tree on every PR. This is fast for repos under ~10K files (~200ms). For monorepos:
- **Symbol index**: Build a `{symbol_name → [file:line]}` map at push time using tree-sitter. O(1) lookup replaces O(n) walk.
- **LSP as primary**: `pyright-langserver` maintains its own persistent index; `textDocument/references` is O(1) after the initial warm-up.
- **Embedding index persistence**: Persist the numpy matrix and chunk metadata to disk, keyed on git commit SHA. Rebuild only on changes.

### Concurrency
Context retrieval for multiple files in a single PR is independent. A `concurrent.futures.ThreadPoolExecutor` over `retrieve_context(diff_files)` would give near-linear speedup up to the number of changed files.

### Caching
A cache keyed on `(file_path, git_sha)` eliminates re-parsing files unchanged since the last review. Particularly valuable for the embedding index (Voyage AI API calls) and LSP reference resolution.

### Language Support
tree-sitter grammars: Python, TypeScript, JavaScript, Go (installed). Add any language with `pip install tree-sitter-<lang>` and a one-line entry in `FUNCTION_NODE_TYPES`. Regex fallback covers Java, Ruby, Rust, C# without grammar installation.

LSP server support: currently Python only (pyright). TypeScript support requires `typescript-language-server`; the JSON-RPC transport in `lsp_client.py` is language-agnostic.

---

## What This System Does Not Do

- **Git history analysis**: Frequently co-changed files are semantically related. `git log --follow -p` could surface this — not implemented, but a natural v2 addition.
- **Cross-repository context**: For microservice architectures, related code lives in separate repos. Out of scope for this implementation.
- **Incremental embedding index**: The current index rebuilds in full when `force=True`. A production system would diff the git tree and re-embed only changed functions.
- **Cross-language LSP**: pyright covers Python; `typescript-language-server` would add JS/TS. The transport layer is already language-agnostic.
