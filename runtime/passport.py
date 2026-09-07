"""runtime/passport.py —— 不可变护照（Passport）。

Passport 是一次任务执行中「某 principal 在某阶段」的能力授权凭证：
- 由 CapabilityGateway 创建，Agent 只能读取、不能修改。
- 本阶段不实现 JWT/密码学签名（单进程个人 Agent），授权边界落在
  CapabilityGateway.execute()，而非这里的字段。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

from runtime.capability import CapabilityGrant


@dataclass(frozen=True)
class Passport:
    """不可变授权凭证。

    task_id / stage_id 可为 None（legacy 适配路径下没有任务/阶段概念）。
    grants 为不可变 tuple，杜绝运行时追加能力。
    """

    passport_id: str
    principal_id: str
    grants: Tuple[CapabilityGrant, ...]
    task_id: Optional[str] = None
    stage_id: Optional[str] = None
    parent_passport_id: Optional[str] = None
    expires_at: Optional[datetime] = None

    def __post_init__(self):
        if not self.passport_id or not self.principal_id:
            raise ValueError("Passport 需要非空的 passport_id 与 principal_id")
        if self.expires_at is not None and isinstance(self.expires_at, str):
            # 宽容：允许传入 ISO 字符串，统一转 datetime，便于序列化后重建。
            object.__setattr__(
                self, "expires_at", datetime.fromisoformat(self.expires_at)
            )

    def allows(self, capability_name: str) -> bool:
        """当前是否拥有一项能力（显式 allow 才成立）。"""
        return any(
            g.allows() and g.capability.name == capability_name
            for g in self.grants
        )
