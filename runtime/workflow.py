"""runtime/workflow.py —— 可执行工作流（Phase 4，顺序 Stage 版）。

只实现顺序 Stage 链，不做 DAG 编辑器（见 TASK.md §24：第一版一个线性列表就够了）。
Stage → Gateway.derive → 新的 Passport，实现「同一 Agent 不同阶段不同能力」。

关系（TASK.md §17）：Plan 回答「要做什么」，Workflow 回答「这个东西具体怎么做」。
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from runtime.context import TaskContext
from runtime.gateway import CapabilityGateway
from runtime.passport import Passport
from runtime.task import Task, TaskStatus


@dataclass
class Stage:
    """工作流中的一个阶段：一个名字 + 本阶段所需能力。

    capabilities 用 Capability 语义名（能跨工具共享），最终由已注册工具的
    tools_caps 映射解析成 tool 集合（若网关已把工具归并到这些语义能力名下）。
    tools 直接列 tool 名则精确到工具。
    """

    name: str
    capabilities: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()

    def __post_init__(self):
        if not self.name or not isinstance(self.name, str):
            raise ValueError("Stage.name 必须为非空字符串")
        object.__setattr__(self, "capabilities", tuple(self.capabilities or ()))
        object.__setattr__(self, "tools", tuple(self.tools or ()))
        # 允许两者皆空：纯控制阶段（如 CommitCursor）不授权任何工具，
        # 只做提交/回滚决策，派生出的 Passport 也就没有任何 grant。


class StageAbort(Exception):
    """阶段中止：runner 判定本阶段的验收条件未满足时抛出。

    中止后 Workflow.run 不再推进后续阶段，并把 task 标记为 FAILED，
    以便调用方回滚副作用（例如 Resident 不推进 Listener 游标）。
    与普通异常的区别：这是「按预期判定失败」，不是「运行时出错」。
    """

    def __init__(self, stage_name: str, reason: str = ""):
        self.stage_name = stage_name
        self.reason = reason
        super().__init__(f"阶段 {stage_name} 中止：{reason}" if reason else f"阶段 {stage_name} 中止")


@dataclass
class Workflow:
    """顺序 Stage 链。

    tool_caps: capability 语义名 → 对应的一组 tool 名，用于把 Stage.capabilities
    解析成 tool 集合。若某个 capability 名未在此登记，则把它原样当作一个 tool 名
    （兼容「能力名 == 工具名」的默认情况）。
    """

    name: str
    stages: list[Stage]
    tool_caps: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self):
        if not self.name:
            raise ValueError("Workflow.name 不能为空")
        if not self.stages:
            raise ValueError("Workflow 至少需要一个 Stage")
        self._stage_index = {s.name: i for i, s in enumerate(self.stages)}

    def next_stage(self, current: Optional[str]) -> Optional[Stage]:
        """顺序推进：返回 current 的下一个 Stage；current 为 None 返回首阶段；
        已是末阶段返回 None。"""
        if current is None:
            return self.stages[0]
        idx = self._stage_index.get(current)
        if idx is None:
            raise ValueError(f"未知 Stage: {current}")
        return self.stages[idx + 1] if idx + 1 < len(self.stages) else None

    # ------------------------------------------------------------------
    # Stage → tool 集解析
    # ------------------------------------------------------------------
    def stage_tools(self, stage: Stage) -> set[str]:
        tools: set[str] = set()
        for cap in stage.capabilities:
            mapped = self.tool_caps.get(cap)
            if mapped:
                tools.update(mapped)
            else:
                tools.add(cap)      # 能力名 == 工具名 的默认情况
        tools.update(stage.tools)
        return tools

    # ------------------------------------------------------------------
    # Passport 派生入口（Stage → CapabilityGateway.derive → 新 Passport）
    # ------------------------------------------------------------------
    def derive_passport(
        self,
        gateway: CapabilityGateway,
        parent: Passport,
        stage: Stage,
    ) -> Passport:
        """把 Stage 派生为一个新 Passport（能力只收不扩，见 gateway.derive）。

        derived 的 allow 集合 = parent 的 allow ∩ Stage 需要的 tool 集合。
        """
        from runtime.capability import Capability, CapabilityGrant, ToolPolicy

        needed = self.stage_tools(stage)
        grants = [
            CapabilityGrant(capability=Capability(t), policy=ToolPolicy(permission="allow"))
            for t in sorted(needed)
        ]
        return gateway.derive(parent, grants, stage_id=stage.name)

    # ------------------------------------------------------------------
    # 顺序执行器（Phase 4 接线用，runner 可注入以便离线测试）
    # ------------------------------------------------------------------
    async def run(
        self,
        gateway: CapabilityGateway,
        parent: Passport,
        task: Task,
        *,
        runner: Callable | None = None,
    ) -> list[Any]:
        """逐阶段执行：每阶段 derive 一个窄化 passport，交给 runner 跑，结果传给下一阶段。

        runner(stage: Stage, passport: Passport, task: Task, prev: list[Any]) -> Any，
        默认抛 NotImplementedError（由上层注入真实专家调度；离线测试传 fake）。
        """
        current: Optional[Stage] = None
        prev: list[Any] = []
        results: list[Any] = []
        task.mark(TaskStatus.RUNNING)
        try:
            while True:
                stage = self.next_stage(current)
                if stage is None:
                    break
                passport = self.derive_passport(gateway, parent, stage)
                out = await (runner or _noop_runner)(stage, passport, task, prev)
                results.append(out)
                prev = [out]
                current = stage.name
        except StageAbort:
            # 验收失败：标记 FAILED 后向上抛出，让调用方回滚副作用
            # （如 Resident 不推进 Listener 游标）。已完成的阶段结果不返回。
            task.mark(TaskStatus.FAILED)
            raise
        except Exception:
            task.mark(TaskStatus.FAILED)
            raise
        task.mark(TaskStatus.DONE)
        return results


async def _noop_runner(stage, passport, task, prev):
    raise NotImplementedError("run_workflow 需要注入真实 runner（或测试 fake）")

