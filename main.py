#!/usr/bin/env python3
"""
main.py — Intelligent PR Context Retrieval for LLM Code Review

Usage:
    python main.py --diff <path-to-diff> --repo <path-to-repo>
    python main.py --diff examples/example1_bugfix.diff --repo examples/sample_repo

Options:
    --diff      Path to a .diff file (or use - to read from stdin)
    --repo      Path to the repository root to search for context
    --budget    Token budget for context (default: 8000)
    --no-llm    Skip the Claude API call (just show retrieval plan)
    --model     Claude model to use (default: claude-sonnet-4-6)
"""

import argparse
import os
import sys
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)

from src.diff_parser import parse_diff, summarize_diff
from src.context_retriever import retrieve_context
from src.ranker import rank, explain_ranking
from src.token_budget import apply_budget, cost_report, format_cost_report, estimate_tokens
from src.reviewer import run_review, build_prompt

load_dotenv()


def main():
    parser = argparse.ArgumentParser(description="Intelligent PR Context Retrieval")
    parser.add_argument("--diff", required=True, help="Path to .diff file (or - for stdin)")
    parser.add_argument("--repo", required=True, help="Path to repository root")
    parser.add_argument("--budget", type=int, default=8000, help="Token budget for context")
    parser.add_argument("--no-llm", action="store_true", help="Skip Claude API call")
    parser.add_argument("--model", default="claude-sonnet-4-6", help="Claude model")
    args = parser.parse_args()

    # ── 1. Load the diff ─────────────────────────────────────────────────────
    if args.diff == '-':
        diff_text = sys.stdin.read()
    else:
        if not os.path.exists(args.diff):
            print(f"ERROR: Diff file not found: {args.diff}")
            sys.exit(1)
        with open(args.diff, 'r') as f:
            diff_text = f.read()

    if not os.path.isdir(args.repo):
        print(f"ERROR: Repository path not found: {args.repo}")
        sys.exit(1)

    print("=" * 60)
    print("STEP 1: PARSING DIFF")
    print("=" * 60)
    diff_files = parse_diff(diff_text)
    print(summarize_diff(diff_files))
    print()

    # ── 2. Retrieve context ───────────────────────────────────────────────────
    print("=" * 60)
    print("STEP 2: RETRIEVING CONTEXT")
    print("=" * 60)
    raw_items = retrieve_context(diff_files, args.repo, token_hint=args.budget)
    print(f"Found {len(raw_items)} context item(s)\n")

    # ── 3. Rank ───────────────────────────────────────────────────────────────
    print("=" * 60)
    print("STEP 3: RANKING")
    print("=" * 60)
    ranked = rank(raw_items)
    print(explain_ranking(ranked))

    # ── 4. Apply token budget ─────────────────────────────────────────────────
    print("=" * 60)
    print("STEP 4: APPLYING TOKEN BUDGET")
    print("=" * 60)
    selected, excluded, context_tokens = apply_budget(ranked, budget=args.budget)
    diff_tokens = estimate_tokens(diff_text)
    system_tokens = 200  # rough estimate for system prompt

    print(f"Budget:            {args.budget:,} tokens")
    print(f"Context used:      {context_tokens:,} tokens ({len(selected)} items)")
    print(f"Excluded:          {len(excluded)} item(s) (over budget)")
    print(f"Diff tokens:       {diff_tokens:,}")
    print(f"Estimated total:   {context_tokens + diff_tokens + system_tokens:,} tokens")

    if excluded:
        print("\nExcluded (would exceed budget):")
        for item in excluded:
            print(f"  - [{item.category}] {item.source} (~{estimate_tokens(item.content)} tokens)")
    print()

    # ── 5. Call Claude (optional) ─────────────────────────────────────────────
    if args.no_llm:
        print("Skipping Claude API call (--no-llm flag set)")
        print("\nPrompt that would be sent:\n")
        print(build_prompt(diff_text, selected, excluded)[:2000] + "\n...[truncated]")
        return

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set in .env file")
        sys.exit(1)

    print("=" * 60)
    print("STEP 5: CLAUDE CODE REVIEW")
    print("=" * 60)
    print("Sending to Claude API...\n")

    review, input_tokens, output_tokens = run_review(
        diff_text, selected, excluded, model=args.model
    )

    print(review)
    print()

    # ── Cost report ───────────────────────────────────────────────────────────
    print("=" * 60)
    report = cost_report(input_tokens, output_tokens)
    print(format_cost_report(report))


if __name__ == "__main__":
    main()
