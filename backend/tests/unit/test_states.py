"""The schema's CHECK constraints must list exactly the values in domain/states.py."""

import re
from enum import StrEnum

import pytest

from videre.domain.states import LedgerType, NodeKind, NodeStatus, RunStatus, StepType
from videre.storage.db import SCHEMA


def check_values(table: str, column: str) -> set[str]:
    sql = SCHEMA.read_text(encoding="utf-8")
    body = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", sql, re.S)
    assert body, table
    check = re.search(rf"CHECK \({column} IN \((.*?)\)\)", body.group(1), re.S)
    assert check, f"{table}.{column}"
    return set(re.findall(r"'([a-z_]+)'", check.group(1)))


@pytest.mark.parametrize(
    ("table", "column", "enum"),
    [
        ("runs", "status", RunStatus),
        ("nodes", "kind", NodeKind),
        ("nodes", "step_type", StepType),
        ("nodes", "status", NodeStatus),
        ("ledger", "type", LedgerType),
    ],
)
def test_schema_checks_match_enums(table: str, column: str, enum: type[StrEnum]) -> None:
    assert check_values(table, column) == {member.value for member in enum}
