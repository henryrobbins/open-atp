"""Token-cost fallback table.

Estimates a run's USD cost from token counts when the harness does not report
cost directly (notably Codex). This is a fallback and will go stale; see the
provider pricing pages for current numbers.
"""

from __future__ import annotations

#: Cost per million tokens, as ``(input, output)``.
COST_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-5.5": (5.0, 30.0),
    "gpt-5.4": (2.5, 15.0),
    "gpt-5.4-mini": (0.75, 4.5),
    "gpt-5.4-nano": (0.20, 1.25),
    # Numina discussion-partner defaults (gpt/gemini backends). ESTIMATES -- verify
    # against the provider pricing pages; unknown variants stay unpriced and are
    # surfaced via NuminaProver's ``helper_unpriced_models`` rather than billed at 0.
    "gpt-5.4-pro": (15.0, 120.0),
    "gemini-3.1-pro-preview": (2.0, 12.0),
    "deepseek-v4-pro": (1.74, 3.48),
    "deepseek-v4-flash": (0.14, 0.28),
    # xAI Grok (https://docs.x.ai/docs/models). The opencode xAI provider does not
    # self-report USD, so these back the cost estimate for the grok prover.
    "grok-4.5": (2.0, 6.0),
    "grok-build-0.1": (1.0, 2.0),
    "grok-4.3": (1.25, 2.5),
}


def compute_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Estimate the USD cost of a run from token counts.

    Returns ``None`` when ``model`` is absent from :data:`COST_PER_MTOK`.
    """
    entry = COST_PER_MTOK.get(model)
    if entry is None:
        return None
    input_price, output_price = entry
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000
