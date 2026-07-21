"""Token-cost fallback table.

Estimates a run's USD cost from token counts when the harness does not report
cost directly. This is a fallback and will go stale; see the provider pricing
pages linked per section below for current numbers.

Last Updated: 2026-07-20
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPrice:
    """A model's USD price per million tokens.

    Parameters
    ----------
    input : float
        Uncached (cache-miss) input tokens.
    output : float
        Output tokens.
    cached_input : float, optional
        Cache-hit input tokens. ``None`` (default) when the provider publishes no
        cached rate, in which case cached tokens are billed at ``input``.
    """

    input: float
    output: float
    cached_input: float | None = None


#: Price per million tokens, keyed by the model id a harness reports.
COST_PER_MTOK: dict[str, ModelPrice] = {
    # Anthropic (https://platform.claude.com/docs/en/about-claude/pricing).
    # Cache reads are 0.1x input; cache writes (1.25x input) are not modeled.
    "claude-fable-5": ModelPrice(10.0, 50.0, 1.0),
    "claude-opus-4-8": ModelPrice(5.0, 25.0, 0.50),
    "claude-opus-4-7": ModelPrice(5.0, 25.0, 0.50),
    "claude-opus-4-6": ModelPrice(5.0, 25.0, 0.50),
    "claude-sonnet-4-6": ModelPrice(3.0, 15.0, 0.30),
    "claude-sonnet-4-5": ModelPrice(3.0, 15.0, 0.30),
    "claude-haiku-4-5": ModelPrice(1.0, 5.0, 0.10),
    # OpenAI (https://developers.openai.com/api/docs/pricing).
    "gpt-5.6-sol": ModelPrice(5.0, 30.0, 0.50),
    "gpt-5.6-terra": ModelPrice(2.5, 15.0, 0.25),
    "gpt-5.6-luna": ModelPrice(1.0, 6.0, 0.10),
    "gpt-5.5": ModelPrice(5.0, 30.0, 0.50),
    "gpt-5.4": ModelPrice(2.5, 15.0, 0.25),
    "gpt-5.4-mini": ModelPrice(0.75, 4.5, 0.075),
    "gpt-5.4-nano": ModelPrice(0.20, 1.25, 0.02),
    # The pro models are listed without a cached input rate.
    "gpt-5.5-pro": ModelPrice(30.0, 180.0),
    "gpt-5.4-pro": ModelPrice(30.0, 180.0),
    # Legacy OpenAI models, no longer on the pricing page above -- kept at their
    # last known input/output rates, with no cached rate to cite.
    "gpt-4.1": ModelPrice(2.0, 8.0),
    "gpt-4o": ModelPrice(2.5, 10.0),
    "gpt-4o-mini": ModelPrice(0.15, 0.60),
    # Google Gemini (https://ai.google.dev/gemini-api/docs/pricing), standard tier
    # at the <=200k-token prompt rate; prompts above that bill at 2x.
    "gemini-3.1-pro-preview": ModelPrice(2.0, 12.0, 0.20),
    # DeepSeek (https://api-docs.deepseek.com/quick_start/pricing/)
    "deepseek-v4-pro": ModelPrice(0.435, 0.87, 0.003625),
    "deepseek-v4-flash": ModelPrice(0.14, 0.28, 0.0028),
    # xAI Grok (https://docs.x.ai/docs/models), at the <200k-token prompt rate;
    # a prompt at or above that threshold bills every token at 2x.
    "grok-4.5": ModelPrice(2.0, 6.0, 0.30),
    "grok-build-0.1": ModelPrice(1.0, 2.0, 0.20),
    "grok-4.3": ModelPrice(1.25, 2.5, 0.20),
    # Moonshot Kimi Code (https://platform.kimi.ai/docs/pricing/chat), keyed by the
    # ``config.toml`` alias the CLI takes.
    "kimi-code/kimi-for-coding": ModelPrice(0.95, 4.0, 0.19),
    "kimi-code/kimi-for-coding-highspeed": ModelPrice(1.90, 8.0, 0.38),
    "kimi-code/k3": ModelPrice(3.0, 15.0, 0.30),
}


def compute_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
) -> float | None:
    """Estimate the USD cost of a run from token counts.

    Parameters
    ----------
    model : str
        Model id to look up in :data:`COST_PER_MTOK`.
    input_tokens : int
        Total input (prompt) tokens.
    output_tokens : int
        Total output (completion) tokens.
    cached_input_tokens : int, optional
        The cache-hit *subset* of ``input_tokens``, if the harness reports it.
        If unreported, the whole input is billed as uncached, an upper bound.

    Returns
    -------
    float or None
        Estimated USD cost, or ``None`` when ``model`` is absent from
        :data:`COST_PER_MTOK`.
    """
    price = COST_PER_MTOK.get(model)
    if price is None:
        return None
    cached_rate = price.input if price.cached_input is None else price.cached_input
    uncached_tokens = input_tokens - cached_input_tokens
    return (
        uncached_tokens * price.input
        + cached_input_tokens * cached_rate
        + output_tokens * price.output
    ) / 1_000_000
