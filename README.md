# Intelligent PR Context Retrieval for LLM Code Review

A CLI tool that takes a Git PR diff and retrieves the most relevant surrounding code to send to Claude for high-quality code review — at minimal token cost.

## The Problem

Sending only a diff to an LLM gives it too little context. Sending the whole codebase is too expensive and degrades review quality through context dilution. This system retrieves exactly what's needed using four complementary methods:

| Method | What it finds |
|---|---|
| tree-sitter AST | Precise function bodies, call expressions, import statements |
| pyright LSP | Symbol-resolved references (not text search — follows the actual symbol) |
| Voyage AI embeddings | Semantically similar code even with different names |
| Keyword / test matching | Related test files by naming convention |

## How It Works

```
PR diff (local file or GitHub PR)
         │
         ▼
[1] Parse diff       → which files, functions, lines changed? (tree-sitter)
         │
         ▼
[2] Retrieve context → function bodies, call sites, tests, types, semantic matches
         │   (AST + LSP + embeddings run in parallel)
         ▼
[3] Rank             → priority 1 (function body) → 5 (imports)
         │
         ▼
[4] Apply budget     → trim to 8,000 token limit
         │
         ▼
[5] Claude review    → prompt cached for 90% cost reduction on repeat calls
```

## Setup

```bash
# 1. Clone and enter the directory
cd /path/to/codity

# 2. Install dependencies
pip install -r requirements.txt

# 3. Copy the env template and fill in your keys
cp .env.example .env
```

Edit `.env`:
```
ANTHROPIC_API_KEY=sk-ant-...      # console.anthropic.com
VOYAGE_API_KEY=pa-...             # dash.voyageai.com → API Keys
GITHUB_TOKEN=ghp_...              # github.com/settings/tokens → repo scope
```

Only `ANTHROPIC_API_KEY` is required for basic use. `VOYAGE_API_KEY` enables semantic search (`--semantic`). `GITHUB_TOKEN` enables GitHub PR fetching (`--github`).

## Usage

```bash
# Basic review using a local diff file
python main.py --diff examples/example1_bugfix.diff --repo examples/sample_repo

# Show retrieval plan only — no API call, no cost
python main.py --diff examples/example1_bugfix.diff --repo examples/sample_repo --no-llm

# Full production mode: AST + LSP + semantic search
python main.py \
  --diff examples/example1_bugfix.diff \
  --repo examples/sample_repo \
  --lsp       \   # precise symbol references via pyright
  --semantic      # semantic similarity via Voyage AI

# Review a real GitHub PR
python main.py \
  --github owner/repo/pull/123 \
  --repo /path/to/local/clone \
  --lsp --semantic

# Generate a diff from any git repo and pipe it in
git diff main..my-feature-branch > my_pr.diff
python main.py --diff my_pr.diff --repo /path/to/repo

# Adjust token budget (default: 8,000)
python main.py --diff my.diff --repo my/repo --budget 4000
```

## Example Output

```
============================================================
STEP 1: PARSING DIFF
============================================================
PR touches 1 file(s):
  auth/service.py  +9/-2 lines  [verify_password, authenticate]

============================================================
STEP 2: RETRIEVING CONTEXT
============================================================
  Running LSP reference resolution (pyright)...
  + 1 LSP reference(s)
  Building semantic index for examples/sample_repo...
  Extracted 22 function chunks. Embedding...
  Index built: 22 chunks, matrix shape (22, 1536)
  + 4 semantic match(es)

Found 12 context item(s) total

============================================================
STEP 3: RANKING
============================================================
=== RETRIEVAL PLAN ===

1. [FUNCTION_BODY] auth/service.py
   Priority: 1 | Why: Full body of `authenticate` — AST-extracted

2. [LSP_REFERENCE] auth/service.py
   Priority: 2 | Why: LSP-resolved reference to `verify_password` (line 25)
                       — precise symbol match, not a text search

3. [SEMANTIC_MATCH] tests/test_auth.py
   Priority: 2 | Why: Semantically similar (similarity=0.84, func=`test_wrong_password_fails`)

4. [CALL_SITE] tests/test_auth.py
   Priority: 2 | Why: Call site of `authenticate` — changes here may break callers
...

============================================================
STEP 4: APPLYING TOKEN BUDGET
============================================================
Budget:            8,000 tokens
Context used:      1,214 tokens (12 items)
Estimated total:   1,739 tokens

============================================================
STEP 5: CLAUDE CODE REVIEW
============================================================
[Claude's review appears here]

============================================================
=== TOKEN & COST REPORT ===
  Input tokens:    1,847
  Output tokens:   412
  Cache written:   1,203 tokens ($0.0045)
  Cache read:      0 tokens ($0.0000)
  Total per PR:    $0.0118
  Cost per 1K PRs: $11.80
```

## Project Structure

```
codity/
├── src/
│   ├── diff_parser.py        # Parse git unified diffs → DiffFile objects
│   │                         # Extracts changed files, functions, added/removed lines
│   │
│   ├── ast_parser.py         # tree-sitter AST parsing (NEW)
│   │                         # Precise function extraction + call-site detection
│   │                         # Supports: Python, TypeScript, JavaScript, Go
│   │
│   ├── context_retriever.py  # Orchestrates all retrieval methods
│   │                         # AST-first with regex fallback for unsupported languages
│   │
│   ├── embeddings.py         # Voyage AI semantic search (NEW)
│   │                         # voyage-code-2 embeddings + numpy cosine similarity
│   │                         # No external vector DB required
│   │
│   ├── lsp_client.py         # pyright LSP integration (NEW)
│   │                         # JSON-RPC over stdio, textDocument/references
│   │                         # Precise symbol resolution, not text matching
│   │
│   ├── ranker.py             # Priority-based sorting of context items
│   │
│   ├── token_budget.py       # Token estimation, budget enforcement, cost reporting
│   │                         # Includes prompt cache hit/miss accounting
│   │
│   ├── reviewer.py           # Claude API with prompt caching (NEW)
│   │                         # System prompt + context block cached (cache_control)
│   │                         # Diff block not cached (unique per PR)
│   │
│   └── github_client.py      # GitHub API integration (NEW)
│                             # Fetch PR diffs, metadata; post reviews back to GitHub
│
├── examples/
│   ├── sample_repo/          # Synthetic Python codebase (auth + payments + tests)
│   ├── example1_bugfix.diff  # Adding logging to auth failure path
│   ├── example2_refactor.diff # Refactoring payment processor + adding session check
│   └── example3_new_feature.diff # Adding audit logging to auth events
│
├── main.py                   # CLI entry point
├── design_doc.md             # Architecture, tradeoffs, scaling considerations
├── requirements.txt
└── .env.example
```

## Retrieval Priority System

| Priority | Category | How detected | Why |
|---|---|---|---|
| 1 | `function_body` | tree-sitter AST | The changed function in full — always include |
| 2 | `lsp_reference` | pyright LSP | Precise symbol reference — resolved, not matched |
| 2 | `semantic_match` | Voyage AI embeddings | Conceptually related code keyword search misses |
| 2 | `call_site` | tree-sitter AST | Direct caller — high breakage risk |
| 3 | `test` | filename heuristic | Expected behavior contract |
| 4 | `type_def` | regex + AST | Data shape context |
| 5 | `import` | tree-sitter AST | Dependency graph |

## Cost Model

Using Claude Sonnet with prompt caching:

| Scenario | Input tokens | Cost per PR | Cost / 1K PRs |
|---|---|---|---|
| Basic (diff only) | ~500 | ~$0.002 | ~$2 |
| With context (typical) | ~1,500 | ~$0.005 | ~$5 |
| With context, cache hit | ~1,500 | ~$0.001 | ~$1 |
| Full (LSP + semantic) | ~2,000 | ~$0.006 | ~$6 |

Prompt caching cuts effective input cost by ~90% on repeated calls within a 5-minute window. The system prompt and retrieved context are cached; only the diff (unique per PR) is billed at full rate.

**Voyage AI (semantic search):** $0.18/1M tokens — a 100K-line repo indexes for ~$4.50 one-time; each PR query costs ~$0.0001.

## Supported Languages

| Language | AST parsing | LSP | Notes |
|---|---|---|---|
| Python | ✅ tree-sitter | ✅ pyright | Full support |
| TypeScript | ✅ tree-sitter | 🔧 add ts-server | Grammar installed |
| JavaScript | ✅ tree-sitter | 🔧 add ts-server | Grammar installed |
| Go | ✅ tree-sitter | — | Grammar installed |
| Java | — regex fallback | — | |
| Ruby | — regex fallback | — | |
| Rust | — regex fallback | — | |

See `design_doc.md` for full architecture, tradeoffs, and scaling considerations.
