"""runtime/gateway.py —— CapabilityGateway（授权与执行边界）。

唯一正式的执行授权边界：任何 Agent tool call 进入 EventBus/Tool handler 之前，
必须经过 CapabilityGateway.authorize / execute。

设计约束（Phase 1）：
- 不重写 EventBus / Agent loop：Gateway.execute 内部仍然把命令送进既有的
  bus.submit()，bus 继续负责 slow/reentrant/dangerous/timeout/结果存储。
- 不改 Tools API：通过 ToolSpec 把现有 tool name/schema/超时等元数据映射进来，
  本阶段只有 capabilities + policy 真正参与授权。
- 旧 tool_names= / deny_tools= 通过 LegacyPolicyAdapter 转成 Passport，
  保证现有 Super/Math/MathWrite/Resident/Listener 全部零行为变化。
"""

import itertools
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Sequence

from runtime.capability import Capability, CapabilityGrant, ToolPolicy
from runtime.passport import Passport


class UnauthorizedTool(Exception):
    """工具未获授权时抛出。"""


@dataclass(frozen=True)
class ToolSpec:
    """一个已注册工具的元数据（对现有 Tools API 的无损映射）。"""

    name: str
    schema: dict = field(default_factory=dict)
    capabilities: tuple[str, ...] = ()

    # 本阶段仅作 metadata，不参与授权。
    timeout: float = 5.0
    side_effect: bool = False
    resources: tuple[str, ...] = ()
    idempotent: bool = False

    def __post_init__(self):
        if not self.name:
            raise ValueError("ToolSpec.name 不能为空")
        caps = self.capabilities or (self.name,)
        object.__setattr__(self, "capabilities", tuple(caps))


class LegacyPolicyAdapter:
    """把旧 run_agent(tool_names=..., deny_tools=...) 转成 Passport。

    旧语义：tool_names 为 None = 全部放行（Super 全量调度权），
    deny_tools 为拒绝名单。这里一一对应：tool_names 为 None 时不做白名单
    限制（仅施加 deny），否则只允许白名单内的工具。
    """

    def __init__(self, gateway: "CapabilityGateway"):
        self.gateway = gateway

    def to_passport(
        self,
        principal_id: str,
        tool_names: Sequence[str] | None = None,
        deny_tools: Sequence[str] | None = None,
        passport_id: str | None = None,
        task_id: str | None = None,
        stage_id: str | None = None,
    ) -> Passport:
        allow_set = set(tool_names) if tool_names is not None else None
        deny_set = set(deny_tools) if deny_tools else set()

        grants: list[CapabilityGrant] = []
        named_tools = (
            allow_set
            if allow_set is not None
            else list(self.gateway.tool_names())
        )
        for name in sorted(named_tools):
            if name not in self.gateway.tool_names():
                continue
            grants.append(
                CapabilityGrant(
                    capability=Capability(name),
                    policy=ToolPolicy(permission="allow", visibility="normal"),
                )
            )
        for name in sorted(deny_set):
            grants.append(
                CapabilityGrant(
                    capability=Capability(name),
                    policy=ToolPolicy(permission="deny", visibility="hidden"),
                )
            )
        return self.gateway.issue(
            passport_id=passport_id
            or f"legacy:{principal_id}:{task_id if task_id else 'session'}",
            principal_id=principal_id,
            grants=tuple(grants),
            task_id=task_id,
            stage_id=stage_id,
        )


class CapabilityGateway:
    """授权与执行边界。

    - issue:  创建 Passport（本阶段唯一签发入口）。
    - derive: 从父 Passport 派生子 Passport（能力只收不扩，供未来 Stage 用）。
    - visible_tools / visible_schema: 按 Passport 裁剪对 LLM 可见的工具。
    - authorize / execute: 执行层授权（真正的安全边界）。
    """

    def __init__(self):
        self._specs: dict[str, ToolSpec] = {}
        self._order: list[str] = []
        self._counter = itertools.count(1)

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------
    def register_tool(
        self,
        name: str,
        schema: dict | None = None,
        capabilities: Sequence[str] | None = None,
        *,
        timeout: float = 5.0,
        side_effect: bool = False,
        resources: Sequence[str] = (),
        idempotent: bool = False,
    ) -> None:
        """注册一个工具。capabilities 缺省时用工具名本身作为能力名（保证
        工具级授权精确），上层语义能力（如 "latex.write"）可显式声明为多个
        工具共享的能力。"""
        self._specs[name] = ToolSpec(
            name=name,
            schema=(schema or {}).copy(),
            capabilities=tuple(capabilities) if capabilities else (name,),
            timeout=timeout,
            side_effect=side_effect,
            resources=tuple(resources),
            idempotent=idempotent,
        )
        if name not in self._order:
            self._order.append(name)

    def tool_names(self) -> list[str]:
        return list(self._order)

    def add_capability(self, capability_name: str, tool_names: Sequence[str]) -> None:
        """把既有工具归并到一个语义能力名下（用于 AgentDefinition 声明）。"""
        for name in tool_names:
            spec = self._specs.get(name)
            if spec is None:
                raise ValueError(f"未注册的工具: {name}")
            caps = set(spec.capabilities) | {capability_name}
            self._specs[name] = dataclass_with_caps(spec, tuple(caps))

    # ------------------------------------------------------------------
    # 签发
    # ------------------------------------------------------------------
    def issue(
        self,
        principal_id: str,
        grants: Sequence[CapabilityGrant],
        passport_id: str | None = None,
        task_id: str | None = None,
        stage_id: str | None = None,
        parent_passport_id: str | None = None,
        expires_at=None,
    ) -> Passport:
        pid = passport_id or f"pp:{next(self._counter)}"
        return Passport(
            passport_id=pid,
            principal_id=principal_id,
            grants=tuple(grants),
            task_id=task_id,
            stage_id=stage_id,
            parent_passport_id=parent_passport_id,
            expires_at=expires_at,
        )

    def derive(
        self,
        parent: Passport,
        grants: Sequence[CapabilityGrant],
        *,
        stage_id: str | None = None,
        task_id: str | None = None,
        principal_id: str | None = None,
    ) -> Passport:
        """派生子 Passport：仅能从父的 allow 集合里收窄，不能扩大。"""
        parent_caps = self._allowed_capabilities(parent)
        narrowed = [
            g for g in grants
            if g.allows() and g.capability.name in parent_caps
        ]
        return self.issue(
            principal_id=principal_id or parent.principal_id,
            grants=tuple(narrowed),
            task_id=task_id or parent.task_id,
            stage_id=stage_id or parent.stage_id,
            parent_passport_id=parent.passport_id,
            expires_at=parent.expires_at,
        )

    # ------------------------------------------------------------------
    # 授权
    # ------------------------------------------------------------------
    def _grant_caps(self, passport: Passport) -> tuple[set[str], set[str]]:
        allow_set: set[str] = set()
        deny_set: set[str] = set()
        for g in passport.grants:
            if g.policy is None:
                continue
            if g.policy.permission == "allow":
                allow_set.add(g.capability.name)
            elif g.policy.permission == "deny":
                deny_set.add(g.capability.name)
        return allow_set, deny_set

    def _allowed_capabilities(self, passport: Passport) -> set[str]:
        allow_set, deny_set = self._grant_caps(passport)
        return allow_set - deny_set

    def _visibility(self, passport: Passport, tool_name: str) -> str:
        """工具可见性：取该工具命中的 grant 里 visibility 最高的一档。"""
        rank = {"hidden": 0, "normal": 1, "emphasized": 2}
        cap_names = set(self._specs[tool_name].capabilities)
        best = "hidden"
        for g in passport.grants:
            if g.capability.name in cap_names and g.policy:
                if g.policy.permission != "allow":
                    return "hidden"
                if rank[g.policy.visibility] > rank[best]:
                    best = g.policy.visibility
        return best

    def authorize(self, passport: Passport, tool_name: str) -> bool:
        """执行层授权：工具是否允许执行（不关心可见性）。"""
        if tool_name not in self._specs:
            return False
        allow_set, deny_set = self._grant_caps(passport)
        caps = set(self._specs[tool_name].capabilities)
        if caps & deny_set:
            return False
        return bool(caps & allow_set)

    # ------------------------------------------------------------------
    # 可见性 / schema
    # ------------------------------------------------------------------
    def visible_tools(self, passport: Passport) -> list[str]:
        names = [
            name for name in self._order
            if self.authorize(passport, name)
            and self._visibility(passport, name) != "hidden"
        ]
        # emphasized / 高优先级工具靠前（visibility 呈现，与 visible_schema 一致）
        names.sort(key=lambda n: (-self._priority(passport, n), self._order.index(n)))
        return names

    def visible_schema(self, passport: Passport) -> list[dict]:
        """按可见性+优先级排序裁剪 schema（schema 过滤只是「看不见」，
        真正的执行边界是 execute()）。"""
        return [self._specs[n].schema for n in self.visible_tools(passport) if self._specs[n].schema]

    def _priority(self, passport: Passport, tool_name: str) -> int:
        if tool_name not in self._specs:
            return 0
        cap_names = set(self._specs[tool_name].capabilities)
        return max(
            (g.policy.priority for g in passport.grants
             if g.capability.name in cap_names and g.policy),
            default=0,
        )

    # ------------------------------------------------------------------
    # 执行边界
    # ------------------------------------------------------------------
    async def execute(self, passport: Passport, submit: Callable, tool_name: str, **kwargs):
        """授权后把命令送进 bus.submit（保留 slow/dangerous/timeout 语义）。

        submit: 形如 bus.submit(func_name=..., **kwargs) 的可等待对象，返回 Message。
        未授权时抛 UnauthorizedTool（由调用方决定如何回填给 LLM）。
        """
        if not self.authorize(passport, tool_name):
            raise UnauthorizedTool(
                f"工具 {tool_name} 未获授权（Passport {passport.passport_id}）。"
            )
        return await submit(tool_name, **kwargs)


def dataclass_with_caps(spec: ToolSpec, caps: Sequence[str]) -> ToolSpec:
    """复制 ToolSpec 并替换 capabilities（dataclass frozen 的便捷重建）。"""
    return ToolSpec(
        name=spec.name,
        schema=spec.schema,
        capabilities=tuple(caps),
        timeout=spec.timeout,
        side_effect=spec.side_effect,
        resources=spec.resources,
        idempotent=spec.idempotent,
    )
