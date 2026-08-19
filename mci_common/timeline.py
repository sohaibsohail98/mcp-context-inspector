"""Shared cumulative-token math for get_context_timeline() — both
metrics/store_sqlite.py and metrics/store_dynamodb.py had this exact
loop duplicated (found in an independent fresh-eyes review). Each
backend still does its own query/row-shaping; only the running-total
math lives here.
"""

from mci_common.config import CONTEXT_WINDOW_TOKENS


def build_timeline(rows):
    """rows: an ordered iterable of dicts, each with category, label,
    char_count, token_estimate, turn_n, and status. Returns the same
    dicts with cumulative_tokens/cumulative_pct added, running total in
    row order."""
    blocks = []
    cumulative_tokens = 0
    for row in rows:
        cumulative_tokens += row["token_estimate"]
        blocks.append(
            {
                **row,
                "cumulative_tokens": cumulative_tokens,
                "cumulative_pct": round(cumulative_tokens / CONTEXT_WINDOW_TOKENS * 100, 4),
            }
        )
    return blocks
