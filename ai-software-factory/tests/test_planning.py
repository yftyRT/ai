import copy
import json

import pytest

from app.agents.roles import path_policy_errors, select_roles
from app.providers.fake import FakeProvider, _PLAN
from app.schemas.models import Plan, PlanError, ProjectState, Task
from tests.helpers import OWNER, make_app, run, settle


def _task(i, agent="backend", deps=(), owned=("src/**",)):
    return Task(id=i, agent=agent, skill="x", description="d", depends_on=list(deps), owned_files=list(owned))


def test_cycle_is_rejected():
    with pytest.raises(Exception, match="circular"):
        Plan(project_name="p", tasks=[_task("a", deps=["b"], owned=["a/**"]), _task("b", deps=["a"], owned=["b/**"])])


def test_unknown_dependency_and_self_dependency_rejected():
    with pytest.raises(Exception, match="unknown task"):
        Plan(project_name="p", tasks=[_task("a", deps=["zzz"])])
    with pytest.raises(Exception, match="itself"):
        Plan(project_name="p", tasks=[_task("a", deps=["a"])])


def test_overlapping_file_ownership_rejected():
    with pytest.raises(Exception, match="overlap"):
        Plan(project_name="p", tasks=[_task("a", owned=["src/**"]), _task("b", owned=["src/api.py"])])


def test_unsafe_paths_and_ids_rejected():
    for bad in ("../x", "/etc/passwd", "C:\\x"):
        with pytest.raises(Exception):
            _task("a", owned=(bad,))
    with pytest.raises(Exception):
        _task("Bad Id")


def test_role_policy_limits_ownership():
    assert path_policy_errors("tester", ["tests/**"]) == []
    assert path_policy_errors("backend", ["tests/**"])  # backend may not own tests
    assert path_policy_errors("docs", ["src/**"])
    assert path_policy_errors("backend", [".git/**"])
    assert path_policy_errors("frontend", ["backend/**"])


def test_simple_project_gets_only_core_roles():
    assert select_roles("a command line tool that renames files") == ["architect", "backend", "tester", "docs"]
    big = select_roles("a web dashboard with a postgres database, login and a stripe integration")
    assert {"frontend", "database", "security", "api_integrator"} <= set(big)


def _plan_with(mutator):
    data = copy.deepcopy(_PLAN)
    mutator(data)
    return lambda prompt: json.dumps(data)


def test_planner_rejects_bad_plans_then_fails_clearly(tmp_path):
    async def go():
        fp = FakeProvider()
        # a cycle the model keeps producing
        fp.handlers["### REQUEST: PLAN"] = _plan_with(lambda d: d["tasks"][0].update(depends_on=["docs"]))
        app, msgr = await make_app(tmp_path, provider=fp)
        await app.bot.handle_text(OWNER, 1, "Build a small python library that counts words in text files.")
        pid = (await app.storage.list_projects(OWNER))[0].id
        p = await settle(app, pid)
        assert p.state == ProjectState.FAILED and "valid plan" in p.error
        assert sum("### REQUEST: PLAN" in c for c in fp.calls) == 2  # one retry with feedback
        assert "REJECTED" in [c for c in fp.calls if "### REQUEST: PLAN" in c][1]
        assert "failed" in msgr.sent[-1]["text"]
    run(go())


def test_planner_rejects_unnecessary_agents(tmp_path):
    async def go():
        fp = FakeProvider()

        def add_frontend(d):
            d["tasks"].append({"id": "ui", "title": "ui", "agent": "frontend", "description": "ui",
                               "depends_on": ["arch"], "acceptance_criteria": ["x"], "owned_files": ["frontend/**"]})
        fp.handlers["### REQUEST: PLAN"] = _plan_with(add_frontend)
        app, msgr = await make_app(tmp_path, provider=fp)
        await app.bot.handle_text(OWNER, 1, "Build a small python library that counts words in text files.")
        pid = (await app.storage.list_projects(OWNER))[0].id
        p = await settle(app, pid)
        assert p.state == ProjectState.FAILED and "not allowed" in p.error and "frontend" in p.error
    run(go())


def test_plan_shows_skills_cost_and_asks_for_confirmation(tmp_path):
    async def go():
        app, msgr = await make_app(tmp_path)
        await app.bot.handle_text(OWNER, 1, "Build a small python library that counts words in text files.")
        pid = (await app.storage.list_projects(OWNER))[0].id
        p = await settle(app, pid)
        text = msgr.sent[-1]["text"]
        assert p.state == ProjectState.AWAITING_CONFIRMATION
        assert "Estimated cost" in text and "ESTIMATE" in text and "Skills:" in text
        assert all(t.skill and t.skill != "pending" and t.skill_version for t in p.plan.tasks)
        assert {u.origin for u in p.skills_used} == {"already_installed"}
        # nothing runs until the user confirms
        assert all(t.state.value == "PENDING" for t in await app.storage.get_tasks(pid))
        assert "cancelled" in await app.bot.handle_callback(OWNER, f"no:{pid}")
    run(go())
