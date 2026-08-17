"""Token budgeting for memory retrieval.

When a collection grows to 10k-30k entries (an XL project), it is wasteful and
token-expensive to dump full chunks into an agent's context. Instead we return
results in two layers:

    layer 1 - condensed summaries (low token)
    layer 2 - full content for the few entries the agent actually needs

`allocate` decides how many full entries fit within the caller's token budget
after paying for all the summaries, mirroring a tokenizer with a rough
character-based estimate (fast, deterministic, no external dependency).
"""

# Rough heuristic: ~4 chars per token for mixed Thai/English text.
CHARS_PER_TOKEN = 4.0


def estimate_tokens(text: str) -> int:
    """Estimate token count for a piece of text (approximate, no tokenizer)."""
    return max(1, int(len(text) / CHARS_PER_TOKEN))


class TokenAllocation:
    """Result of allocating a token budget across search results."""

    def __init__(
        self,
        summaries: list[dict],
        full_payloads: list[dict],
        budget: int,
        summaries_tokens: int,
        full_tokens: int,
        truncated: int,
    ) -> None:
        self.summaries = summaries
        self.full_payloads = full_payloads
        self.budget = budget
        self.summaries_tokens = summaries_tokens
        self.full_tokens = full_tokens
        self.truncated = truncated

    @property
    def total_tokens(self) -> int:
        return self.summaries_tokens + self.full_tokens

    def to_dict(self) -> dict:
        return {
            "summary": self.summaries,
            "full": self.full_payloads,
            "budget": self.budget,
            "summary_tokens": self.summaries_tokens,
            "full_tokens": self.full_tokens,
            "total_tokens": self.total_tokens,
            "truncated": self.truncated,
        }


def allocate(
    candidates: list[dict],
    budget: int,
    *,
    summaries_per_budget: float = 0.6,
) -> TokenAllocation:
    """Split candidate search hits into summaries + full payloads under a budget.

    Parameters
    ----------
    candidates:
        Ordered search results (most relevant first), each a dict with at least
        ``summary``, ``content``, and ``id`` keys.
    budget:
        Maximum total tokens to return.
    summaries_per_budget:
        Fraction of the budget reserved for condensed summaries; the rest goes
        to the full content of the top entries.
    """
    if budget <= 0:
        budget = 1
    summary_quota = int(budget * summaries_per_budget)
    full_quota = budget - summary_quota

    summaries: list[dict] = []
    summaries_tokens = 0
    full_payloads: list[dict] = []
    full_tokens = 0
    truncated = 0

    for cand in candidates:
        summary = cand.get("summary") or cand.get("content") or ""
        content = cand.get("content") or ""
        s_tok = estimate_tokens(summary)

        # Always add the summary as long as it fits the summary quota.
        if summaries_tokens + s_tok <= summary_quota or not summaries:
            summaries.append(
                {
                    "id": cand.get("id"),
                    "score": cand.get("score"),
                    "summary": summary,
                    "metadata": cand.get("metadata", {}),
                    "has_full": bool(content),
                }
            )
            summaries_tokens += s_tok

        if not content:
            continue

        # Fill full payloads for the top entries within the full quota.
        c_tok = estimate_tokens(content)
        if full_tokens + c_tok <= full_quota:
            full_payloads.append(
                {
                    "id": cand.get("id"),
                    "score": cand.get("score"),
                    "content": content,
                    "metadata": cand.get("metadata", {}),
                }
            )
            full_tokens += c_tok
        else:
            truncated += 1

    return TokenAllocation(
        summaries=summaries,
        full_payloads=full_payloads,
        budget=budget,
        summaries_tokens=summaries_tokens,
        full_tokens=full_tokens,
        truncated=truncated,
    )