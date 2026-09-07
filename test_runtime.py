"""Phase 1/3/4 运行时权限层 + 任务/工作流自测（不依赖外部模型/网络，assert 风格）。

运行：python test_runtime.py
覆盖 TASK.md §26-§24 的验收点：授权 / 可见性 / Passport 不可变 / legacy adapter /
derive 不扩权 / Task 状态机 / Workflow 顺序 Stage 派生。
"""

import asyncio

from runtime.capability import Capability, CapabilityGrant, ToolPolicy
from runtime.gateway import (
    CapabilityGateway,
    LegacyPolicyAdapter,
    UnauthorizedTool,
)
from runtime.passport import Passport
from runtime.task import TaskStatus, new_task
from runtime.context import TaskContext
from runtime.workflow import Stage, Workflow


def make_gateway() -> CapabilityGateway:
    g = CapabilityGateway()
    g.register_tool("view", schema={"function": {"name": "view"}})
    g.register_tool("check_latex", schema={"function": {"name": "check_latex"}})
    g.register_tool("write_latex", schema={"function": {"name": "write_latex"}}, side_effect=True)
    g.register_tool("delete_memory", schema={"function": {"name": "delete_memory"}}, side_effect=True)
    return g


def test_authorization():
    g = make_gateway()
    p = g.issue("math", [CapabilityGrant(Capability("view"), ToolPolicy(permission="allow"))])
    assert g.authorize(p, "view") is True
    assert g.authorize(p, "check_latex") is False
    assert g.authorize(p, "nonexistent") is False


def test_deny_overrides_allow():
    g = make_gateway()
    p = g.issue(
        "math",
        [
            CapabilityGrant(Capability("view"), ToolPolicy(permission="allow")),
            CapabilityGrant(Capability("view"), ToolPolicy(permission="deny")),
        ],
    )
    assert g.authorize(p, "view") is False


def test_visibility():
    g = make_gateway()
    p = g.issue(
        "math",
        [
            CapabilityGrant(Capability("view"), ToolPolicy(permission="allow", visibility="hidden")),
            CapabilityGrant(Capability("check_latex"), ToolPolicy(permission="allow", visibility="normal")),
        ],
    )
    assert g.authorize(p, "view") is True       # hidden 仍可执行
    assert "view" not in g.visible_tools(p)      # 但不暴露给 LLM
    assert "check_latex" in g.visible_tools(p)


def test_visibility_emphasis_order():
    g = make_gateway()
    p = g.issue(
        "math",
        [
            CapabilityGrant(Capability("view"), ToolPolicy(permission="allow", visibility="normal", priority=0)),
            CapabilityGrant(Capability("check_latex"), ToolPolicy(permission="allow", visibility="emphasized", priority=10)),
        ],
    )
    vis = g.visible_tools(p)
    assert vis.index("check_latex") < vis.index("view")


def test_passport_immutable():
    g = make_gateway()
    p = g.issue("math", [CapabilityGrant(Capability("view"), ToolPolicy(permission="allow"))])
    try:
        p.grants = ()  # type: ignore
        raise AssertionError("Passport 应不可变：grants 可被重赋值")
    except Exception:
        pass
    try:
        p.principal_id = "hacker"  # type: ignore
        raise AssertionError("Passport 应不可变：principal_id 可被重赋值")
    except Exception:
        pass
    try:
        p.grants += (CapabilityGrant(Capability("check_latex"), ToolPolicy(permission="allow")),)  # type: ignore
        raise AssertionError("Passport 应不可变：grants 可被拼接扩权")
    except Exception:
        pass


def test_legacy_adapter():
    g = make_gateway()
    adapter = LegacyPolicyAdapter(g)

    p = adapter.to_passport("math", tool_names=["view"])
    assert g.authorize(p, "view") is True
    assert g.authorize(p, "check_latex") is False

    p2 = adapter.to_passport("super", deny_tools=["delete_memory"])
    assert g.authorize(p2, "view") is True
    assert g.authorize(p2, "check_latex") is True
    assert g.authorize(p2, "delete_memory") is False


def test_derive_cannot_expand():
    g = make_gateway()
    parent = g.issue("math", [CapabilityGrant(Capability("view"), ToolPolicy(permission="allow"))])
    child = g.derive(
        parent,
        [
            CapabilityGrant(Capability("view"), ToolPolicy(permission="allow")),
            CapabilityGrant(Capability("check_latex"), ToolPolicy(permission="allow")),
        ],
        stage_id="latex_generation",
    )
    assert g.authorize(child, "view") is True
    assert g.authorize(child, "check_latex") is False
    assert child.parent_passport_id == parent.passport_id


def test_execute():
    g = make_gateway()
    p = g.issue("math", [CapabilityGrant(Capability("view"), ToolPolicy(permission="allow"))])

    async def _run():
        calls = []

        async def fake_submit(func_name, **kwargs):
            calls.append((func_name, kwargs))
            return {"ok": True, "func": func_name}

        res = await g.execute(p, fake_submit, "view", path="a.tex")
        assert res["func"] == "view"
        assert calls == [("view", {"path": "a.tex"})]

        try:
            await g.execute(p, fake_submit, "delete_memory", memory_id="x")
            raise AssertionError("denied 工具应抛 UnauthorizedTool")
        except UnauthorizedTool:
            pass

    asyncio.run(_run())


# ---------------- Phase 3: Task / TaskContext ----------------

def test_task_status_machine():
    t = new_task("整理课堂笔记", holder="notetaker")
    assert t.status is TaskStatus.PENDING
    assert t.id.startswith("task-")
    t.mark(TaskStatus.RUNNING).mark(TaskStatus.VERIFYING).mark(TaskStatus.DONE)
    assert t.status is TaskStatus.DONE


def test_task_context_brief():
    ctx = TaskContext(
        task_id="task-1",
        goal="把公式整理成 LaTeX",
        holder="mathwrite",
        constraints=["遵守 view_theorem_style"],
        previous_results=["第 1 页 OCR 原文"],
    )
    b = ctx.brief()
    assert "把公式整理成 LaTeX" in b
    assert "mathwrite" in b
    assert "view_theorem_style" in b
    assert "第 1 页 OCR 原文" in b


# ---------------- Phase 4: Workflow / Stage ----------------

def test_workflow_sequential_and_derive():
    g = make_gateway()
    # 父 Passport 拥有全部工具（模拟 Super / 全量 parent）
    parent = g.issue(
        "super",
        [
            CapabilityGrant(Capability(name), ToolPolicy(permission="allow"))
            for name in ("view", "check_latex", "write_latex")
        ],
    )
    wf = Workflow(
        name="LatexGen",
        stages=[
            Stage("understand", tools=("view",)),
            Stage("write", tools=("write_latex", "check_latex")),
            Stage("verify", tools=("check_latex",)),
        ],
    )
    assert wf.next_stage(None).name == "understand"
    assert wf.next_stage("understand").name == "write"
    assert wf.next_stage("write").name == "verify"
    assert wf.next_stage("verify") is None

    # Stage → 派生 Passport：understand 阶段不能 write_latex，也不能扩出父没有的工具
    pp_understand = wf.derive_passport(g, parent, wf.stages[0])
    assert g.authorize(pp_understand, "view") is True
    assert g.authorize(pp_understand, "write_latex") is False
    assert pp_understand.stage_id == "understand"

    pp_write = wf.derive_passport(g, parent, wf.stages[1])
    assert g.authorize(pp_write, "write_latex") is True
    assert g.authorize(pp_write, "check_latex") is True
    assert g.authorize(pp_write, "view") is False


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"\n{len(tests)} 个测试全部通过。")


if __name__ == "__main__":
    main()
