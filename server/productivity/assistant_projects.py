"""Project actions use the same Work proposal, permission and transaction boundary.

Selectors are resolved locally. A provider proposes names, never account records,
IDs, permissions or versions. Only explicitly accepted project access is used.
"""

from __future__ import annotations

import json
import re
from uuid import uuid4

from fastapi import HTTPException
from pydantic import Field, model_validator

from ..database import get_db
from . import collaboration, projects, scheduling, store
from .assistant_changes import Clarification, TaskFields
from .models import ClosedModel, CreateTask, TaskPriority, UpdateTask, clean_text


class Selector(ClosedModel):
    project_name: str = Field(min_length=1, max_length=100)


class ProjectTaskChange(Selector):
    match_title: str = Field(min_length=1, max_length=200)
    changes: TaskFields


class ProjectTaskCreate(Selector):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=6000)
    priority: TaskPriority = "normal"
    due_at: str | None = Field(default=None, max_length=40)
    timezone: str | None = Field(default=None, max_length=100)


class PlanFields(ClosedModel):
    duration_minutes: int | None = Field(default=None, ge=0, le=100000)
    assignee: str | None = Field(default=None, min_length=1, max_length=100)
    not_before: str | None = Field(default=None, max_length=40)
    milestone: bool | None = None

    @model_validator(mode="after")
    def valid(self):
        scheduling.TaskPlan(
            request_id=str(uuid4()), expected_version=0, **self.model_dump(exclude_unset=True)
        )
        return self


class ProjectTaskPlan(Selector):
    match_title: str = Field(min_length=1, max_length=200)
    changes: PlanFields


class NamedEdge(ClosedModel):
    before_title: str = Field(min_length=1, max_length=200)
    after_title: str = Field(min_length=1, max_length=200)
    lag_minutes: int = Field(default=0, ge=0, le=43200)


class ProjectDependencies(Selector):
    links: list[NamedEdge] = Field(min_length=1, max_length=20)
    remove: bool = False


class ProjectCalendar(Selector):
    config: scheduling.Config


SCHEMAS = {
    "create_project_task": ProjectTaskCreate,
    "update_project_task": ProjectTaskChange,
    "plan_project_task": ProjectTaskPlan,
    "set_project_dependencies": ProjectDependencies,
    "set_project_calendar": ProjectCalendar,
}


def parse(text, zone):
    if len(text) > 400 or any(w in text for w in ("不要", "别", "如果", "例如", "示例", "然后", "；", "\n")):
        return None
    match = re.fullmatch(r"在项目「([^」]+)」中(?:添加|创建)任务「([^」]+)」", text)
    if match:
        return {
            "actions": [
                {
                    "kind": "create_project_task",
                    "fields": {"project_name": match[1], "title": match[2], "timezone": zone},
                }
            ]
        }
    match = re.fullmatch(r"把项目「([^」]+)」的任务「([^」]+)」(工时|负责人)设为(.+)", text)
    if match:
        value = match[4].strip()
        if match[3] == "工时":
            minutes = re.fullmatch(r"(\d{1,6})分钟", value)
            if not minutes or not 1 <= int(minutes[1]) <= 100000:
                return {"clarification": "请明确任务所需的正整数分钟数，里程碑请单独说明。"}
            changes = {"duration_minutes": int(minutes[1])}
        else:
            changes = {"assignee": value.strip('「」“”"')}
        return {
            "actions": [
                {
                    "kind": "plan_project_task",
                    "fields": {"project_name": match[1], "match_title": match[2], "changes": changes},
                }
            ]
        }
    match = re.fullmatch(r"在项目「([^」]+)」中让任务「([^」]+)」完成后开始任务「([^」]+)」", text)
    if match:
        return {
            "actions": [
                {
                    "kind": "set_project_dependencies",
                    "fields": {
                        "project_name": match[1],
                        "links": [{"before_title": match[2], "after_title": match[3]}],
                    },
                }
            ]
        }
    match = re.fullmatch(r"把项目「([^」]+)」的任务「([^」]+)」(标为|设为)(.+)", text)
    if match:
        from .assistant_changes import parse as task_parse

        parsed = task_parse(f"把任务「{match[2]}」{match[3]}{match[4]}", zone)
        if parsed and parsed.get("actions"):
            action = parsed["actions"][0]
            return {
                "actions": [
                    {"kind": "update_project_task", "fields": {**action["fields"], "project_name": match[1]}}
                ]
            }
    return None


def _one(rows, label):
    if not rows:
        raise Clarification(f"没有找到可操作的{label}，请核对名称和项目权限。")
    if len(rows) != 1:
        raise Clarification(f"有多个同名{label}，请先区分完整名称。")
    return rows[0]


def _task(db, parent, name):
    return _one(
        db.execute(
            "SELECT t.* FROM work_tasks t JOIN work_project_tasks p ON p.task_id=t.id JOIN work_spaces s ON s.id=t.workspace_id "
            "WHERE p.project_id=? AND p.owner=? AND s.owner=? AND s.deleted_at IS NULL AND t.title=? LIMIT 2",
            [parent["id"], parent["owner"], parent["owner"], clean_text(name)],
        ).fetchall(),
        "项目任务",
    )


def resolve(actor, kind, fields, zone):
    body = SCHEMAS[kind].model_validate(fields)
    with get_db() as db:
        store._active(db, actor)
        selected = _one(
            db.execute(
                "SELECT DISTINCT p.id FROM work_projects p JOIN users u ON u.username=p.owner LEFT JOIN work_project_members m ON m.project_id=p.id AND m.username=? "
                "WHERE p.name=? AND u.disabled_at IS NULL AND (p.owner=? OR m.role='editor') LIMIT 2",
                [actor, clean_text(body.project_name), actor],
            ).fetchall(),
            "项目",
        )
        parent, role = collaboration.access(db, actor, selected[0], write=True)
        if parent["status"] != "active":
            raise Clarification("项目已结束或归档，请先明确是否重新激活。")
        state = scheduling.state(db, parent["id"])
        target = {
            "id": parent["id"],
            "project_id": parent["id"],
            "project_name": parent["name"],
            "project_version": parent["version"],
            "role": role,
            "membership_version": parent["member_version"],
            "schedule_version": state["version"],
            "timezone": state["config"]["timezone"],
        }
        operation_id = str(uuid4())
        if kind == "create_project_task":
            before = {}
            content = body.model_dump(exclude={"project_name", "timezone"})
            validated = CreateTask(
                request_id=operation_id, timezone=body.timezone or zone, **content
            ).model_dump()
            target["title"] = validated["title"]
        elif kind in {"update_project_task", "plan_project_task"}:
            row = _task(db, parent, body.match_title)
            target.update(id=row["id"], title=row["title"])
            changes = body.changes.model_dump(exclude_unset=True)
            if kind == "update_project_task":
                before = store._task(row)
                validated = UpdateTask(expected_version=row["version"], **changes).model_dump(
                    exclude_unset=True
                )
                if validated.get("due_at"):
                    validated["due_at"] = scheduling.canonical_instant(
                        validated["due_at"], validated.get("timezone", row["timezone"])
                    )
            else:
                old = db.execute(
                    "SELECT * FROM work_project_task_plans WHERE task_id=?", [row["id"]]
                ).fetchone()
                before = {
                    k: old[k] if old else (False if k == "milestone" else None)
                    for k in ("duration_minutes", "assignee", "not_before", "milestone")
                }
                before["milestone"] = bool(before["milestone"])
                if changes.get("milestone") and "duration_minutes" not in changes:
                    changes["duration_minutes"] = 0
                merged = {**before, **changes}
                if (merged["milestone"] and merged["duration_minutes"] != 0) or (
                    not merged["milestone"] and merged["duration_minutes"] == 0
                ):
                    raise Clarification("里程碑工时为0，普通任务需正工时或默认估算。")
                if merged["assignee"] and not scheduling._available(db, parent, merged["assignee"]):
                    raise Clarification("请使用本项目已接受邀请的编辑成员账号，或项目负责人账号。")
                if "not_before" in changes:
                    changes["not_before"] = scheduling.canonical_instant(
                        changes["not_before"], state["config"]["timezone"]
                    )
                validated = scheduling.TaskPlan(
                    request_id=operation_id, expected_version=old["version"] if old else 0, **changes
                ).model_dump(exclude_unset=True)
        elif kind == "set_project_dependencies":
            before = {"links": scheduling.links(db, parent["id"])}
            merged = {(e["predecessor_id"], e["successor_id"]): e for e in before["links"]}
            names = []
            for edge in body.links:
                a, b = _task(db, parent, edge.before_title), _task(db, parent, edge.after_title)
                key = (a["id"], b["id"])
                names.append(
                    {"before_title": a["title"], "after_title": b["title"], "lag_minutes": edge.lag_minutes}
                )
                if body.remove:
                    merged.pop(key, None)
                else:
                    if b["status"] in {"doing", "done"} and a["status"] != "done":
                        raise Clarification("后续任务已开始，不能新增未完成的前置任务。")
                    merged[key] = {
                        "predecessor_id": a["id"],
                        "successor_id": b["id"],
                        "lag_minutes": edge.lag_minutes,
                    }
            try:
                scheduling.planning.graph(
                    [t["id"] for t in scheduling.tasks(db, parent)], list(merged.values())
                )
            except scheduling.planning.PlanError:
                raise Clarification("依赖不能引用自身或形成循环，请调整先后顺序。") from None
            validated = scheduling.Dependencies(
                request_id=operation_id, expected_version=state["version"], links=list(merged.values())
            ).model_dump()
            target.update(title=parent["name"], dependency_changes=names, remove=body.remove)
        else:
            before = {"config": state["config"], "configured": state["configured"]}
            try:
                scheduling.planning.WorkingCalendar(body.config.model_dump())
            except (ValueError, OverflowError):
                raise Clarification("开始时间或工作日历无法计算，请重新核对。") from None
            validated = scheduling.ConfigUpdate(
                request_id=operation_id, expected_version=state["version"], **body.config.model_dump()
            ).model_dump()
            target["title"] = parent["name"]
        meaningful = {k: v for k, v in validated.items() if k not in {"request_id", "expected_version"}}
        compare = before["config"] if kind == "set_project_calendar" and before["configured"] else before
        if all(compare.get(k) == v for k, v in meaningful.items()):
            raise Clarification("当前项目安排已经符合要求，无需重复修改。")
        return {
            "kind": kind,
            "operation_id": operation_id,
            "target": target,
            "before": before,
            "fields": validated,
        }


def check_access(actor, action, db):
    target = action["target"]
    parent, role = collaboration.access(db, actor, target["project_id"], write=True)
    if (
        parent["version"] != target["project_version"]
        or role != target["role"]
        or parent["member_version"] != target["membership_version"]
    ):
        raise HTTPException(409, "项目或成员权限已有变化，请重新整理安排")
    return parent


def preflight(actor, actions, db, *, results=None):
    """Pin external state once; sibling actions then share this single transaction."""
    versions = {}
    for index, action in enumerate(actions):
        if action["kind"] not in SCHEMAS:
            continue
        check_access(actor, action, db)
        project_id = action["target"]["project_id"]
        expected = (
            action["target"]["schedule_version"] if results is None else results[index]["schedule_version"]
        )
        versions[project_id] = max(versions.get(project_id, expected), expected)
    for project_id, expected in versions.items():
        if scheduling.state(db, project_id)["version"] != expected:
            raise HTTPException(409, "项目任务或排程已有后续变化，请重新核对")


def apply(actor, action, db):
    target, fields, kind = action["target"], action["fields"], action["kind"]
    project_id = target["project_id"]
    if kind == "create_project_task":
        result = projects.save_project_task(
            actor, project_id, CreateTask.model_validate(fields), transaction=db
        )
    elif kind == "update_project_task":
        result = projects.save_task(
            actor, project_id, target["id"], UpdateTask.model_validate(fields), transaction=db
        )
    elif kind == "plan_project_task":
        result = scheduling.save_plan(
            actor, project_id, target["id"], scheduling.TaskPlan.model_validate(fields), transaction=db
        )
    else:
        fields = {**fields, "expected_version": scheduling.state(db, project_id)["version"]}
        if kind == "set_project_dependencies":
            result = scheduling.save_links(
                actor, project_id, scheduling.Dependencies.model_validate(fields), transaction=db
            )
        else:
            result = scheduling.save_config(
                actor, project_id, scheduling.ConfigUpdate.model_validate(fields), transaction=db
            )
    return {**result, "schedule_version": scheduling.state(db, project_id)["version"]}


def undo(actor, action, result, request_id, db):
    target, before, kind = action["target"], action["before"], action["kind"]
    project_id = target["project_id"]
    if kind == "create_project_task":
        inverse = projects.save_task(
            actor,
            project_id,
            result["task"]["id"],
            UpdateTask(expected_version=result["task"]["version"], status="cancelled"),
            transaction=db,
        )
    elif kind == "update_project_task":
        inverse = projects.save_task(
            actor,
            project_id,
            target["id"],
            UpdateTask(
                expected_version=result["task"]["version"],
                **{
                    k: before[k] for k in ("title", "description", "status", "priority", "due_at", "timezone")
                },
            ),
            transaction=db,
        )
    elif kind == "plan_project_task":
        inverse = scheduling.save_plan(
            actor,
            project_id,
            target["id"],
            scheduling.TaskPlan(
                request_id=request_id, expected_version=result["plan"]["plan_version"], **before
            ),
            transaction=db,
        )
    elif kind == "set_project_dependencies":
        inverse = scheduling.save_links(
            actor,
            project_id,
            scheduling.Dependencies(
                request_id=request_id,
                expected_version=scheduling.state(db, project_id)["version"],
                links=before["links"],
            ),
            transaction=db,
        )
    else:
        scheduling._parent(db, actor, project_id)
        version = scheduling.state(db, project_id)["version"] + 1
        db.execute(
            "UPDATE work_project_schedule_state SET version=?,config_json=?,configured=? WHERE project_id=?",
            [version, json.dumps(before["config"]), int(before["configured"]), project_id],
        )
        collaboration.audit(db, project_id, actor, "schedule_restored")
        inverse = {"version": version, **before}
    return {**inverse, "schedule_version": scheduling.state(db, project_id)["version"]}
