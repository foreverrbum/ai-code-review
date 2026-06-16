# Intelligent PR Context Retrieval for LLM Code Review

A CLI tool that takes a Git PR diff and retrieves the most relevant surrounding code to send to Claude for high-quality code review — at minimal token cost.

## The Problem

Sending only the diff gives an LLM too little context: it misses callers, type definitions, and the behavioral contract in tests. Sending the whole codebase is too expensive and degrades quality through context dilution. This system retrieves exactly what matters, ranks it by relevance, and enforces a token budget.

## Core System

The base tool runs with no external API keys beyond your Anthropic key and no special binaries:

```bash
python main.py --diff my.diff --repo /path/to/repo
```

It retrieves context via five methods that run on every PR:

| Method | What it finds |
|---|---|
| tree-sitter AST | Precise function bodies, call sites, imports (Python, TS, JS, Go) |
| grep pre-filter | Finds candidate files in O(1) I/O before opening anything — fast on large repos |
| Test file matching | Test files covering the changed module, by naming convention |
| Git co-change | Files that historically co-change with the modified file — structural coupling AST cannot see |
| Token budget | Drops low-priority items when context exceeds 8,000 tokens; always fits function bodies first |

## Optional Layers

Two flags add more signal at the cost of latency and external dependencies:

| Flag | What it adds | Latency | Requires |
|---|---|---|---|
| `--semantic` | Voyage AI embeddings — finds conceptually similar code that shares no keywords with the changed function | ~5s index build on first run | `VOYAGE_API_KEY` |
| `--lsp` | pyright symbol resolution — follows re-exports and subclass overrides, not text matching | ~2s pyright startup | `pyright-langserver` binary |

**When to use them:** batch CI mode on Python-heavy repos where symbol precision or semantic similarity matters. Neither flag is recommended for interactive use — the base system produces good results for the vast majority of PRs without them.

## How It Works

```
PR diff (local file or GitHub PR)
         │
         ▼
[1] Parse diff       → which files, functions, lines changed?
         │
         ▼
[2] Retrieve context → function bodies, call sites, tests, git co-change, type defs, imports
         │             (grep pre-filter for speed; optional: LSP + embeddings)
         ▼
[3] Rank             → priority 1 (function body) → 5 (imports)
         │
         ▼
[4] Apply budget     → trim to 8,000 token limit; log what was excluded and why
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
ANTHROPIC_API_KEY=sk-ant-...      # required — console.anthropic.com
VOYAGE_API_KEY=pa-...             # optional — enables --semantic (dash.voyageai.com)
GITHUB_TOKEN=ghp_...              # optional — enables --github (github.com/settings/tokens)
```

## Usage

```bash
# Review using a local diff — core system, no external API keys needed beyond Anthropic
python main.py --diff my.diff --repo /path/to/repo

# Show retrieval plan only — no Claude API call, no cost
python main.py --diff my.diff --repo /path/to/repo --no-llm

# Review a real GitHub PR (requires GITHUB_TOKEN in .env)
python main.py --github owner/repo/pull/123 --repo /path/to/local/clone

# Try the included example
python main.py --diff examples/example1_bugfix.diff --repo examples/sample_repo --no-llm

# Generate a diff from any git repo and pipe it in
git diff main..feature-branch > my.diff
python main.py --diff my.diff --repo /path/to/repo

# Tighten the token budget to force more aggressive exclusions (default: 8,000)
python main.py --diff my.diff --repo /path/to/repo --budget 4000

# Optional: add semantic search (requires VOYAGE_API_KEY) or LSP (requires pyright-langserver)
python main.py --diff my.diff --repo /path/to/repo --semantic
python main.py --diff my.diff --repo /path/to/repo --lsp
```

## Example Output

Real run against [psf/requests#7505](https://github.com/psf/requests/pull/7505)
(`python main.py --github psf/requests/pull/7505 --repo ./requests --no-llm`):

```
============================================================
STEP 1: PARSING DIFF
============================================================
PR touches 3 file(s):
  src/requests/_types.py  +5/-0 lines  [SupportsRead, has_read]
  src/requests/models.py  +4/-7 lines  [_encode_params, _encode_files, prepare_body]
  tests/test_requests.py  +15/-0 lines  [test_post_named_tempfile, ...]

============================================================
STEP 2: RETRIEVING CONTEXT
============================================================
Found 21 context item(s) total

============================================================
STEP 3: RANKING
============================================================
=== RETRIEVAL PLAN ===

1. [FUNCTION_BODY] src/requests/models.py
   Priority: 1 | Why: Full body of `_encode_files` — AST-extracted

2. [FUNCTION_BODY] src/requests/_types.py
   Priority: 1 | Why: Full body of `has_read` — AST-extracted

3. [CALL_SITE] src/requests/models.py
   Priority: 2 | Why: Call site of `has_read` — changes here may break callers

4. [GIT_COCHANGE] src/requests/models.py
   Priority: 3 | Why: Co-changed with _types.py in 4 prior commits
                       — historically coupled, changes here may need to stay in sync

5. [GIT_COCHANGE] src/requests/sessions.py
   Priority: 3 | Why: Co-changed with _types.py in 3 prior commits

6. [TYPE_DEF] src/requests/_types.py
   Priority: 4 | Why: Definition of `SupportsRead` used in changed code
...

============================================================
STEP 4: APPLYING TOKEN BUDGET
============================================================
Budget:            8,000 tokens
Context used:      5,928 tokens (20 items)
Excluded:          1 item(s) (over budget)
Diff tokens:         861
Estimated total:   6,989 tokens

Excluded (would exceed budget):
  - [test] tests/test_requests.py (~27,109 tokens)
```

## Project Structure

```
codity/
├── src/
│   ├── diff_parser.py        # Parse git unified diffs → DiffFile objects
│   │                         # Extracts changed files, functions, added/removed lines
│   │
│   ├── ast_parser.py         # tree-sitter AST parsing
│   │                         # Precise function extraction + call-site detection
│   │                         # Supports: Python, TypeScript, JavaScript, Go
│   │
│   ├── context_retriever.py  # Orchestrates all retrieval methods
│   │                         # grep pre-filter, git co-change, test matching, type defs
│   │
│   ├── ranker.py             # Priority-based sorting of context items
│   │
│   ├── token_budget.py       # Token estimation, budget enforcement, cost reporting
│   │
│   ├── reviewer.py           # Claude API with prompt caching
│   │                         # System prompt + context cached; diff not cached
│   │
│   ├── embeddings.py         # [optional] Voyage AI semantic search
│   │                         # voyage-code-2 embeddings + numpy cosine similarity
│   │
│   ├── lsp_client.py         # [optional] pyright LSP integration
│   │                         # JSON-RPC over stdio, textDocument/references
│   │
│   └── github_client.py      # GitHub API — fetch PR diffs and post reviews
│
├── examples/
│   ├── sample_repo/          # Synthetic Python codebase (auth + payments + tests)
│   ├── example1_bugfix.diff
│   ├── example2_refactor.diff
│   └── example3_new_feature.diff
│
├── main.py                   # CLI entry point
├── design_doc.md             # Architecture, tradeoffs, scaling considerations
├── evaluation.md             # 3 real open-source PRs with retrieval plans and token data
├── requirements.txt
└── .env.example
```

## Retrieval Priority System

| Priority | Category | How detected | Why |
|---|---|---|---|
| 1 | `function_body` | tree-sitter AST | The changed function in full — always include |
| 2 | `lsp_reference` | pyright LSP | Precise symbol reference — resolved, not matched |
| 2 | `semantic_match` | Voyage AI embeddings | Conceptually related code keyword search misses |
| 2 | `call_site` | grep + tree-sitter AST | Direct caller — high breakage risk |
| 3 | `test` | filename heuristic | Expected behavior contract |
| 3 | `git_cochange` | gitpython log | Files that historically co-change — structural coupling invisible to static analysis |
| 4 | `type_def` | grep + regex/AST | Data shape context |
| 5 | `import` | tree-sitter AST | Dependency graph |

Call-site and type-definition searches use `grep -rl` as a fast pre-filter before opening
any files. On a 10K-file repo this cuts file reads from ~10,000 to the handful that actually
contain the name, making the tool fast on large open-source codebases.

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
