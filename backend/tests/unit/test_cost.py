import pytest

from videre.agent.llm import LLMRequest, Message, estimate_request_max_micro
from videre.domain.cost import Prices, cost_micro, estimate_input_tokens, estimate_max_micro

FLASH = Prices(in_per_mtok=750_000, out_per_mtok=3_750_000)


def test_cost_is_exact_when_it_divides() -> None:
    # 1M in at $0.75 + 1M out at $3.75
    assert cost_micro(1_000_000, 1_000_000, FLASH) == 4_500_000


def test_cost_rounds_up_to_the_next_micro() -> None:
    # 1 input token = 0.75 micro -> 1; 3 = 2.25 -> 3
    assert cost_micro(1, 0, FLASH) == 1
    assert cost_micro(3, 0, FLASH) == 3
    assert cost_micro(4, 0, FLASH) == 3
    assert cost_micro(0, 1, FLASH) == 4  # 3.75 -> 4


def test_cost_of_nothing_is_zero() -> None:
    assert cost_micro(0, 0, FLASH) == 0
    assert cost_micro(10_000, 10_000, Prices(0, 0)) == 0


def test_cost_is_an_int_and_rejects_negatives() -> None:
    assert isinstance(cost_micro(123, 456, FLASH), int)
    with pytest.raises(ValueError):
        cost_micro(-1, 0, FLASH)


def test_prices_must_be_non_negative_ints() -> None:
    with pytest.raises(ValueError):
        Prices(-1, 0)
    with pytest.raises(ValueError):
        Prices(0.5, 0)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("chars", "tokens"),
    [(0, 0), (1, 2), (4, 2), (40, 11), (400, 110), (401, 112)],
)
def test_input_estimate_is_chars_over_4_plus_10_percent(chars: int, tokens: int) -> None:
    assert estimate_input_tokens(chars) == tokens


def test_worst_case_counts_full_output_budget() -> None:
    # 400 chars -> 110 tokens in; 2048 tokens out
    expected = cost_micro(110, 2048, FLASH)
    assert estimate_max_micro(400, 2048, FLASH) == expected
    assert expected >= cost_micro(100, 2048, FLASH)


def test_request_estimate_uses_system_and_messages() -> None:
    req = LLMRequest(
        system="s" * 100,
        messages=[Message(role="user", text="u" * 300)],
        max_output_tokens=2048,
    )
    assert estimate_request_max_micro(req, FLASH) == estimate_max_micro(400, 2048, FLASH)
