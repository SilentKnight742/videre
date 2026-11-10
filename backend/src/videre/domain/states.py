"""Status vocabularies. `storage/schema.sql` CHECK constraints mirror these values."""

from enum import StrEnum


class RunStatus(StrEnum):
    CREATED = "created"
    AWAITING_APPROVAL = "awaiting_approval"
    FUNDED = "funded"
    RUNNING = "running"
    CLOSED = "closed"


class NodeKind(StrEnum):
    TASK = "task"
    SUBTASK = "subtask"
    CALL = "call"


class NodeStatus(StrEnum):
    BLOCKED = "blocked"
    WAITING_FUNDS = "waiting_funds"
    READY = "ready"
    RUNNING = "running"
    PAUSED_BUDGET = "paused_budget"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class LedgerType(StrEnum):
    FUND = "fund"
    UNFUND = "unfund"
    RESERVE = "reserve"
    RELEASE = "release"
    SPEND = "spend"
    CAPTURE = "capture"
    VOID = "void"
    ROUNDING = "rounding"
