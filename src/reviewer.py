"""
reviewer.py

Sends the diff + retrieved context to Claude and returns a code review.

The prompt structure is:
  [System]  You are an expert code reviewer. Here is additional context...
  [User]    Here is the PR diff. Please review it.

We include the retrieval plan as a comment so the model understands
why each piece of context was included.
"""

import os
import anthropic
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"), override=True)
from src.context_retriever import ContextItem


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


def build_prompt(diff_text: str, selected_items: list, excluded_items: list) -> str:
    """
    Assemble the full prompt that will be sent to Claude.

    Args:
        diff_text:      Raw git diff string
        selected_items: ContextItem list that fit in the budget
        excluded_items: ContextItem list that were excluded (we mention them)

    Returns:
        The user message string
    """
    parts = []

    # ── Retrieved context ────────────────────────────────────────────────────
    if selected_items:
        parts.append("## Retrieved Context\n")
        parts.append("The following context was retrieved from the codebase to help you review:\n")
        for item in selected_items:
            parts.append(f"\n### [{item.category.upper()}] {item.source}")
            parts.append(f"_Reason: {item.reason}_\n")
            parts.append(f"```\n{item.content}\n```")

    # ── Excluded context note ─────────────────────────────────────────────────
    if excluded_items:
        excluded_list = ', '.join(f"{i.category}:{i.source}" for i in excluded_items)
        parts.append(
            f"\n_Note: The following were retrieved but excluded due to token budget: {excluded_list}_\n"
        )

    # ── The actual diff ───────────────────────────────────────────────────────
    parts.append("\n## PR Diff\n")
    parts.append(f"```diff\n{diff_text}\n```")
    parts.append("\nPlease review the above changes.")

    return '\n'.join(parts)


def run_review(
    diff_text: str,
    selected_items: list,
    excluded_items: list,
    model: str = "claude-sonnet-4-6",
) -> tuple:
    """
    Send the prompt to Claude and return the review text + token usage.

    Returns:
        (review_text, input_tokens, output_tokens)
    """
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    user_message = build_prompt(diff_text, selected_items, excluded_items)

    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": user_message}
        ],
    )

    review_text = response.content[0].text
    input_tokens = response.usage.input_tokens
    output_tokens = response.usage.output_tokens

    return review_text, input_tokens, output_tokens
