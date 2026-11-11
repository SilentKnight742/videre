"""Plan validation and planner prompts (SPEC-A2 section 5). Pure functions, no I/O."""

import json
from typing import Any, NamedTuple

import pytest

from videre.agent.llm import LLMResponse
from videre.agent.planner import (
    PLAN_SCHEMA,
    PlanInvalid,
    build_plan_request,
    build_repair_request,
    read_plan,
    validate_plan,
)


class Tool(NamedTuple):
    name: str
    description: str


TOOLS = [
    Tool("write_file", "Write a text file into the run workspace."),
    Tool("check_domain", "Mock: says whether a domain name is free."),
]


def task(key: str, *deps: str, subtasks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "key": key,
        "title": f"Task {key}",
        "objective": f"{key} is finished.",
        "depends_on": list(deps),
    }
    if subtasks is not None:
        out["subtasks"] = subtasks
    return out


def plan(*tasks: dict[str, Any], title: str = "Flower shop site") -> dict[str, Any]:
    return {"title": title, "tasks": list(tasks)}


def errors_of(raw: Any) -> list[str]:
    with pytest.raises(PlanInvalid) as caught:
        validate_plan(raw)
    return caught.value.errors


def has(errors: list[str], *needles: str) -> bool:
    return any(all(n in e for n in needles) for e in errors)


# ---- valid plans --------------------------------------------------------------------------------


def test_a_diamond_with_a_composite_task_is_valid() -> None:
    result = validate_plan(
        plan(
            task("t1"),
            task("t2", "t1", subtasks=[task("t2a"), task("t2b", "t2a")]),
            task("t3", "t1"),
            task("t4", "t2", "t3"),
        )
    )
    assert result.title == "Flower shop site"
    assert [t.key for t in result.tasks] == ["t1", "t2", "t3", "t4"]
    assert result.tasks[3].depends_on == ("t2", "t3")
    assert [(s.key, s.depends_on) for s in result.tasks[1].subtasks] == [
        ("t2a", ()),
        ("t2b", ("t2a",)),
    ]
    assert result.tasks[0].subtasks == ()


def test_siblings_come_back_in_topological_order_keeping_the_given_order() -> None:
    result = validate_plan(
        plan(
            task("build", "copy", "style"),
            task("style"),
            task("copy"),
            task("extra"),
            task("t2", subtasks=[task("b", "a"), task("c"), task("a")]),
        )
    )
    assert [t.key for t in result.tasks] == ["style", "copy", "build", "extra", "t2"]
    assert [s.key for s in result.tasks[4].subtasks] == ["c", "a", "b"]


def test_missing_optional_lists_and_duplicate_dependencies_are_tolerated() -> None:
    t3 = task("t3", "t1", "t1")
    t2 = task("t2")
    del t2["depends_on"]
    t2["subtasks"] = None
    result = validate_plan(plan(task("t1"), t2, t3))
    assert result.tasks[1].depends_on == ()
    assert result.tasks[2].depends_on == ("t1",)


def test_over_long_titles_are_trimmed_not_rejected() -> None:
    long = task("t1")
    long["title"] = "  word " * 30
    result = validate_plan(plan(long, task("t2"), task("t3"), title="x" * 200))
    assert len(result.tasks[0].title) == 60
    assert result.tasks[0].title.startswith("word word")
    assert result.tasks[0].title.endswith("…")
    assert len(result.title) == 60


def test_bounds_are_inclusive() -> None:
    assert len(validate_plan(plan(*(task(f"t{i}") for i in range(3)))).tasks) == 3
    assert len(validate_plan(plan(*(task(f"t{i}") for i in range(7)))).tasks) == 7
    four = [task(f"s{i}") for i in range(4)]
    assert len(validate_plan(plan(task("a", subtasks=four), task("b"), task("c"))).tasks) == 3


# ---- invalid plans ------------------------------------------------------------------------------


def test_cycle() -> None:
    errors = errors_of(plan(task("t1", "t3"), task("t2", "t1"), task("t3", "t2"), task("t4", "t3")))
    # t4 only waits on the cycle; it is not part of it
    assert errors == ["dependency cycle among: t1, t2, t3"]


def test_cycle_between_subtasks_and_self_dependency() -> None:
    errors = errors_of(
        plan(
            task("t1", subtasks=[task("a", "b"), task("b", "a")]),
            task("t2", "t2"),
            task("t3"),
        )
    )
    assert has(errors, "dependency cycle among: a, b")
    assert has(errors, "task 't2'", "depends on itself")
    assert len(errors) == 2


def test_unknown_dependency() -> None:
    errors = errors_of(plan(task("t1"), task("t2", "t9"), task("t3")))
    assert errors == ["task 't2': depends_on names an unknown key 't9'"]


def test_cross_level_dependencies() -> None:
    errors = errors_of(
        plan(
            task("t1", subtasks=[task("t1a", "t2"), task("t1b", "t2a")]),  # uncle, cousin
            task("t2", subtasks=[task("t2a"), task("t2b", "t2")]),  # own parent
            task("t3", "t1a"),  # nephew
        )
    )
    for key, dep in (("t1a", "t2"), ("t1b", "t2a"), ("t2b", "t2"), ("t3", "t1a")):
        assert has(errors, f"task '{key}'", f"'{dep}' is not a sibling")
    assert len(errors) == 4


@pytest.mark.parametrize("count", [0, 2, 8])
def test_top_level_bounds(count: int) -> None:
    raw = plan(*(task(f"t{i}") for i in range(count)))
    assert errors_of(raw) == [f"tasks: need 3 to 7 top-level tasks, got {count}"]


def test_subtask_bounds_and_depth() -> None:
    five = [task(f"s{i}") for i in range(5)]
    assert errors_of(plan(task("a", subtasks=five), task("b"), task("c"))) == [
        "task 'a': at most 4 subtasks allowed, got 5"
    ]
    deep = task("a", subtasks=[task("a1", subtasks=[task("a1x")])])
    assert errors_of(plan(deep, task("b"), task("c"))) == [
        "task 'a1': a subtask cannot have subtasks (two levels at most)"
    ]


def test_duplicate_keys_anywhere_in_the_plan() -> None:
    same_level = errors_of(plan(task("t1"), task("t1"), task("t3")))
    assert has(same_level, "task 't1'", "duplicate key")
    across_levels = errors_of(plan(task("t1", subtasks=[task("t2")]), task("t2"), task("t3")))
    assert has(across_levels, "task 't2'", "duplicate key")
    cousins = errors_of(
        plan(task("t1", subtasks=[task("a")]), task("t2", subtasks=[task("a")]), task("t3"))
    )
    assert has(cousins, "task 'a'", "duplicate key")


def test_empty_keys_titles_and_objectives() -> None:
    blank = task("t1")
    blank.update(title="   ", objective="")
    nokey = task("")
    wrong = task("t3")
    wrong.update(title=7, objective=None)
    errors = errors_of(plan(blank, nokey, wrong, title=" "))
    assert has(errors, "title: the run title")
    assert has(errors, "task 't1'", "title must be a non-empty string")
    assert has(errors, "task 't1'", "objective must be a non-empty string")
    assert has(errors, "tasks[1]", "key must be a non-empty string")
    assert has(errors, "task 't3'", "title must be a non-empty string")
    assert has(errors, "task 't3'", "objective must be a non-empty string")
    assert len(errors) == 6


@pytest.mark.parametrize(
    ("raw", "needle"),
    [
        (None, "must be a JSON object"),
        ([], "must be a JSON object"),
        ({"title": "x"}, "tasks: must be a list"),
        ({"title": "x", "tasks": "t1"}, "tasks: must be a list"),
        (plan("t1", task("t2"), task("t3")), "tasks[0]: must be an object"),
        (plan({**task("t1"), "depends_on": "t2"}, task("t2"), task("t3")), "list of keys"),
        (plan({**task("t1"), "depends_on": [1]}, task("t2"), task("t3")), "list of keys"),
        (plan({**task("t1"), "subtasks": {}}, task("t2"), task("t3")), "subtasks must be a list"),
    ],
)
def test_wrong_shapes(raw: Any, needle: str) -> None:
    assert has(errors_of(raw), needle)


def test_every_problem_is_reported_at_once_but_the_list_is_capped() -> None:
    errors = errors_of(plan(task("t1", "nope"), task("t1")))
    assert has(errors, "need 3 to 7")
    assert has(errors, "duplicate key")
    assert has(errors, "unknown key 'nope'")
    noisy = plan(*(task(f"t{i}", *(f"x{j}" for j in range(5))) for i in range(7)))
    assert len(errors_of(noisy)) == 20


# ---- reading a model response -------------------------------------------------------------------


def response(text: str, parsed: dict[str, Any] | None, finish: str = "STOP") -> LLMResponse:
    return LLMResponse(
        text=text,
        parsed=parsed,
        tokens_in=1,
        tokens_out=1,
        latency_ms=1,
        model="m",
        finish_reason=finish,
    )


def test_read_plan_rejects_truncated_and_non_json_answers() -> None:
    good = plan(task("t1"), task("t2"), task("t3"))
    assert len(read_plan(response("{}", good)).tasks) == 3
    with pytest.raises(PlanInvalid, match="cut off at the output limit"):
        read_plan(response("{}", good, finish="MAX_TOKENS"))  # even if it happens to parse
    with pytest.raises(PlanInvalid, match=r"not a JSON object \(finish reason: SAFETY\)"):
        read_plan(response("", None, finish="SAFETY"))
    with pytest.raises(PlanInvalid, match="not a JSON object"):
        read_plan(response("[1, 2]", None))


def test_read_plan_parses_the_text_when_the_provider_did_not() -> None:
    good = plan(task("t1"), task("t2"), task("t3"))
    assert len(read_plan(response(json.dumps(good), None)).tasks) == 3


# ---- prompts ------------------------------------------------------------------------------------


def test_plan_request_states_the_rules_and_lists_only_the_given_tools() -> None:
    req = build_plan_request("build a website for my flower shop", TOOLS, 8192)
    assert req.messages[0].text == "build a website for my flower shop"
    assert [m.role for m in req.messages] == ["user"]
    assert req.max_output_tokens == 8192
    assert req.response_schema == PLAN_SCHEMA
    for needle in (
        "  - write_file: Write a text file into the run workspace.",
        "  - check_domain: Mock: says whether a domain name is free.",
        "and nothing else",
        "There is no internet",
        "3 to 7 top-level tasks",
        "up to 4 subtasks",
        "at most 60 characters",
        "The last top-level task must assemble the final deliverable",
        "one index.html with inline CSS and no external assets",
        "Keep tasks concrete",
    ):
        assert needle in req.system, needle
    assert "read_file" not in req.system
    assert "(no tools are available)" in build_plan_request("x", [], 100).system


def test_plan_schema_mirrors_the_bounds_and_stops_at_two_levels() -> None:
    tasks = PLAN_SCHEMA["properties"]["tasks"]
    assert (tasks["minItems"], tasks["maxItems"]) == (3, 7)
    sub = tasks["items"]["properties"]["subtasks"]
    assert sub["maxItems"] == 4
    assert "subtasks" not in sub["items"]["properties"]
    assert tasks["items"]["required"] == ["key", "title", "objective", "depends_on"]


def test_repair_request_shows_the_rejected_answer_and_the_errors() -> None:
    first = build_plan_request("build a site", TOOLS, 8192)
    repair = build_repair_request(first, '{"title": "x"}', ["tasks: must be a list", "other"])
    assert [m.role for m in repair.messages] == ["user", "model", "user"]
    assert repair.messages[1].text == '{"title": "x"}'
    assert "- tasks: must be a list\n- other" in repair.messages[2].text
    assert (repair.system, repair.response_schema) == (first.system, first.response_schema)
    assert first.messages == [repair.messages[0]]  # the first request is untouched


def test_repair_request_without_a_previous_answer_stays_one_user_message() -> None:
    first = build_plan_request("build a site", TOOLS, 8192)
    repair = build_repair_request(first, "  ", ["the answer was not a JSON object"])
    assert [m.role for m in repair.messages] == ["user"]
    assert repair.messages[0].text.startswith("build a site\n\n")
    assert "- the answer was not a JSON object" in repair.messages[0].text
