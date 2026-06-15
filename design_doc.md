# Design Document: Intelligent PR Context Retrieval

## Problem

Sending only the diff to an LLM produces shallow reviews — no caller context, no type
shapes, no behavioral contract. Sending the full codebase costs 100K+ tokens per PR and
degrades review quality through context dilution. The goal is to maximize signal per token.

---

## Architecture

```
GitHub PR / local diff
         │
         ▼
  ┌─────────────┐
  │ diff_parser │  Parse unified diff → DiffFile objects.
  └──────┬──────┘  Extract changed files, function names, added/removed lines.
         │
         ▼
  ┌───────────────────────────────────────────────────────┐
  │               context_retriever                        │
  │  ┌──────────────┐  ┌──────────┐  ┌──────────────┐    │
  │  │  ast_parser  │  │   LSP    │  │  embeddings  │    │
  │  │ (tree-sitter)│  │(pyright) │  │ (Voyage AI)  │    │
  │  └──────────────┘  └──────────┘  └──────────────┘    │
  │  ┌──────────────┐  ┌──────────┐                       │
  │  │  git history │  │  grep    │                       │
  │  │  co-change   │  │pre-filter│                       │
  │  └──────────────┘  └──────────┘                       │
  └────────────────────────┬──────────────────────────────┘
                           │ ContextItem[]
                           ▼
                    ┌─────────────┐
                    │   ranker    │  Priority sort (1–5)
                    └──────┬──────┘
                           ▼
                    ┌─────────────┐
                    │token_budget │  Trim to token limit, cost estimate
                    └──────┬──────┘
                           ▼
                    ┌─────────────┐
                    │  reviewer   │  Claude API with prompt caching
                    └─────────────┘
```

---

## Retrieval Strategy

Six context categories are retrieved per changed file, merged, deduplicated, and ranked:

| Priority | Category | Method | Why |
|---|---|---|---|
| 1 | `function_body` | tree-sitter AST | Full body of the changed function — diff alone omits surrounding logic and return paths |
| 2 | `lsp_reference` | pyright LSP | Symbol-resolved references; follows re-exports and subclass overrides, not text matching |
| 2 | `call_site` | tree-sitter + grep | Callers of the changed function; signature/return changes break callers silently in dynamic languages |
| 2 | `semantic_match` | Voyage AI voyage-code-2 | Finds semantically related code that shares no keyword with the changed function |
| 3 | `test` | filename heuristic | Behavioral contract; shows what the function is supposed to do and what's currently untested |
| 3 | `git_cochange` | gitpython log | Files that co-change with the modified file in git history — structural coupling invisible to static analysis |
| 4 | `type_def` | regex + AST | Class/interface definitions referenced in changed lines; needed to reason about data shape |
| 5 | `import` | tree-sitter AST | Import list; flags new dependencies, circular imports, security risks |

Items are sorted by priority tier, with ties broken by content length (shorter = denser signal per token).

### Precision vs. Recall

Every layer favors **precision**. An irrelevant 2,000-token file can displace a critical
call site from the budget. We use multiple methods (AST, LSP, semantic, git) to improve
recall *without* sacrificing precision — each contributes only high-confidence results.

---

## Scalability

**Call-site search on large repos:**
Without optimization, finding callers requires opening every source file (O(n) reads). On a
10K-file monorepo this is prohibitively slow.

**grep pre-filter:** Before opening any file, we run:
```bash
grep -rl "func_name" repo/ --include="*.py" --exclude-dir=node_modules ...
```
Grep uses OS memory-mapped I/O and returns only file names. A 10K-file Django codebase
returns ~5–15 candidate files in <1s; only those are opened for AST parsing. The Python
file walk remains as a fallback when grep is unavailable (Windows, restricted CI).

**Beyond 100K files:** Build a `{symbol → [file:line]}` map at push time using tree-sitter.
O(1) lookup replaces O(n) walk. LSP (`pyright-langserver`) maintains a persistent index —
`textDocument/references` is O(1) after warm-up.

**Embedding index:** Built once per process, cached in memory. A production system keys on
`(file_path, git_sha)` and rebuilds only on changes. For repos > 100K functions, swap the
numpy matrix for `faiss` without changing the interface.

---

## Cost Model

| Scenario | Input tokens | Cost per PR |
|---|---|---|
| Basic (diff only) | ~500 | ~$0.002 |
| With context (typical) | ~1,500 | ~$0.005 |
| With context, cache hit | ~1,500 | ~$0.001 |
| Full (LSP + semantic) | ~2,000 | ~$0.006 |

**Prompt caching (Claude Sonnet):**
- System prompt + retrieved context → `cache_control: ephemeral` (reused across the batch)
- PR diff → not cached (unique per PR)
- Cache read: $0.30/1M vs $3.00/1M normal — **90% cheaper on repeat calls**

**Voyage AI:** $0.18/1M tokens. Indexing a 100K-line repo costs ~$4.50 once; each PR query ~$0.0001.

---

## Failure Modes

| Failure | Cause | Mitigation |
|---|---|---|
| Missed callers | Dynamic dispatch, duck typing | LSP resolves symbol rather than matching text |
| Wrong function extracted | `@@` header shows outer class | Also scan added/removed lines for function definition patterns |
| Test file not found | Non-standard naming | Fallback: match parent directory name |
| Budget exhausted before key context | Large test file ranked ahead of call site | Priority ordering guarantees function_body always fits first |
| Type detection false positives | CamelCase heuristic | Capped at 5 candidates; false positives waste < 200 tokens |
| LSP timeout | Large repo, cold index | Falls back to AST call detection silently |
| git_cochange empty | No git history | Returns `[]` silently |

---

## What This System Does Not Do

- **Cross-repo context:** Callers in microservice sibling repos are out of scope.
- **Incremental embedding index:** Rebuilds on each process start; production would key on `(file_path, git_sha)`.
- **Cross-language LSP:** pyright covers Python; TypeScript requires `typescript-language-server` (transport is already language-agnostic).
- **Git blame / recency weighting:** Recently changed callers are higher risk; not implemented but a natural next addition.
