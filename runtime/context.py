"""runtime/context.py —— 结构化工作状态（Phase 3，纯内存态）。

TaskContext 是一次 Task 执行中「专家能拿到的结构化上下文」：
goal / 约束 / 产物引用 / 前置结果。目标是用它逐步替代 super.py 的
_dialogue_context() 把 Super 纯文本历史塞给专家的 hack（见 TASK.md §11/§23）。

本阶段不写文件、不做定权：TaskContext 只是数据容器，授权仍在 Passport/Gateway。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from runtime.task import ArtifactRef


@dataclass
class VerificationState:
    """验证状态（如 check_latex / check_tikz 的通过与否）。

    与 TaskStatus 不同：VerificationState 描述「某产物当前是否通过验证」，
    TaskStatus 描述「任务当前阶段」。二者解耦。
    """

    artifact: ArtifactRef
    passed: bool = False
    log: str = ""
    checked_at: Optional[datetime] = None

    def set(self, passed: bool, log: str = "") -> "VerificationState":
        self.passed = passed
        self.log = log
        self.checked_at = datetime.now()
        return self


@dataclass
class TaskContext:
    """结构化工作上下文：goal + 约束 + 产物 + 前置结果。

    previous_results 为任意结构化内容（list/dict），供专家作为背景参考，
    不再依赖 Super 的纯文本对话历史。
    """

    task_id: str
    goal: str
    holder: Optional[str] = None
    constraints: list[str] = field(default_factory=list)
    artifacts: list[ArtifactRef] = field(default_factory=list)
    previous_results: list[Any] = field(default_factory=list)
    plan: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    def brief(self) -> str:
        """给专家注入的可读摘要（multiple 行，非 JSON）。"""
        lines = [f"目标: {self.goal}"]
        if self.holder:
            lines.append(f"负责: {self.holder}")
        if self.constraints:
            lines.append("约束: " + "；".join(self.constraints))
        if self.artifacts:
            lines.append(
                "产物: " + "；".join(
                    f"{a.path}({a.kind})" for a in self.artifacts
                )
            )
        if self.previous_results:
            lines.append(
                "前置结果:\n" + "\n".join(f"- {r}" for r in self.previous_results)
            )
        if self.plan:
            lines.append(f"计划: {self.plan}")
        return "\n".join(lines)
