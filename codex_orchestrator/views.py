"""Pure read models and renderers for CLI visibility commands."""

from __future__ import annotations

import json
import re
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping


_ACTIVE_STATES = frozenset({"running", "waiting_approval", "interrupting"})
_MAX_REPORT_EVENTS = 1000
_EVENT_PAGE_SIZE = 1000
_TOKEN_ALIASES = {
    "input_tokens": ("inputTokens", "input_tokens"),
    "cached_input_tokens": ("cachedInputTokens", "cached_input_tokens"),
    "output_tokens": ("outputTokens", "output_tokens"),
    "reasoning_output_tokens": ("reasoningOutputTokens", "reasoning_output_tokens"),
    "total_tokens": ("totalTokens", "total_tokens"),
}


def _safe_text(value: Any, limit: int | None = None) -> str:
    text = "" if value is None else str(value)
    text = "".join(char if char.isprintable() or char in "\n\t" else " " for char in text)
    if limit is not None and len(text) > limit:
        return text[: max(0, limit - 1)] + "…"
    return text


def _as_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(row) for row in value if isinstance(row, Mapping)]


def _single_line(value: Any, limit: int | None = None) -> str:
    return _safe_text(value, limit).replace("\n", " ").replace("\t", " ")


def _normalise_states(states: Iterable[str] | str | None) -> frozenset[str] | None:
    if states is None:
        return None
    if isinstance(states, str):
        values = (states,)
    else:
        values = states
    result = frozenset(str(state) for state in values if str(state))
    return result


def filter_snapshot(
    snapshot: Mapping[str, Any],
    *,
    task_id: str | None = None,
    workflow_id: str | None = None,
    role: str | None = None,
    states: Iterable[str] | str | None = None,
    active_only: bool = False,
) -> dict[str, Any]:
    """Return an independently filtered snapshot without changing its source."""

    result = dict(snapshot)
    all_tasks = _as_rows(snapshot.get("tasks"))
    selected_states = _normalise_states(states)
    active_states = _ACTIVE_STATES if active_only else None

    def task_matches(task: Mapping[str, Any]) -> bool:
        if task_id is not None and task.get("id") != task_id:
            return False
        if workflow_id is not None and task.get("workflow_id") != workflow_id:
            return False
        if role is not None and task.get("role") != role:
            return False
        status = task.get("status")
        if selected_states is not None and status not in selected_states:
            return False
        if active_states is not None and status not in active_states:
            return False
        return True

    tasks = [task for task in all_tasks if task_matches(task)]
    task_ids = {task.get("id") for task in tasks}
    has_filter = any((task_id is not None, workflow_id is not None, role is not None,
                      selected_states is not None, active_only))
    result["tasks"] = tasks

    agents = _as_rows(snapshot.get("agents"))
    result["agents"] = [agent for agent in agents if not has_filter or agent.get("task_id") in task_ids]

    if "requests" in snapshot:
        requests = _as_rows(snapshot.get("requests"))
        result["requests"] = [request for request in requests
                              if not has_filter or request.get("task_id") in task_ids]
    for key in ("events", "controls"):
        if key not in snapshot:
            continue
        rows = _as_rows(snapshot.get(key))
        result[key] = [row for row in rows
                       if not has_filter or row.get("task_id") in task_ids]

    result["filters"] = {
        "task_id": task_id,
        "workflow_id": workflow_id,
        "role": role,
        "states": sorted(selected_states) if selected_states is not None else None,
        "active_only": active_only,
    }
    return result


def _task_label(task: Mapping[str, Any]) -> str:
    task_id = _single_line(task.get("id") or "unknown", 80)
    title = _single_line(task.get("title") or "(untitled)", 100)
    state = _single_line(task.get("status") or "unknown", 40)
    role = _single_line(task.get("role") or "unknown", 40)
    parts = [f"task {task_id}: {title}", f"state={state}", f"role={role}"]
    model = task.get("next_model") if task.get("status") in {"queued", "blocked"} else task.get("current_model")
    if model:
        parts.append(f"model={_single_line(model, 80)}")
    dependencies = task.get("depends_on")
    if isinstance(dependencies, list) and dependencies:
        parts.append("depends on " + ", ".join(_single_line(item, 80) for item in dependencies))
    workflow_id = task.get("workflow_id")
    workflow_step = task.get("workflow_step")
    if isinstance(workflow_id, str) and workflow_id:
        parts.append(f"workflow={_single_line(workflow_id, 80)}")
    if isinstance(workflow_step, str) and workflow_step:
        parts.append(f"step={_single_line(workflow_step, 60)}")
    return " · ".join(parts)


def _agent_label(agent: Mapping[str, Any]) -> str:
    agent_id = _single_line(agent.get("id") or "unknown", 100)
    nickname = _single_line(agent.get("nickname"), 80)
    role = _single_line(agent.get("role"), 60)
    identity = nickname or role or agent_id
    if identity != agent_id:
        identity = f"{identity} ({agent_id})"
    state = _single_line(agent.get("status") or "unknown", 40)
    activity = _single_line(agent.get("activity"), 180)
    model = f"model={_single_line(agent['model'], 80)}" if agent.get("model") else ""
    return " · ".join(part for part in (f"native {identity}", f"state={state}", model, activity) if part)


def render_tree(snapshot: Mapping[str, Any]) -> str:
    """Render managed task parentage and observed native thread nesting."""

    tasks = _as_rows(snapshot.get("tasks"))
    agents = _as_rows(snapshot.get("agents"))
    lines = [f"Codex Orchestrator · {_single_line(snapshot.get('project') or '(project)', 300)}"]
    runner = snapshot.get("runner") or {}
    lines.append(f"Runner: {_single_line(runner.get('status') or 'not started')} · "
                 f"{_single_line(runner.get('observation') or 'run serve to execute queued work')}")
    lines.append("Dispatch: PAUSED (active work continues)" if snapshot.get("dispatch", {}).get("paused")
                 else "Dispatch: enabled")
    requests = snapshot.get("requests") or []
    if requests:
        lines.append(f"Pending requests: {len(requests)}; use approvals, then respond ID")
    if not tasks:
        lines.append("(no tasks)")

    task_by_id = {task.get("id"): task for task in tasks if isinstance(task.get("id"), str)}
    task_children: dict[str, list[dict[str, Any]]] = defaultdict(list)
    task_roots: list[tuple[dict[str, Any], str | None]] = []
    for task in tasks:
        parent_id = task.get("parent_id")
        if isinstance(parent_id, str) and parent_id in task_by_id and parent_id != task.get("id"):
            task_children[parent_id].append(task)
        else:
            missing = parent_id if isinstance(parent_id, str) and parent_id not in task_by_id else None
            task_roots.append((task, missing))

    native_by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unattached_agents: list[dict[str, Any]] = []
    for agent in agents:
        task_owner = agent.get("task_id")
        if isinstance(task_owner, str) and task_owner in task_by_id:
            native_by_task[task_owner].append(agent)
        else:
            unattached_agents.append(agent)

    shown_tasks: set[str] = set()
    shown_agents: set[str] = set()

    def render_agent(agent: dict[str, Any], prefix: str, last: bool, task_thread_id: str | None,
                     visiting: set[str]) -> None:
        agent_id = agent.get("id")
        if not isinstance(agent_id, str):
            return
        branch = "└─ " if last else "├─ "
        if agent_id in visiting:
            lines.append(prefix + branch + f"native {agent_id} (cycle omitted)")
            return
        if agent_id in shown_agents:
            lines.append(prefix + branch + f"native {agent_id} (already shown)")
            return
        shown_agents.add(agent_id)
        lines.append(prefix + branch + _agent_label(agent))
        next_prefix = prefix + ("   " if last else "│  ")
        nested = [child for child in native_by_task.get(agent.get("task_id"), [])
                  if child.get("parent_thread_id") == agent_id and child.get("id") != agent_id]
        # Agent records carry a single task_id, so nested children are selected
        # by their parent thread and same managed task.
        for index, child in enumerate(nested):
            render_agent(child, next_prefix, index == len(nested) - 1,
                         task_thread_id, visiting | {agent_id})

    def render_task(task: dict[str, Any], prefix: str, last: bool, missing_parent: str | None = None) -> None:
        task_id_value = task.get("id")
        if not isinstance(task_id_value, str):
            return
        branch = "└─ " if last else "├─ "
        if task_id_value in shown_tasks:
            lines.append(prefix + branch + f"task {task_id_value} (cycle/already shown)")
            return
        shown_tasks.add(task_id_value)
        suffix = f" · missing parent={_single_line(missing_parent, 80)}" if missing_parent else ""
        lines.append(prefix + branch + _task_label(task) + suffix)

        task_prefix = prefix + ("   " if last else "│  ")
        for label, value in (("activity", task.get("activity")), ("error", task.get("error"))):
            if value:
                lines.append(task_prefix + f"{label}: {_single_line(value, 240)}")
        if task.get("model_change_pending"):
            lines.append(task_prefix + f"next turn: {_single_line(task.get('next_model'))}/"
                         f"{_single_line(task.get('next_effort'))}")
        if task.get("live_update_status"):
            lines.append(task_prefix + f"live publication: {_single_line(task['live_update_status'])} "
                         f"{_single_line(task.get('live_model'))}")
        root_thread = task.get("thread_id") if isinstance(task.get("thread_id"), str) else None
        native = native_by_task.get(task_id_value, [])
        native_roots = [agent for agent in native
                        if agent.get("parent_thread_id") == root_thread and root_thread is not None]
        for agent in native:
            parent_thread = agent.get("parent_thread_id")
            known_agent_ids = {item.get("id") for item in native_by_task.get(task_id_value, [])}
            if parent_thread not in known_agent_ids and agent not in native_roots:
                native_roots.append(agent)
        # Keep displayed children stable and avoid duplicate roots.
        native_roots = list({agent.get("id"): agent for agent in native_roots}.values())
        for index, agent in enumerate(native_roots):
            parent_thread = agent.get("parent_thread_id")
            render_agent(agent, task_prefix, index == len(native_roots) - 1,
                         root_thread, {parent_thread} if isinstance(parent_thread, str) else set())

        # A cycle among native threads has no root. Render each remaining
        # component as an orphan so every observed agent remains inspectable.
        orphaned_native = [agent for agent in native if agent.get("id") not in shown_agents]
        for index, agent in enumerate(orphaned_native):
            if agent.get("id") in shown_agents:
                continue
            lines.append(task_prefix + f"└─ native parent cycle/orphan: {_single_line(agent.get('parent_thread_id') or '(unknown)', 100)}")
            render_agent(agent, task_prefix + "   ", index == len(orphaned_native) - 1,
                         root_thread, set())

        children = task_children.get(task_id_value, [])
        for index, child in enumerate(children):
            render_task(child, task_prefix, index == len(children) - 1)

    for index, (root, missing_parent) in enumerate(task_roots):
        render_task(root, "", index == len(task_roots) - 1 and not unattached_agents, missing_parent)

    # Orphaned managed parentage and parent cycles have no natural root. Start
    # each remaining component once; the seen sets bound malformed lineages.
    for task in tasks:
        if task.get("id") not in shown_tasks:
            render_task(task, "", True, task.get("parent_id") if isinstance(task.get("parent_id"), str) else None)

    if unattached_agents:
        lines.append("Unattached native agents:")
        for index, agent in enumerate(unattached_agents):
            render_agent(agent, "", index == len(unattached_agents) - 1, None, set())

    return "\n".join(lines)


def _token_counts(value: Any) -> dict[str, int | float]:
    if not isinstance(value, Mapping):
        return {}
    counts: dict[str, int | float] = {}
    for normalized, aliases in _TOKEN_ALIASES.items():
        for alias in aliases:
            count = value.get(alias)
            if isinstance(count, (int, float)) and not isinstance(count, bool):
                counts[normalized] = count
                break
    return counts


def _thread_usage_row(
    *,
    thread_id: Any,
    task_id: Any,
    kind: str,
    role: Any,
    status: Any,
    entity_id: Any,
    entity_name: Any,
    usage: Any,
    parent_thread_id: Any = None,
) -> dict[str, Any]:
    usage_data = usage if isinstance(usage, Mapping) else {}
    last = _token_counts(usage_data.get("last"))
    total = _token_counts(usage_data.get("total"))
    available = bool(last or total)
    missing_reason = None if available else ("thread_not_started" if not isinstance(thread_id, str) else "token_usage_not_reported")
    context_window = usage_data.get("model_context_window")
    if not isinstance(context_window, (int, float)) or isinstance(context_window, bool):
        context_window = None
    row = {
        "thread_id": thread_id if isinstance(thread_id, str) else None,
        "task_id": task_id if isinstance(task_id, str) else None,
        "kind": kind,
        "role": role if isinstance(role, str) else None,
        "status": status if isinstance(status, str) else None,
        "entity_id": entity_id if isinstance(entity_id, str) else None,
        "entity_name": _safe_text(entity_name, 120) if entity_name is not None else None,
        "parent_thread_id": parent_thread_id if isinstance(parent_thread_id, str) else None,
        "usage_available": available,
        "missing_reason": missing_reason,
        "last": last,
        "total": total,
        "model_context_window": context_window,
    }
    return row


def usage_report(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Describe token counters per thread and labeled kind/role subtotals."""

    threads: list[dict[str, Any]] = []
    known_thread_ids: set[str] = set()
    for task in _as_rows(snapshot.get("tasks")):
        thread_id = task.get("thread_id")
        if isinstance(thread_id, str):
            # A managed task is the canonical owner if a duplicate agent row is
            # present for the same thread.
            known_thread_ids.add(thread_id)
        threads.append(_thread_usage_row(
            thread_id=thread_id,
            task_id=task.get("id"),
            kind="managed",
            role=task.get("role"),
            status=task.get("status"),
            entity_id=task.get("id"),
            entity_name=task.get("title"),
            usage=task.get("usage"),
        ))

    for agent in _as_rows(snapshot.get("agents")):
        agent_id = agent.get("id")
        if not isinstance(agent_id, str) or agent_id in known_thread_ids:
            continue
        if agent.get("native") is False:
            continue
        known_thread_ids.add(agent_id)
        threads.append(_thread_usage_row(
            thread_id=agent_id,
            task_id=agent.get("task_id"),
            kind="native",
            role=agent.get("role"),
            status=agent.get("status"),
            entity_id=agent_id,
            entity_name=agent.get("nickname"),
            usage=agent.get("usage"),
            parent_thread_id=agent.get("parent_thread_id"),
        ))

    by_kind: dict[str, dict[str, Any]] = {}
    by_role: dict[str, dict[str, Any]] = {}

    def add_to_group(groups: dict[str, dict[str, Any]], key: str, row: Mapping[str, Any]) -> None:
        stats = groups.setdefault(key, {
            "threads": 0,
            "reported": 0,
            "missing": 0,
            "reported_total_sums": {},
            "metric_coverage": {},
        })
        stats["threads"] += 1
        stats["reported" if row["usage_available"] else "missing"] += 1
        if not row["usage_available"]:
            return
        for metric, count in row["total"].items():
            stats["reported_total_sums"][metric] = stats["reported_total_sums"].get(metric, 0) + count
            stats["metric_coverage"][metric] = stats["metric_coverage"].get(metric, 0) + 1

    for row in threads:
        role_key = row.get("role") or "(unknown)"
        add_to_group(by_kind, row["kind"], row)
        add_to_group(by_role, role_key, row)

    reported = sum(1 for row in threads if row["usage_available"])
    return {
        "aggregation": "reported_thread_counter_sums_by_kind_and_role",
        "note": (
            "Per-kind and per-role subtotals add reported cumulative thread counters. Native and parent-thread "
            "counters may overlap; these are not deduplicated project totals, billing measures, or dollar costs. "
            "Per-thread rows remain the source of truth."
        ),
        "coverage": {
            "threads": len(threads),
            "reported": reported,
            "missing": len(threads) - reported,
            "by_kind": by_kind,
            "by_role": by_role,
        },
        "threads": threads,
    }


def _task_reference(task_id: Any, tasks_by_id: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    reference: dict[str, Any] = {"id": task_id}
    task = tasks_by_id.get(task_id) if isinstance(task_id, str) else None
    if task is None:
        reference["missing"] = True
        return reference
    reference.update({key: task.get(key) for key in ("title", "status", "result")})
    return reference


def _summarise_request(request: Mapping[str, Any]) -> dict[str, Any]:
    summary = {key: request.get(key) for key in (
        "id", "task_id", "method", "run_id", "status", "created_at", "resolved_at",
        "expired_at", "completed_at", "error",
    ) if key in request}
    response = request.get("response")
    if isinstance(response, Mapping):
        if isinstance(response.get("decision"), str):
            summary["decision"] = response["decision"]
        answers = response.get("answers")
        if isinstance(answers, Mapping):
            summary["answered_question_ids"] = sorted(str(key) for key in answers)
    return summary


def _summarise_control(control: Mapping[str, Any]) -> dict[str, Any]:
    return {key: control.get(key) for key in (
        "id", "task_id", "kind", "status", "run_id", "turn_id", "model", "effort",
        "created_at", "completed_at", "error",
    ) if key in control}


def _recent_events(store: Any, task_ids: set[str], *, include_global: bool) -> tuple[list[dict[str, Any]], bool]:
    latest_page = store.events(limit=1)
    if not latest_page:
        return [], False
    watermark = latest_page[-1]["id"]
    cursor = 0
    kept: deque[dict[str, Any]] = deque(maxlen=_MAX_REPORT_EVENTS)
    matched = 0
    while cursor < watermark:
        page = store.events(after=cursor, limit=_EVENT_PAGE_SIZE)
        if not page:
            break
        for event in page:
            event_id = event.get("id")
            if not isinstance(event_id, int):
                continue
            if event_id > watermark:
                cursor = watermark
                break
            cursor = event_id
            owner = event.get("task_id")
            if owner in task_ids or (include_global and owner is None):
                kept.append(event)
                matched += 1
        if cursor >= watermark:
            break
    return list(kept), matched > _MAX_REPORT_EVENTS


def build_report(store: Any, *, task_id: str | None = None, workflow_id: str | None = None) -> dict[str, Any]:
    """Build a bounded, self-contained report from persisted project state."""

    if task_id is not None and workflow_id is not None:
        raise ValueError("Choose a task or workflow scope, not both.")
    if workflow_id is not None:
        store.get_workflow_run(workflow_id)
    snapshot = store.snapshot()
    all_tasks = store.tasks()
    tasks_by_id = {task["id"]: task for task in all_tasks if isinstance(task.get("id"), str)}
    if task_id is not None and task_id not in tasks_by_id:
        raise ValueError(f"Unknown task: {task_id}")
    filtered = filter_snapshot(snapshot, task_id=task_id, workflow_id=workflow_id)
    selected_tasks = filtered.get("tasks", [])
    selected_ids = {task.get("id") for task in selected_tasks if isinstance(task.get("id"), str)}
    dependencies = []
    for task in selected_tasks:
        parent_id = task.get("parent_id")
        dependency_ids = task.get("depends_on") if isinstance(task.get("depends_on"), list) else []
        dependencies.append({
            "task_id": task.get("id"),
            "parent": _task_reference(parent_id, tasks_by_id) if parent_id else None,
            "depends_on": [_task_reference(dependency_id, tasks_by_id) for dependency_id in dependency_ids],
        })

    requests = [_summarise_request(request) for request in store.requests()
                if not selected_ids or request.get("task_id") in selected_ids]
    controls = [_summarise_control(control) for control in store.controls()
                if not selected_ids or control.get("task_id") in selected_ids]
    # In a scoped report with no matching tasks, do not accidentally include
    # unrelated project-wide history or requests.
    scoped = task_id is not None or workflow_id is not None
    if scoped and not selected_ids:
        requests = []
        controls = []
    events, events_truncated = _recent_events(store, selected_ids, include_global=not scoped)
    run_ids = sorted({task.get("run_id") for task in selected_tasks if isinstance(task.get("run_id"), str)})
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project": snapshot.get("project"),
        "scope": {"task_id": task_id, "workflow_id": workflow_id, "run_ids": run_ids},
        "runner": filtered.get("runner"),
        "roles": filtered.get("roles", []),
        "tasks": selected_tasks,
        "agents": filtered.get("agents", []),
        "dependencies": dependencies,
        "requests": requests,
        "controls": controls,
        "usage": usage_report(filtered),
        "events": events,
        "events_truncated": events_truncated,
        "event_limit": _MAX_REPORT_EVENTS,
    }


def _markdown_inline(value: Any) -> str:
    text = _safe_text(value).replace("\r", " ").replace("\n", " ")
    return re.sub(r"([\\`*_{}\[\]()<>#+\-.!|])", r"\\\1", text)


def _markdown_fence(value: Any, *, language: str = "text") -> str:
    text = _safe_text(value)
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{text}\n{fence}"


def _json_fence(value: Any) -> str:
    return _markdown_fence(json.dumps(value, ensure_ascii=False, indent=2, default=str), language="json")


def render_report_markdown(report: Mapping[str, Any]) -> str:
    """Render a report while keeping persisted prompt/result text inside code fences."""

    project = _markdown_inline(report.get("project") or "(project)")
    scope = report.get("scope") if isinstance(report.get("scope"), Mapping) else {}
    lines = ["# Codex Orchestrator run report", "", f"- Project: {project}"]
    generated = report.get("generated_at")
    if generated:
        lines.append(f"- Generated: {_markdown_inline(generated)}")
    if scope.get("task_id"):
        lines.append(f"- Task: {_markdown_inline(scope['task_id'])}")
    if scope.get("workflow_id"):
        lines.append(f"- Workflow: {_markdown_inline(scope['workflow_id'])}")
    run_ids = scope.get("run_ids")
    if isinstance(run_ids, list) and run_ids:
        lines.append(f"- Runner IDs: {', '.join(_markdown_inline(item) for item in run_ids)}")
    lines.extend(["", "## Runner", "", _json_fence(report.get("runner"))])

    tasks = _as_rows(report.get("tasks"))
    lines.extend(["", "## Tasks", ""])
    if not tasks:
        lines.append("(none)")
    for task in tasks:
        task_id_value = task.get("id") or "unknown"
        title = _markdown_inline(task.get("title") or "(untitled)")
        lines.extend([
            f"### {_markdown_inline(task_id_value)} — {title}",
            "",
            f"- State: {_markdown_inline(task.get('status') or 'unknown')}",
            f"- Role: {_markdown_inline(task.get('role') or 'unknown')}",
        ])
        if task.get("workflow_id"):
            lines.append(f"- Workflow: {_markdown_inline(task['workflow_id'])}")
        lines.extend(["", "#### Prompt", "", _markdown_fence(task.get("prompt") or "(no prompt recorded)")])
        result = task.get("result")
        if result:
            lines.extend(["", "#### Result", "", _markdown_fence(result)])
        error = task.get("error")
        if error:
            lines.extend(["", "#### Error", "", _markdown_fence(error)])

    for heading, key in (("Dependencies and parent tasks", "dependencies"),
                         ("Native agents", "agents"), ("Token usage by thread", "usage"),
                         ("Approval and control summaries", "requests")):
        lines.extend(["", f"## {heading}", ""])
        lines.append(_json_fence(report.get(key, [])))
        if key == "requests":
            lines.extend(["", "### Controls", "", _json_fence(report.get("controls", []))])

    lines.extend(["", "## Lifecycle event history", ""])
    lines.append(f"Events shown: {_markdown_inline(len(report.get('events', [])))}; "
                 f"older events omitted: {_markdown_inline(report.get('events_truncated', False))}.")
    lines.extend(["", _json_fence(report.get("events", [])), ""])
    return "\n".join(lines)
