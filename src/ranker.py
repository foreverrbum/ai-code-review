"""
ranker.py

Scores and sorts context items by relevance.

Priority tiers (lower number = higher priority):
  1. function_body  — the actual changed function, always include
  2. call_site      — callers of the changed function, high risk of breakage
  3. test           — test coverage, shows expected behavior
  4. type_def       — type/class definitions, needed to understand data shapes
  5. import         — import list, useful but low information density
"""

from src.context_retriever import ContextItem


PRIORITY_ORDER = {
    'function_body': 1,
    'call_site': 2,
    'test': 3,
    'type_def': 4,
    'import': 5,
}


def rank(items: list) -> list:
    """
    Sort context items so the most valuable ones come first.
    Items with the same priority are sorted by content length (shorter = denser signal).
    """
    def sort_key(item: ContextItem):
        p = PRIORITY_ORDER.get(item.category, 99)
        # Prefer shorter snippets at the same priority — less filler
        length_penalty = len(item.content) / 10000
        return (p, length_penalty)

    return sorted(items, key=sort_key)


def explain_ranking(items: list) -> str:
    """Return a human-readable retrieval plan explaining each item."""
    lines = ["=== RETRIEVAL PLAN ===\n"]
    for i, item in enumerate(items, 1):
        lines.append(
            f"{i}. [{item.category.upper()}] {item.source}\n"
            f"   Priority: {item.priority} | Why: {item.reason}\n"
            f"   Size: {len(item.content)} chars\n"
        )
    return '\n'.join(lines)
