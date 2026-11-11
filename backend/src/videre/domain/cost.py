"""Metered cost of model calls. Pure integer arithmetic in micro-USD; never floats.

LLM spend is list-price metering (the Gemini free tier is not charged); see CLAUDE.md.
"""

from dataclasses import dataclass

MTOK = 1_000_000


def _ceil_div(n: int, d: int) -> int:
    return -(-n // d)


@dataclass(frozen=True, slots=True)
class Prices:
    """List price in integer micro-USD per million tokens."""

    in_per_mtok: int
    out_per_mtok: int

    def __post_init__(self) -> None:
        for name in ("in_per_mtok", "out_per_mtok"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative int, got {value!r}")


def cost_micro(tokens_in: int, tokens_out: int, prices: Prices) -> int:
    """Exact cost of a finished call, rounded up to the next whole micro-USD."""
    if tokens_in < 0 or tokens_out < 0:
        raise ValueError("token counts must be non-negative")
    return _ceil_div(tokens_in * prices.in_per_mtok + tokens_out * prices.out_per_mtok, MTOK)


def estimate_input_tokens(chars: int) -> int:
    """Rough input size: chars / 4 rounded up, plus a 10% margin (rounded up)."""
    if chars < 0:
        raise ValueError("chars must be non-negative")
    base = _ceil_div(chars, 4)
    return base + _ceil_div(base, 10)


def estimate_max_micro(input_chars: int, max_output_tokens: int, prices: Prices) -> int:
    """Worst-case cost of a call before it runs (phase B reserves this much)."""
    if max_output_tokens < 0:
        raise ValueError("max_output_tokens must be non-negative")
    return cost_micro(estimate_input_tokens(input_chars), max_output_tokens, prices)
