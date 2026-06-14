# Intelligent PR Context Retrieval for LLM Code Review

A system that takes a Git PR diff and retrieves the most relevant surrounding code to send to an LLM for high-quality code review — at minimal token cost.

## The Problem

Sending only a diff to an LLM gives it too little context. Sending the whole codebase is too expensive. This system retrieves exactly what's needed: function bodies, callers, tests, and type definitions — ranked by relevance and trimmed to a token budget.

## How It Works

```
PR diff
   │
   ▼
[1] Parse diff          → which files, functions, lines changed?
   │
   ▼
[2] Retrieve context    → function bodies, call sites, tests, types
   │
   ▼
[3] Rank               → priority 1 (function body) → 5 (imports)
   │
   ▼
[4] Apply token budget  → fit within 8,000 tokens
   │
   ▼
[5] Call Claude API     → get code review
```

## Setup

```bash
# 1. Clone and enter the directory
cd codity

# 2. Install dependencies
pip install -r requirements.txt

# 3. Add your Anthropic API key
cp .env.example .env
# Edit .env and set ANTHROPIC_API_KEY=your-key-here
```

## Usage

```bash
# Full review (calls Claude API)
python main.py --diff examples/example1_bugfix.diff --repo examples/sample_repo

# Show retrieval plan only, no API call
python main.py --diff examples/example1_bugfix.diff --repo examples/sample_repo --no-llm

# Use your own diff and repo
python main.py --diff path/to/my.diff --repo path/to/my/repo

# Adjust token budget (default: 8000)
python main.py --diff my.diff --repo my/repo --budget 4000
```

### Generate a diff from a real git repo

```bash
# Diff between two commits
git diff abc123..def456 > my_pr.diff

# Diff of a branch vs main
git diff main..my-feature-branch > my_pr.diff

# Then run the reviewer
python main.py --diff my_pr.diff --repo /path/to/repo
```

## Example Output

```
STEP 1: PARSING DIFF
PR touches 1 file(s):
  auth/service.py  +9/-2 lines  [verify_password, authenticate]

STEP 2: RETRIEVING CONTEXT
Found 6 context item(s)

STEP 3: RANKING
=== RETRIEVAL PLAN ===
1. [FUNCTION_BODY] auth/service.py
   Priority: 1 | Why: Full body of `authenticate` — the function directly modified in this PR

2. [CALL_SITE] tests/test_auth.py
   Priority: 2 | Why: Call site of `authenticate` — changes here may break callers
...

STEP 4: APPLYING TOKEN BUDGET
Budget:          8,000 tokens
Context used:      408 tokens (6 items)
Estimated total:   933 tokens

STEP 5: CLAUDE CODE REVIEW
[Claude's review appears here]

TOKEN & COST REPORT
  Input tokens:    1,247
  Total per PR:    $0.0037
  Cost per 1K PRs: $3.70
```

## Project Structure

```
codity/
├── src/
│   ├── diff_parser.py        # Parse git diff → structured objects
│   ├── context_retriever.py  # Find function bodies, callers, tests, types
│   ├── ranker.py             # Rank context items by relevance
│   ├── token_budget.py       # Token counting and cost estimation
│   └── reviewer.py           # Claude API integration
├── examples/
│   ├── sample_repo/          # Synthetic Python codebase for demos
│   ├── example1_bugfix.diff  # Example: adding logging to auth
│   ├── example2_refactor.diff # Example: refactoring payment processor
│   └── example3_new_feature.diff # Example: adding audit logging
├── main.py                   # CLI entry point
├── design_doc.md             # Architecture and tradeoff decisions
└── requirements.txt
```

## Supported Languages

Python, TypeScript, JavaScript, Go, Java, Ruby, Rust

## Context Priority System

| Priority | Category | Why |
|---|---|---|
| 1 | Function body | The changed code in full — always include |
| 2 | Call sites | Most common source of silent breakage |
| 3 | Test files | Shows expected behavior contract |
| 4 | Type definitions | Needed to understand data shapes |
| 5 | Imports | Low-signal but cheap to include |

## Cost

Using Claude Sonnet (claude-sonnet-4-6):
- Typical PR review: ~1,200 input tokens + ~500 output tokens
- Cost per review: ~$0.004 (less than half a cent)
- Cost per 1,000 PRs: ~$4

See `design_doc.md` for full architecture, tradeoffs, and scaling considerations.
