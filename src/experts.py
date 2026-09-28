"""从 settings.json 装载 Super 与专家规格，避免提示词散落在 Python 源码。"""

from typing import Annotated
from config import require

SUPER_PROMPT = require("prompts.super")
_EXPERT_SPECS = require("prompts.experts")
EXPERTS = {
    name: (spec["prompt"], list(spec["tools"]))
    for name, spec in _EXPERT_SPECS.items()
}

LISTENER_STATEFUL_TOOLS = ["start_listening", "stop_listening", "get_listen_result", "clear_listen_result"]
LIGHT_DISABLED_EXPERTS = frozenset({"listener", "notetaker"})


def experts_for_tools(tool_names, *, light: bool = False) -> dict:
    """按当前实际注册的工具生成专家规格，避免向模型暴露不存在的能力。"""
    available = set(tool_names)
    return {
        name: (prompt, [tool for tool in tools if tool in available])
        for name, (prompt, tools) in EXPERTS.items()
        if not (light and name in LIGHT_DISABLED_EXPERTS)
    }


def view_expert_prompts(
    expert_name: Annotated[str, "可选：指定专家名，留空则查看全部"] = "",
) -> str:
    """查看 settings.json 中当前生效的专家提示词与工具白名单。"""
    names = [expert_name.strip()] if expert_name.strip() else list(EXPERTS)
    lines = []
    for name in names:
        item = EXPERTS.get(name)
        if item is None:
            return f"未找到专家 {name!r}；可用专家名：{', '.join(EXPERTS)}"
        prompt, tools = item
        lines.extend([f"### {name}_expert", f"系统提示词:\n{prompt}", f"可用工具: {', '.join(tools) or '无'}", ""])
    return "\n".join(lines).strip()
