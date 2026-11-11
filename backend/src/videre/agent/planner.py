"""The planner: one model call turns the run's prompt into a task tree (SPEC-A2 section 5).

Three parts, top to bottom:
- the plan's shape and `validate_plan`, a pure function;
- the prompt and the repair prompt, also pure;
- `Planner`, which makes the call as an llm step under the root task, repairs an invalid plan
  once, and creates the task nodes through `RunService` so every node emits `node.created`.

There is no scripted fallback: a plan that is still invalid after the repair fails the run.
"""

import heapq
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from videre.agent.llm.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    Message,
    ProviderError,
    parse_json_object,
    render_request,
)
from videre.domain.cost import Prices, cost_micro
from videre.domain.events import clip
from videre.domain.states import NodeKind, NodeStatus, RunStatus, StepType
from videre.services.runs import EXECUTOR, PLANNER, RunService

MIN_TASKS = 3
MAX_TASKS = 7
MAX_SUBTASKS = 4
TASK_TITLE_MAX = 60

PLAN_STEP_TITLE = "Plan the work"
REPAIR_STEP_TITLE = "Repair plan"
PLAN_TURN = 0  # the repair belongs to the same turn, as in the executor (SPEC-A2 section 6)

ERROR_MAX = 500  # an error kept on a node or a run, and carried by its event
MAX_ERRORS = 20  # validation errors reported for one plan


# ---- the plan and its validation ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlannedTask:
    key: str
    title: str
    objective: str
    depends_on: tuple[str, ...] = ()
    subtasks: tuple["PlannedTask", ...] = ()


@dataclass(frozen=True, slots=True)
class Plan:
    """A validated plan. Every sibling group is in topological order."""

    title: str
    tasks: tuple[PlannedTask, ...]


class PlanInvalid(ValueError):
    """The model's plan broke the rules; `errors` says how, one line per problem."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


@dataclass
class _Draft:
    """A task as the model wrote it, before its dependencies are checked."""

    label: str
    key: str
    title: str
    objective: str
    depends_on: list[str]
    children: list["_Draft"] = field(default_factory=list)
    child_group: str = ""  # names the sibling group its children form


def _one_line(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _string_list(value: Any) -> list[str] | None:
    """`value` as a de-duplicated list of strings; None if it is not a list of strings."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None
    return list(dict.fromkeys(v.strip() for v in value))


def _read_group(
    items: list[Any], where: str, group: str, top: bool, owner: dict[str, str], errors: list[str]
) -> list[_Draft]:
    """Read one sibling group. `owner` maps every key seen so far to the group it belongs to."""
    drafts: list[_Draft] = []
    for i, item in enumerate(items):
        label = f"{where}[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{label}: must be an object")
            continue
        key = _one_line(item.get("key"))
        if not key:
            errors.append(f"{label}: key must be a non-empty string")
        else:
            label = f"task '{key}'"
            if key in owner:
                errors.append(f"{label}: duplicate key (keys must be unique in the whole plan)")
            else:
                owner[key] = group
        title = _one_line(item.get("title"))
        if not title:
            errors.append(f"{label}: title must be a non-empty string")
        objective = item.get("objective")
        objective = objective.strip() if isinstance(objective, str) else ""
        if not objective:
            errors.append(f"{label}: objective must be a non-empty string")
        depends_on = _string_list(item.get("depends_on"))
        if depends_on is None:
            errors.append(f"{label}: depends_on must be a list of keys")
        draft = _Draft(label, key, clip(title, TASK_TITLE_MAX) or "", objective, depends_on or [])
        subtasks = item.get("subtasks")
        if subtasks is None:
            subtasks = []
        if not isinstance(subtasks, list):
            errors.append(f"{label}: subtasks must be a list")
        elif not top:
            if subtasks:
                errors.append(f"{label}: a subtask cannot have subtasks (two levels at most)")
        else:
            if len(subtasks) > MAX_SUBTASKS:
                errors.append(
                    f"{label}: at most {MAX_SUBTASKS} subtasks allowed, got {len(subtasks)}"
                )
            draft.child_group = f"{where}[{i}]"
            draft.children = _read_group(
                subtasks, f"{label}.subtasks", draft.child_group, False, owner, errors
            )
        drafts.append(draft)
    return drafts


def _topological(keys: list[str], deps: dict[str, list[str]]) -> tuple[list[str], list[str]]:
    """Kahn's algorithm, keeping the given order wherever the dependencies allow it.

    Returns (order, cycle): `cycle` holds the keys that sit on a dependency cycle (or between
    two cycles) and is empty when the group is a DAG.
    """
    position = {k: i for i, k in enumerate(keys)}
    waiting = {k: len(deps[k]) for k in keys}
    dependents: dict[str, list[str]] = {k: [] for k in keys}
    for k in keys:
        for d in deps[k]:
            dependents[d].append(k)
    ready = [position[k] for k in keys if waiting[k] == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        k = keys[heapq.heappop(ready)]
        order.append(k)
        for m in dependents[k]:
            waiting[m] -= 1
            if waiting[m] == 0:
                heapq.heappush(ready, position[m])
    stuck = set(keys) - set(order)
    # Tasks that only wait on a cycle are not part of it: peel them off from the far end.
    while True:
        tail = {k for k in stuck if not any(m in stuck for m in dependents[k])}
        if not tail:
            break
        stuck -= tail
    return order, [k for k in keys if k in stuck]


def _check_group(
    drafts: list[_Draft], group: str, owner: dict[str, str], errors: list[str]
) -> list[_Draft]:
    """Check one sibling group's dependencies; return it in topological order if it is a DAG."""
    by_key = {d.key: d for d in drafts if d.key}
    deps: dict[str, list[str]] = {k: [] for k in by_key}
    for d in drafts:
        for dep in d.depends_on:
            if dep == d.key:
                errors.append(f"{d.label}: depends on itself")
            elif dep not in owner:
                errors.append(f"{d.label}: depends_on names an unknown key '{dep}'")
            elif owner[dep] != group:
                errors.append(
                    f"{d.label}: depends_on '{dep}' is not a sibling"
                    " (a task may only depend on tasks with the same parent)"
                )
            elif d.key in deps and dep not in deps[d.key]:
                deps[d.key].append(dep)
    order, cycle = _topological(list(by_key), deps)
    if cycle:
        errors.append("dependency cycle among: " + ", ".join(cycle))
    return [by_key[k] for k in order]


def validate_plan(raw: Any) -> Plan:
    """Turn the model's JSON into a `Plan`, or raise `PlanInvalid` listing every problem.

    Rules (SPEC-A2 section 5): a run title; 3-7 top-level tasks, each with 0-4 subtasks and
    nothing deeper; keys unique across the whole plan; non-empty titles and objectives;
    `depends_on` names siblings only; each sibling group is acyclic. Over-long titles are cut
    to 60 characters rather than rejected.
    """
    if not isinstance(raw, dict):
        raise PlanInvalid(["the plan must be a JSON object with 'title' and 'tasks'"])
    errors: list[str] = []
    title = _one_line(raw.get("title"))
    if not title:
        errors.append("title: the run title must be a non-empty string")
    items = raw.get("tasks")
    if not isinstance(items, list):
        raise PlanInvalid([*errors, "tasks: must be a list"])
    if not MIN_TASKS <= len(items) <= MAX_TASKS:
        errors.append(f"tasks: need {MIN_TASKS} to {MAX_TASKS} top-level tasks, got {len(items)}")
    owner: dict[str, str] = {}
    drafts = _read_group(items, "tasks", "", True, owner, errors)
    for d in drafts:
        d.children = _check_group(d.children, d.child_group, owner, errors)
    ordered = _check_group(drafts, "", owner, errors)
    if errors:
        raise PlanInvalid(errors[:MAX_ERRORS])
    return Plan(
        title=clip(title, TASK_TITLE_MAX) or "",
        tasks=tuple(
            PlannedTask(
                key=d.key,
                title=d.title,
                objective=d.objective,
                depends_on=tuple(d.depends_on),
                subtasks=tuple(
                    PlannedTask(c.key, c.title, c.objective, tuple(c.depends_on))
                    for c in d.children
                ),
            )
            for d in ordered
        ),
    )


# ---- prompts ------------------------------------------------------------------------------------


class ToolDescription(Protocol):
    """What the planner needs to know about a tool (the registry's tools satisfy this)."""

    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...


def _task_schema(*, with_subtasks: bool) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "key": {"type": "string", "description": "Unique in the whole plan, e.g. t1, t2, t1a."},
        "title": {"type": "string", "description": f"At most {TASK_TITLE_MAX} characters."},
        "objective": {
            "type": "string",
            "description": "One sentence that can be checked when the task is done.",
        },
        "depends_on": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Keys of sibling tasks whose results this task needs. Often empty.",
        },
    }
    if with_subtasks:
        properties["subtasks"] = {
            "type": "array",
            "maxItems": MAX_SUBTASKS,
            "items": _task_schema(with_subtasks=False),
            "description": "Optional. Leave empty unless the task splits into separate parts.",
        }
    return {
        "type": "object",
        "properties": properties,
        "required": ["key", "title", "objective", "depends_on"],
    }


PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Short name for the whole run."},
        "tasks": {
            "type": "array",
            "minItems": MIN_TASKS,
            "maxItems": MAX_TASKS,
            "items": _task_schema(with_subtasks=True),
        },
    },
    "required": ["title", "tasks"],
}

_SYSTEM = """\
You are the planner of an autonomous agent. Break the user's request into a small plan of \
concrete tasks. You only plan: an executor carries out each task afterwards, one at a time or \
in parallel, and gets the results of the tasks it depends on.

What the executor can do
- It can call these tools and nothing else:
{tools}
- There is no internet: no web search, no downloads, no external APIs, no remote images, \
fonts or scripts. Everything must be produced from the request and the executor's own writing.

Rules for the plan
- {min_tasks} to {max_tasks} top-level tasks. A task may have up to {max_subtasks} subtasks; \
a subtask has no subtasks of its own. Use subtasks only when a task really has separate parts.
- Every task and subtask has a key that is unique in the whole plan (t1, t2, t1a, ...).
- depends_on lists keys of siblings only: a top-level task depends on top-level tasks, a \
subtask on subtasks of the same task. Add a dependency only when the result is needed. No cycles.
- title: at most {title_max} characters. objective: one sentence that can be checked once the \
task is done.
- Keep tasks concrete: each one must be finishable with the tools above in a few steps. Do not \
plan web research, deployment, hosting, or anything that needs a person.
- The last top-level task must assemble the final deliverable and depend on the tasks whose \
results it uses. For a website, that task writes one index.html with inline CSS and no \
external assets.
- The top-level "title" is a short name for the whole run.

Reply with JSON only, matching the response schema."""


def planner_system_prompt(tools: Sequence[ToolDescription]) -> str:
    listed = "\n".join(f"  - {t.name}: {t.description}" for t in tools)
    return _SYSTEM.format(
        tools=listed or "  (no tools are available)",
        min_tasks=MIN_TASKS,
        max_tasks=MAX_TASKS,
        max_subtasks=MAX_SUBTASKS,
        title_max=TASK_TITLE_MAX,
    )


def build_plan_request(
    prompt: str, tools: Sequence[ToolDescription], max_output_tokens: int
) -> LLMRequest:
    return LLMRequest(
        system=planner_system_prompt(tools),
        messages=[Message(role="user", text=prompt)],
        response_schema=PLAN_SCHEMA,
        max_output_tokens=max_output_tokens,
    )


def build_repair_request(request: LLMRequest, previous: str, errors: Sequence[str]) -> LLMRequest:
    """The same request, plus the rejected answer and why it was rejected."""
    problems = "\n".join(f"- {e}" for e in errors)
    ask = (
        f"That plan was rejected:\n{problems}\n\n"
        "Return the complete corrected plan as JSON matching the response schema. Keep it short."
    )
    if previous.strip():
        extra = [Message(role="model", text=previous), Message(role="user", text=ask)]
        return request.model_copy(update={"messages": [*request.messages, *extra]})
    # Nothing came back (blocked or empty): a model turn with no text is not a valid message.
    first, *rest = request.messages
    merged = Message(role="user", text=f"{first.text}\n\n{ask}")
    return request.model_copy(update={"messages": [merged, *rest]})


def read_plan(resp: LLMResponse) -> Plan:
    """The plan in a model response, or `PlanInvalid`. A truncated answer counts as invalid."""
    if resp.finish_reason == "MAX_TOKENS":
        raise PlanInvalid(["the answer was cut off at the output limit; return a shorter plan"])
    parsed = resp.parsed if resp.parsed is not None else parse_json_object(resp.text)
    if parsed is None:
        raise PlanInvalid(
            [f"the answer was not a JSON object (finish reason: {resp.finish_reason})"]
        )
    return validate_plan(parsed)


# ---- the planner --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Attempt:
    plan: Plan | None
    errors: list[str]
    text: str


class Planner:
    """Plans a run: an llm step under the root task, then the task nodes.

    The root task and the run keep their status on success (the executor starts them). On
    failure the run becomes `failed` with the reason in `runs.error`, and no task is created.
    """

    def __init__(
        self,
        runs: RunService,
        provider: LLMProvider,
        *,
        tools: Sequence[ToolDescription],
        prices: Prices,
        max_output_tokens: int,
    ) -> None:
        self.runs = runs
        self.provider = provider
        self.tools = tuple(tools)
        self.prices = prices
        self.max_output_tokens = max_output_tokens

    async def plan(self, run_id: str) -> Plan | None:
        """Plan `run_id` and create its tasks. Returns None when the run was failed instead."""
        prompt = await self.runs.prompt(run_id)
        root = await self.runs.root_id(run_id)
        request = build_plan_request(prompt, self.tools, self.max_output_tokens)
        try:
            attempt = await self._attempt(run_id, root, PLAN_STEP_TITLE, request)
            if attempt.plan is None:
                repair = build_repair_request(request, attempt.text, attempt.errors)
                attempt = await self._attempt(run_id, root, REPAIR_STEP_TITLE, repair)
        except ProviderError as exc:
            await self._fail_run(run_id, f"planner: model call failed: {exc}")
            return None
        if attempt.plan is None:
            await self._fail_run(
                run_id, "planner: plan still invalid after one repair: " + "; ".join(attempt.errors)
            )
            return None
        await self.runs.set_run_title(run_id, attempt.plan.title)
        await self._create_tasks(run_id, root, attempt.plan.tasks)
        return attempt.plan

    async def _attempt(self, run_id: str, root: str, title: str, request: LLMRequest) -> _Attempt:
        """One model call as an llm step: record what it cost and said, then judge the plan."""
        runs = self.runs
        step = await runs.add_node(
            run_id,
            NodeKind.STEP,
            title,
            parent_id=root,
            step_type=StepType.LLM,
            actor=PLANNER,
            turn=PLAN_TURN,
            status=NodeStatus.RUNNING,
        )
        try:
            resp = await self.provider.generate(request)
        except ProviderError as exc:
            await runs.set_node_status(
                step, NodeStatus.FAILED, reason=clip(f"model call failed: {exc}", ERROR_MAX)
            )
            raise
        try:
            plan: Plan | None = read_plan(resp)
            errors: list[str] = []
            preview = f"{len(plan.tasks)} tasks: " + ", ".join(t.title for t in plan.tasks)
        except PlanInvalid as exc:
            plan, errors = None, exc.errors
            preview = "invalid plan: " + "; ".join(errors)
        if resp.thought_summary:
            await runs.record_thought(step, resp.thought_summary)
        await runs.record_io(
            step,
            preview=preview,
            tokens_in=resp.tokens_in,
            tokens_out=resp.tokens_out,
            latency_ms=resp.latency_ms,
            cached=resp.cached,
            model=resp.model,
            prompt=render_request(request),
            response=resp.text,
            error="\n".join(errors) or None,
        )
        # Metered at list price whether or not the plan is usable, and when served from cache.
        await runs.record_cost(step, cost_micro(resp.tokens_in, resp.tokens_out, self.prices))
        if plan is None:
            await runs.set_node_status(step, NodeStatus.FAILED, reason=clip(preview, ERROR_MAX))
        else:
            await runs.set_node_status(step, NodeStatus.DONE)
        return _Attempt(plan, errors, resp.text)

    async def _fail_run(self, run_id: str, error: str) -> None:
        await self.runs.set_run_status(run_id, RunStatus.FAILED, error=clip(error, ERROR_MAX))

    async def _create_tasks(
        self, run_id: str, parent_id: str, tasks: Sequence[PlannedTask]
    ) -> None:
        """Create one sibling group (already in topological order), then each task's subtasks.

        `add_node` gives a task with dependencies `blocked` and one without `ready`.
        """
        ids: dict[str, str] = {}
        for task in tasks:
            ids[task.key] = await self.runs.add_node(
                run_id,
                NodeKind.TASK,
                task.title,
                parent_id=parent_id,
                actor=EXECUTOR,
                objective=task.objective,
                depends_on=[ids[dep] for dep in task.depends_on],
            )
            await self._create_tasks(run_id, ids[task.key], task.subtasks)
