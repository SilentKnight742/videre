import pytest

from videre.domain.states import (
    TERMINAL,
    TRANSITIONS,
    NodeStatus,
    can_transition,
    check_transition,
)

S = NodeStatus

ALLOWED = {
    (S.BLOCKED, S.READY),
    (S.BLOCKED, S.CANCELLED),
    (S.WAITING_FUNDS, S.READY),
    (S.WAITING_FUNDS, S.CANCELLED),
    (S.READY, S.RUNNING),
    (S.READY, S.CANCELLED),
    (S.RUNNING, S.DONE),
    (S.RUNNING, S.FAILED),
    (S.RUNNING, S.PAUSED_BUDGET),
    (S.RUNNING, S.CANCELLED),
    (S.PAUSED_BUDGET, S.RUNNING),
    (S.PAUSED_BUDGET, S.CANCELLED),
}


def test_table_covers_every_status() -> None:
    assert set(TRANSITIONS) == set(NodeStatus)


def test_terminal_states() -> None:
    assert {S.DONE, S.FAILED, S.CANCELLED} == TERMINAL


@pytest.mark.parametrize("old", list(NodeStatus))
@pytest.mark.parametrize("new", list(NodeStatus))
def test_exactly_the_spec_transitions_are_allowed(old: NodeStatus, new: NodeStatus) -> None:
    allowed = (old, new) in ALLOWED
    assert can_transition(old, new) is allowed
    if allowed:
        check_transition(old, new)
    else:
        with pytest.raises(ValueError, match="illegal node transition"):
            check_transition(old, new)


def test_accepts_plain_strings() -> None:
    assert can_transition("ready", "running")
    with pytest.raises(ValueError):
        can_transition("ready", "nonsense")
