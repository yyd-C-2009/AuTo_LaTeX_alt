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
from runtime.workflow import Stage, StageAbort, Workflow
from event_bus import Message
from resident import ResidentAgent, build_lecture_note_workflow


class FakeToolBox:
    """最小 tools 替身：只实现 bus.submit 落到工具所需的 tool_list。"""

    def __init__(self, check_latex_fn):
        self.tool_list = {"check_latex": check_latex_fn}


class DummyBus:
    """常驻 Agent 的离线替身总线。

    提供真实 CapabilityGateway（含 check_latex 工具），并实现 submit 以
    驱动 gateway.execute —— 这样测试覆盖的是真实的授权+执行链路，
    而不是把 gateway 绕过去。
    """

    def __init__(self, check_latex_result: str = "语法检查通过"):
        self.check_latex_calls = 0
        self._check_latex_result = check_latex_result
        self.gateway = CapabilityGateway()
        for name in (
            "get_listen_result",
            "get_listen_cursor",
            "write_latex",
            "check_latex",
            "str_replace_editor",
            "view_theorem_style",
            "retrieve_context",
        ):
            self.gateway.register_tool(name, schema={"function": {"name": name}})
        self.tools = FakeToolBox(self._check_latex_stub)

    def _check_latex_stub(self, filename: str = "notes.tex") -> str:
        self.check_latex_calls += 1
        self.last_checked_filename = filename
        return self._check_latex_result

    def on(self, *args, **kwargs):
        return None

    def off(self, *args, **kwargs):
        return None

    def io_status(self, *args, **kwargs):
        return None

    async def io_print(self, *args, **kwargs):
        return None

    async def submit(self, func_name, **kwargs):
        """模拟 bus.submit 的 Done 包装，供 gateway.execute 调用。"""
        if func_name == "check_latex":
            return Message(title="Done", content=self._check_latex_stub(**kwargs))
        return Message(title="Done", content=f"stub:{func_name}")


class FakeTranscriptProvider:
    def __init__(self, cursor: int = 5):
        self.cursor = cursor

    def get_listen_cursor(self) -> str:
        return str(self.cursor)

    def get_listen_result(self, include_timestamps: bool = False, since_index: int = 0) -> str:
        if since_index < self.cursor:
            return "新增一段课堂内容"
        return "（暂无新增转写）"


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


def test_workflow_executor_offline():
    g = make_gateway()  # view / check_latex / write_latex / delete_memory
    parent = g.issue("super", [
        CapabilityGrant(Capability(nm), ToolPolicy(permission="allow"))
        for nm in ("view", "check_latex", "write_latex")
    ])
    task = new_task("离线工作流", holder="super")

    wf = Workflow(name="OfflineWf", stages=[
        Stage("s1", tools=("view",)),
        Stage("s2", tools=("write_latex", "check_latex")),
        Stage("s3", tools=("check_latex",)),
    ])

    calls = []

    async def fake_runner(stage, passport, task_obj, prev):
        calls.append((stage.name, stage.tools, prev))
        return f"out-{stage.name}"

    results = asyncio.run(wf.run(g, parent, task, runner=fake_runner))

    # 顺序推进: s1 → s2 → s3
    assert [c[0] for c in calls] == ["s1", "s2", "s3"]
    # 每阶段派生 passport 已窄化: s1 只允许 view
    assert g.authorize(wf.derive_passport(g, parent, wf.stages[0]), "view") is True
    assert g.authorize(wf.derive_passport(g, parent, wf.stages[0]), "write_latex") is False
    # 结果按序传递: 下一阶段 prev 收到上一阶段输出
    assert calls[1][2] == ["out-s1"]
    assert calls[2][2] == ["out-s2"]
    # 全部完成后 task 状态为 DONE
    assert task.status is TaskStatus.DONE


def test_resident_cursor_commits_only_after_verification():
    """Phase 5 核心不变量：Listener 游标只在 check_latex 验收通过后推进。

    三种情形都必须覆盖，否则「验收门」可能形同虚设：
      ① check_latex 通过           → 游标推进
      ② check_latex 失败           → 游标不动（下次唤醒重放同一段转写）
      ③ write_notes 阶段 LLM 异常  → 游标不动
    """

    async def run_case(check_latex_result, llm_raises=False):
        provider = FakeTranscriptProvider(cursor=5)
        bus = DummyBus(check_latex_result=check_latex_result)
        ra = ResidentAgent(
            name="notetaker",
            agent_type="notetaker",
            bus=bus,
            client=None,
            model="dummy",
            prompt="notetaker",
            tool_names=["get_listen_result", "get_listen_cursor", "write_latex", "check_latex"],
            interval_sec=30,
            transcript_provider=provider,
            gateway=bus.gateway,
            workflow=build_lecture_note_workflow(),
        )
        ra.last_cursor = 0

        async def fake_run_agent(*args, **kwargs):
            if llm_raises:
                raise RuntimeError("LLM 侧失败")
            return "已更新笔记"

        ra.agent.run_agent = fake_run_agent  # type: ignore
        await ra._tick()
        return ra, bus

    # ① 验收通过 → 游标提交到本轮 observe 到的游标
    ra_ok, bus_ok = asyncio.run(run_case("语法检查通过 (未发现 LaTeX 语法错误)"))
    assert ra_ok.last_cursor == 5, "验收通过后游标应推进"
    assert bus_ok.check_latex_calls >= 1, "验收阶段必须真的调用过 check_latex"

    # ② 验收失败 → 游标保持不动
    ra_fail, bus_fail = asyncio.run(run_case("语法检查失败: \n! Undefined control sequence."))
    assert ra_fail.last_cursor == 0, "check_latex 未通过时游标绝不能推进"
    assert bus_fail.check_latex_calls >= 1, "失败分支也真的检查过"

    # ③ LLM 阶段异常 → 游标保持不动
    ra_err, _ = asyncio.run(run_case("语法检查通过", llm_raises=True))
    assert ra_err.last_cursor == 0, "写入阶段异常时游标绝不能推进"


def test_resident_workflow_stage_authorizations():
    """LectureNoteTaking 的 stage passport 必须逐阶段收窄：
    observe 不能写笔记、write_notes 不能提交游标、verify 只能 check_latex。"""
    bus = DummyBus(check_latex_result="语法检查通过")
    g = bus.gateway
    wf = build_lecture_note_workflow()
    parent = LegacyPolicyAdapter(g).to_passport(
        "notetaker",
        tool_names=["get_listen_result", "get_listen_cursor", "write_latex", "check_latex"],
    )

    observe = wf.derive_passport(g, parent, wf.stages[0])
    assert g.authorize(observe, "get_listen_result") is True
    assert g.authorize(observe, "write_latex") is False

    write = wf.derive_passport(g, parent, wf.stages[1])
    assert g.authorize(write, "write_latex") is True
    assert g.authorize(write, "get_listen_result") is False

    verify = wf.derive_passport(g, parent, wf.stages[2])
    assert g.authorize(verify, "check_latex") is True
    assert g.authorize(verify, "write_latex") is False

    # commit_cursor 是纯控制阶段：不派生任何能力
    commit = wf.derive_passport(g, parent, wf.stages[3])
    assert commit.grants == ()
    assert wf.stages[3].name == "commit_cursor"


def test_workflow_abort_marks_task_failed():
    """StageAbort 应停止后续阶段并把 task 标记为 FAILED（供调用方回滚副作用）。"""
    g = make_gateway()
    parent = g.issue("super", [
        CapabilityGrant(Capability(nm), ToolPolicy(permission="allow"))
        for nm in ("view", "write_latex")
    ])
    task = new_task("验收失败的工作流", holder="super")
    wf = Workflow(name="AbortWf", stages=[
        Stage("s1", tools=("view",)),
        Stage("s2", tools=("write_latex",)),
        Stage("s3", tools=("view",)),
    ])

    visited = []

    async def aborting_runner(stage, passport, task_obj, prev):
        visited.append(stage.name)
        if stage.name == "s2":
            raise StageAbort("s2", "验收未通过")
        return f"out-{stage.name}"

    try:
        asyncio.run(wf.run(g, parent, task, runner=aborting_runner))
        raise AssertionError("StageAbort 应向上抛出")
    except StageAbort:
        pass

    assert visited == ["s1", "s2"], "中止后不得再进入后续阶段"
    assert task.status is TaskStatus.FAILED


def test_task_context_in_expert_wiring():
    # 直接构造专家调度会用的 TaskContext, 验证 brief 含关键字段 (不改真实 LLM 路径)
    ctx = TaskContext(
        task_id="task-1",
        goal="把公式整理成 LaTeX",
        holder="mathwrite",
        previous_results=["Super 与用户的对话历史: 用户问公式"],
    )
    b = ctx.brief()
    assert "把公式整理成 LaTeX" in b
    assert "mathwrite" in b
    assert "对话历史" in b


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"\n{len(tests)} 个测试全部通过。")


if __name__ == "__main__":
    main()
