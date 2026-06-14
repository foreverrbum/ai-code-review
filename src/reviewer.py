"""
reviewer.py

Sends the diff + retrieved context to Claude and returns a code review.

Prompt structure (with prompt caching):
  [System]  ← CACHED: never changes, pays write fee once per 5-min window
  [User]
    ├── [Context block] ← CACHED: large, stable within a batch
    └── [Diff block]    ← NOT cached: unique per PR

Prompt caching pricing (Claude Sonnet):
  Normal input:         $3.00 / 1M tokens
  Cache write:          $3.75 / 1M tokens  (5-min TTL)
  Cache read:           $0.30 / 1M tokens  ← 90% savings on repeat calls

In a batch of 10 similar PRs on the same repo, the system prompt + context
is written to cache once and read 9 times. Net input cost ≈ 10x cheaper.
"""

import os
import anthropic
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"), override=True)


SYSTEM_PROMPT = """You are an expert software engineer performing a code review.

You have been provided with:
1. Additional context retrieved from the codebase (callers, tests, type definitions, etc.)
2. The PR diff showing what changed

Your job is to review the changes and provide:
- A summary of what the PR does
- Potential bugs or regressions (especially check call sites and tests)
- Security concerns if any
- Suggestions for improvement
- Overall assessment: APPROVE / REQUEST CHANGES / NEEDS DISCUSSION

Be specific. Reference line numbers and function names. Be concise."""


def build_context_block(selected_items: list, excluded_items: list) -> str:
    """
    Build the retrieved-context section of the prompt.
    This is separated from the diff so we can cache it independently.
    """
    parts = []

    if selected_items:
        parts.append("## Retrieved Context\n")
        parts.append("The following context was retrieved from the codebase to help you review:\n")
        for item in selected_items:
            parts.append(f"\n### [{item.category.upper()}] {item.source}")
            parts.append(f"_Reason: {item.reason}_\n")
            parts.append(f"```\n{item.content}\n```")

    if excluded_items:
        excluded_list = ', '.join(f"{i.category}:{i.source}" for i in excluded_items)
        parts.append(
            f"\n_Note: The following were retrieved but excluded due to token budget: {excluded_list}_\n"
        )

    return '\n'.join(parts)


def build_diff_block(diff_text: str) -> str:
    """The diff itself — always unique, never cached."""
    return f"## PR Diff\n\n```diff\n{diff_text}\n```\n\nPlease review the above changes."


def build_prompt(diff_text: str, selected_items: list, excluded_items: list) -> str:
    """Legacy single-string prompt (used for --no-llm preview)."""
    context = build_context_block(selected_items, excluded_items)
    diff = build_diff_block(diff_text)
    return f"{context}\n\n{diff}"


def run_review(
    diff_text: str,
    selected_items: list,
    excluded_items: list,
    model: str = "claude-sonnet-4-6",
) -> tuple:
    """
    Send the prompt to Claude with prompt caching enabled.

    The message content is split into two blocks:
      1. context_block — tagged with cache_control, reused across calls
      2. diff_block    — not cached, unique per PR

    Returns:
        (review_text, input_tokens, output_tokens, cache_stats)
        where cache_stats = {"created": int, "read": int}
    """
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    context_block = build_context_block(selected_items, excluded_items)
    diff_block = build_diff_block(diff_text)

    response = client.messages.create(
        model=model,
        max_tokens=1024,

        # ── System prompt: cache it ─────────────────────────────────────────
        # The system prompt never changes between calls. We mark it for caching
        # so repeated calls in the same 5-minute window pay 90% less.
        system=[{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],

        messages=[{
            "role": "user",
            "content": [
                # ── Context block: cache it ─────────────────────────────────
                # Retrieved context is large and reusable within a review batch
                # (e.g., same repo, multiple PRs). Cache it.
                {
                    "type": "text",
                    "text": context_block,
                    "cache_control": {"type": "ephemeral"},
                },
                # ── Diff block: do NOT cache ────────────────────────────────
                # The diff is unique per PR. Caching would waste the write fee.
                {
                    "type": "text",
                    "text": diff_block,
                },
            ],
        }],
    )

    review_text = response.content[0].text
    input_tokens = response.usage.input_tokens
    output_tokens = response.usage.output_tokens

    # Cache stats: how many tokens were read from cache vs written
    cache_stats = {
        "created": getattr(response.usage, "cache_creation_input_tokens", 0),
        "read":    getattr(response.usage, "cache_read_input_tokens", 0),
    }

    return review_text, input_tokens, output_tokens, cache_stats
