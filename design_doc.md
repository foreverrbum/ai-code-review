# Design Document: Intelligent PR Context Retrieval

## Problem

Naive AI code review has two failure modes:

| Approach | Problem |
|---|---|
| Send only the diff | AI misses caller impact, type definitions, test coverage — produces shallow reviews |
| Send full codebase | 100K+ tokens per PR — expensive, slow, and degrades review quality through context dilution |

The goal is to **maximize signal while minimizing token cost**.

---

## Context Retrieval Strategy

The system retrieves context in five categories, applied for every file touched in the PR:

### 1. Function Body (Priority 1)
**What:** The full source of each function that was modified.

**Why:** The diff alone only shows the changed lines, not the surrounding logic. An AI reviewer needs to see the complete function to assess whether the change breaks existing behavior (e.g., missed return paths, changed invariants).

**How:** We extract function names from the `@@` hunk headers in the diff, then scan the source file to extract the complete function body. For Python we use indentation depth; for brace languages we count `{` / `}` pairs.

### 2. Call Sites (Priority 2)
**What:** Places in the codebase that call the modified function.

**Why:** A function signature or behavior change silently breaks callers. Showing the AI where and how the function is called lets it catch breaking changes that the diff author may not have considered.

**How:** Regex search for `function_name(` across all source files, capped at 3 callers per function to avoid redundancy.

### 3. Test Coverage (Priority 3)
**What:** Test files that cover the changed module.

**Why:** Tests encode the expected behavior contract. When tests are present, the AI can verify whether the change is consistent with that contract, flag untested paths, and notice if test assertions are no longer valid.

**How:** We match test files by base filename and parent directory name (e.g., `payments/processor.py` → `test_payments.py`).

### 4. Type / Class Definitions (Priority 4)
**What:** Class or type definitions referenced in changed lines.

**Why:** Understanding the shape of data flowing through a function is critical for spotting bugs. Knowing that `LoginResult` has an optional `session` field is context the AI needs to reason about null-safety.

**How:** We extract CamelCase identifiers from changed lines (heuristic for class names), then search the repo for matching `class`/`interface`/`type` definitions.

### 5. Import Declarations (Priority 5)
**What:** The import section of the modified file.

**Why:** Imports reveal the dependency graph. A new import could introduce a heavy dependency or a security risk. At minimum it tells the reviewer what other modules this code depends on.

**How:** Regex match on `import` / `from` / `require` lines, capped at 20.

---

## Ranking and Prioritization

Items are ranked by category priority (1–5), with ties broken by content length (shorter = higher density of signal per token). The intuition:

- **Function body**: Always include. No context is complete without it.
- **Call sites**: High risk. The most common source of silent breakage in PRs.
- **Tests**: Medium. High value when present; absent tests are themselves a signal.
- **Type defs**: Medium. Especially important for typed languages.
- **Imports**: Low. Rarely the most important context, but cheap to include.

---

## Cost Optimization

### Token Budget
Each PR gets a fixed token budget (default: 8,000 tokens) for retrieved context, separate from the diff itself. Items are added in priority order; once the budget is exhausted, remaining items are excluded and listed in the prompt as a note.

### Why 8,000?
A typical PR diff + system prompt consumes ~1,000–2,000 tokens. Total input stays under 12,000 tokens at the 8K context budget, which keeps cost around **$0.036 per PR** with Claude Sonnet. At 1,000 PRs/day, that's ~$36/day — reasonable for a production service.

### Fast Estimation
We use a character-count heuristic (1 token ≈ 4 chars) for pre-filtering — this is fast and accurate enough to make budget decisions without an API round-trip per item. The Anthropic `count_tokens` API is available for exact counts when needed.

### Deduplication
Before ranking, we deduplicate context items by (source file, first 100 chars of content) to avoid sending the same snippet twice when multiple changed functions share callers or tests.

---

## Failure Modes and Tradeoffs

| Failure Mode | Cause | Mitigation |
|---|---|---|
| Missed callers | Dynamic dispatch, dependency injection, string-based lookups | Can't solve statically — document as a known limitation |
| Wrong function extracted | Ambiguous `@@` header (shows outer class, not changed function) | We also scan added/removed lines for `def`/`function` keywords |
| Test file not found | Non-standard naming convention | Fallback: match on parent directory name |
| Budget exceeded, key context dropped | Large type definitions or many callers | Priority ordering ensures function body always fits first |
| Type detection false positives | CamelCase regex matches non-type words | Capped at 5 candidates; false positives waste tokens but don't break the review |

### Key Tradeoff: Recall vs. Precision
We deliberately favor **precision** (only include high-confidence context) over **recall** (include everything potentially relevant). An irrelevant 2,000-token test file can push a critical caller out of the budget. When in doubt, we exclude rather than include.

---

## Scaling Considerations

### Repo Size
The current implementation walks the full repo file tree on every PR. For large repos (100K+ files), this becomes slow. Production solutions:
- **Pre-built index**: Build a symbol-to-file index at push time using tree-sitter or a language server. Query the index instead of walking files.
- **Language Server Protocol (LSP)**: Use `go to definition` / `find references` via an LSP server for precise, language-aware retrieval.

### Concurrency
Context retrieval for multiple files in a single PR can be parallelized using `concurrent.futures.ThreadPoolExecutor`. Each file's retrieval is independent.

### Caching
Function bodies and call sites rarely change between PRs. A cache keyed on `(file_path, git_sha)` would avoid re-reading and re-parsing files that haven't changed since the last review.

### Language Support
Current patterns cover Python, TypeScript, JavaScript, Go, Java, Ruby, and Rust. New languages only require adding a regex pattern to `FUNCTION_PATTERNS` in `context_retriever.py`.

---

## What This System Does Not Do

- **Semantic similarity search** (embedding-based retrieval): Would improve recall for non-trivially-named functions, but adds infrastructure (vector DB, embedding API calls) that meaningfully increases cost and latency per PR. Worth considering for v2.
- **Git history analysis**: Frequently co-changed files are likely semantically related. Not implemented here, but `git log --follow -p` could surface this.
- **Cross-service context**: For microservice architectures, related code may live in separate repositories. Out of scope for this implementation.
