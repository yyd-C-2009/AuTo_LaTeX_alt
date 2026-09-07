"""runtime/task.py —— 任务生命周期原语（Phase 3，纯内存态，不持久化）。

Task 只回答「要做什么、现在什么状态」，不回答「怎么做」——
「怎么做」是 Phase 4 的 Workflow 职责，二者刻意解耦（见 TASK.md §17-18）。
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from itertools import count
from typing import Optional


class TaskStatus(Enum):
    """任务状态机。属于 Task/Workflow 层，不塞进 Plan（见 TASK.md §16-18）。"""

    PENDING = "pending"
    RUNNING = "running"
    VERIFYING = "verifying"
    DONE = "done"
    FAILED = "failed"


@dataclass
class ArtifactRef:
    """任务产物的引用。本阶段仅 metadata，不做内容校验/持久化。"""

    path: str
    kind: str = "file"                 # file / latex / tikz / note ...
    checksum: Optional[str] = None


@dataclass
class Task:
    """一个工作单元。id 由外部（Super/Workflow）分配，new_task 提供便捷构造。"""

    id: str
    goal: str
    status: TaskStatus = TaskStatus.PENDING
    holder: Optional[str] = None       # 负责此任务的 principal / agent id
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    def mark(self, status: TaskStatus) -> "Task":
        self.status = status
        self.updated_at = datetime.now()
        return self


_task_counter = count(1)


def new_task(goal: str, holder: Optional[str] = None) -> Task:
    """便捷构造：自动分配增量 id。"""
    return Task(id=f"task-{next(_task_counter)}", goal=goal, holder=holder)
