"""Status vocabularies and the node transition table (SPEC-A2 sections 1-2, ADR 0005).

`storage/schema.sql` CHECK constraints mirror these values.
"""

from enum import StrEnum


class RunStatus(StrEnum):
    CREATED = "created"
    AWAITING_APPROVAL = "awaiting_approval"
    FUNDED = "funded"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CLOSED = "closed"


class NodeKind(StrEnum):
    TASK = "task"  # composite (has child tasks) or leaf (has steps); max depth 3
    STEP = "step"  # atom: one request, one response; never paused midway


class StepType(StrEnum):
    LLM = "llm"
    TOOL = "tool"


class NodeStatus(StrEnum):
    BLOCKED = "blocked"
    WAITING_FUNDS = "waiting_funds"
    READY = "ready"
    RUNNING = "running"
    PAUSED_BUDGET = "paused_budget"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


_S = NodeStatus

# Allowed node status changes. Terminal states (done, failed, cancelled) have no way out.
TRANSITIONS: dict[NodeStatus, frozenset[NodeStatus]] = {
    _S.BLOCKED: frozenset({_S.READY, _S.CANCELLED}),
    _S.WAITING_FUNDS: frozenset({_S.READY, _S.CANCELLED}),
    _S.READY: frozenset({_S.RUNNING, _S.CANCELLED}),
    _S.RUNNING: frozenset({_S.DONE, _S.FAILED, _S.PAUSED_BUDGET, _S.CANCELLED}),
    _S.PAUSED_BUDGET: frozenset({_S.RUNNING, _S.CANCELLED}),
    _S.DONE: frozenset(),
    _S.FAILED: frozenset(),
    _S.CANCELLED: frozenset(),
}

TERMINAL: frozenset[NodeStatus] = frozenset(s for s, nxt in TRANSITIONS.items() if not nxt)

MAX_TASK_DEPTH = 3  # root task = depth 0; a step sits one level below its task


def can_transition(old: NodeStatus | str, new: NodeStatus | str) -> bool:
    return NodeStatus(new) in TRANSITIONS[NodeStatus(old)]


def check_transition(old: NodeStatus | str, new: NodeStatus | str) -> None:
    """Raise ValueError for a status change the table does not allow."""
    if not can_transition(old, new):
        raise ValueError(f"illegal node transition {old} -> {new}")


class LedgerType(StrEnum):
    FUND = "fund"
    UNFUND = "unfund"
    RESERVE = "reserve"
    RELEASE = "release"
    SPEND = "spend"
    CAPTURE = "capture"
    VOID = "void"
    ROUNDING = "rounding"
