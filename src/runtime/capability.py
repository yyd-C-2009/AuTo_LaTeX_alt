"""runtime/capability.py —— 能力原语。

Capability 是「系统能力语义」，刻意不与 tool name 直接等同。
上一层的 Passport.stage（阶段）映射到一组 Capability name，
再由 CapabilityGateway 把 capability name 落到具体 tool 白名单。
这样「Agent 身份」与「某次任务当前阶段能做什么」得以分离。

本模块刻意保持最小：没有 Provider / Resolver / Factory。
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Capability:
    """一种系统能力（如 "latex.read" / "memory.write"）。

    约束（constraints）：可选字典，用于未来对能力附加参数级限制，
    本阶段仅作为 metadata 携带，不参与授权判断。
    """

    name: str
    constraints: tuple[tuple[str, str], ...] = ()

    def __post_init__(self):
        if not self.name or not isinstance(self.name, str):
            raise ValueError("Capability.name 必须为非空字符串")
        object.__setattr__(self, "name", self.name.strip())
        if not self.name:
            raise ValueError("Capability.name 不能为空白")


@dataclass(frozen=True)
class CapabilityGrant:
    """授权声明：把一种能力在给定 policy 下授予某个 principal/stage。

    本阶段只用 tool/alias 的显式授权（allow + visibility）；priority 仅作
    visibility 排序元数据，不改变授权结论。
    """

    capability: Capability
    policy: "ToolPolicy"

    def allows(self) -> bool:
        """是否有权执行该能力（显式 allow 为唯一依据）。"""
        return self.policy and self.policy.permission == "allow"


@dataclass(frozen=True)
class ToolPolicy:
    """授权策略。

    permission : "allow" / "deny"（默认 deny）
    visibility : "hidden" / "normal" / "emphasized"（默认 normal）
    priority   : 排序元数据（越大越靠前，仅影响 schema 呈现）
    """

    permission: str = "deny"
    visibility: str = "normal"
    priority: int = 0

    _PERMISSIONS = ("allow", "deny")
    _VISIBILITIES = ("hidden", "normal", "emphasized")

    def __post_init__(self):
        if self.permission not in self._PERMISSIONS:
            raise ValueError(f"permission 必须是 {self._PERMISSIONS} 之一: {self.permission!r}")
        if self.visibility not in self._VISIBILITIES:
            raise ValueError(f"visibility 必须是 {self._VISIBILITIES} 之一: {self.visibility!r}")


def allow(*, visibility: str = "normal", priority: int = 0) -> ToolPolicy:
    """快捷构造一个 allow 策略。"""
    return ToolPolicy(permission="allow", visibility=visibility, priority=priority)


def deny() -> ToolPolicy:
    """快捷构造一个 deny 策略。"""
    return ToolPolicy(permission="deny", visibility="hidden")
