"""
token_budget.py

Manages the token budget for context sent to the LLM.

Key concepts:
  - Tokens are roughly 4 characters each (Claude's tokenizer is similar)
  - We use the Anthropic API's count_tokens endpoint for accuracy
  - Claude Sonnet 4 pricing: $3.00 per 1M input tokens (as of 2025)
  - We reserve a budget for context; the diff + system prompt use the rest

Budget strategy:
  - Total context budget: 16,000 tokens
  - Always include: diff itself (no budget deducted — it's mandatory)
  - Context budget: 8,000 tokens for retrieved context
  - Items are added in priority order until the budget is exhausted
  - Items that exceed remaining budget are excluded (with a note in the plan)
"""

import anthropic
import os
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"), override=True)

# Approximate: 1 token ≈ 4 characters (used for fast pre-filtering)
CHARS_PER_TOKEN = 4

# How many tokens we allow for the retrieved context (not counting the diff)
CONTEXT_TOKEN_BUDGET = 8_000

# Claude Sonnet pricing (USD per 1M tokens)
INPUT_PRICE_PER_M         = 3.00   # normal input
CACHE_WRITE_PRICE_PER_M   = 3.75   # first call in 5-min window: slight premium to write cache
CACHE_READ_PRICE_PER_M    = 0.30   # subsequent calls: 90% cheaper
OUTPUT_PRICE_PER_M        = 15.00


def estimate_tokens(text: str) -> int:
    """Fast token estimate: character count ÷ 4. Good enough for pre-filtering."""
    return max(1, len(text) // CHARS_PER_TOKEN)


def count_tokens_exact(text: str, model: str = "claude-sonnet-4-6") -> int:
    """
    Use the Anthropic API to count tokens exactly.
    Falls back to estimate if the API call fails.
    """
    try:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        response = client.messages.count_tokens(
            model=model,
            messages=[{"role": "user", "content": text}],
        )
        return response.input_tokens
    except Exception:
        return estimate_tokens(text)


def apply_budget(ranked_items: list, budget: int = CONTEXT_TOKEN_BUDGET) -> tuple:
    """
    Select context items that fit within the token budget.

    Args:
        ranked_items: ContextItem list, already sorted by ranker.rank()
        budget:       Max tokens to spend on context

    Returns:
        (selected_items, excluded_items, tokens_used)
    """
    selected = []
    excluded = []
    tokens_used = 0

    for item in ranked_items:
        item_tokens = estimate_tokens(item.content)

        if tokens_used + item_tokens <= budget:
            selected.append(item)
            tokens_used += item_tokens
        else:
            excluded.append(item)

    return selected, excluded, tokens_used


def cost_report(
    input_tokens: int,
    output_tokens: int = 500,
    cache_stats: dict = None,
) -> dict:
    """
    Estimate API cost for a single review call, accounting for cache hits/misses.

    Args:
        input_tokens:  Total tokens sent to the API (diff + context + system prompt)
        output_tokens: Expected output tokens (review text, default 500)
        cache_stats:   {"created": N, "read": N} from response.usage
    """
    cs = cache_stats or {"created": 0, "read": 0}

    # Tokens that were NOT cached are billed at normal input rate
    normal_tokens = input_tokens - cs["created"] - cs["read"]

    input_cost  = (normal_tokens    / 1_000_000) * INPUT_PRICE_PER_M
    write_cost  = (cs["created"]    / 1_000_000) * CACHE_WRITE_PRICE_PER_M
    read_cost   = (cs["read"]       / 1_000_000) * CACHE_READ_PRICE_PER_M
    output_cost = (output_tokens    / 1_000_000) * OUTPUT_PRICE_PER_M
    total = input_cost + write_cost + read_cost + output_cost

    # What it would have cost without caching
    uncached_cost = (input_tokens / 1_000_000) * INPUT_PRICE_PER_M + output_cost
    savings = uncached_cost - total

    return {
        "input_tokens":          input_tokens,
        "output_tokens":         output_tokens,
        "cache_created_tokens":  cs["created"],
        "cache_read_tokens":     cs["read"],
        "input_cost_usd":        round(input_cost, 6),
        "cache_write_cost_usd":  round(write_cost, 6),
        "cache_read_cost_usd":   round(read_cost, 6),
        "output_cost_usd":       round(output_cost, 6),
        "total_cost_usd":        round(total, 6),
        "savings_usd":           round(savings, 6),
        "cost_per_1000_prs_usd": round(total * 1000, 2),
    }


def format_cost_report(report: dict) -> str:
    cache_line = ""
    if report["cache_created_tokens"] or report["cache_read_tokens"]:
        cache_line = (
            f"  Cache written:   {report['cache_created_tokens']:,} tokens (${report['cache_write_cost_usd']:.4f})\n"
            f"  Cache read:      {report['cache_read_tokens']:,} tokens (${report['cache_read_cost_usd']:.4f})\n"
            f"  Cache savings:   ${report['savings_usd']:.4f}\n"
        )
    return (
        f"=== TOKEN & COST REPORT ===\n"
        f"  Input tokens:    {report['input_tokens']:,}\n"
        f"  Output tokens:   {report['output_tokens']:,}\n"
        f"{cache_line}"
        f"  Total per PR:    ${report['total_cost_usd']:.4f}\n"
        f"  Cost per 1K PRs: ${report['cost_per_1000_prs_usd']:.2f}\n"
    )
