"""terminal.py：多 Agent 直接对话终端（重置版）。

提供系统指令：
    /db                 查看本地记忆数据库内容
    /agent <name>       切换直接对话的 Agent 身份
    /agent              查看当前 Agent 与可用 Agent
    /agents             列出可用 Agent
    /branch             列出全部对话分支
    /branch fork <name> 从当前对话分叉出新分支并切换
    /branch new <name>  用当前 Agent 新建空白分支并切换
    /branch switch <name> 切换对话分支
    /branch rm <name>   删除分支
    /cd <path>          切换当前工作目录
    /pwd                显示当前工作目录
    /whoami             查看当前分支与 Agent
    /help               显示本帮助
    /exit               退出

普通文本会直接发送给当前 Agent；切换 Agent 会清空当前分支历史。
"""

import asyncio
import copy
import os

from Agent import Agent
from event_bus import Bus
from Tools import Tools
from initer import init
from super import (
    SUPER_PROMPT,
    EXPERTS,
    MODEL,
    LISTENER_STATEFUL_TOOLS,
    build_client,
    build_super_tools,
    register_common_tools,
)

AVAILABLE_AGENTS = ["super"] + list(EXPERTS.keys())

HELP_TEXT = """===== terminal.py 多Agent直接对话终端 =====
系统指令（以 / 开头）：
  /db                  查看本地记忆数据库内容
  /agent <name>        切换直接对话的 Agent 身份（super/math/mathwrite/passagewrite/draw/listener）
  /agent               查看当前 Agent 与可用 Agent
  /agents              列出可用 Agent
  /branch              列出全部对话分支
  /branch fork <name>  从当前对话分叉出新分支并切换
  /branch new <name>   用当前 Agent 新建空白分支并切换
  /branch switch <name> 切换对话分支
  /branch rm <name>    删除分支（不能删除当前分支）
  /cd <path>           切换当前工作目录
  /pwd                 显示当前工作目录
  /whoami              查看当前分支与 Agent
  /help                显示本帮助
  /exit                退出
普通文本会直接发送给当前 Agent；切换 Agent 会清空当前分支历史。
"""


def agent_prompt(agent: str) -> str:
    if agent == "super":
        return SUPER_PROMPT
    return EXPERTS[agent][0]


def agent_tool_names(agent: str):
    if agent == "super":
        return None
    return EXPERTS[agent][1]


def agent_deny_tools(agent: str):
    # Super 直接对话时禁用连续监听的有状态工具，必须通过 listener_expert 间接管理，
    # 避免与 listener_expert 轮流读取/清空同一份累积转写造成状态竞争。
    if agent == "super":
        return LISTENER_STATEFUL_TOOLS
    return None


def fresh_messages(agent: str) -> list:
    return [{"role": "system", "content": agent_prompt(agent)}]


class TerminalSession:
    """维护当前 Agent、分支与对话历史。"""

    def __init__(self, bus: Bus, client, agent_memory):
        self.bus = bus
        self.client = client
        self.agent_memory = agent_memory
        # Super 专家工具的回传闭包需要引用一个稳定的 list，因此 super 分支的
        # 实时消息统一使用 self.super_history，分支中保存其深拷贝快照。
        self.super_history = fresh_messages("super")
        self.branches = {
            "main": {"agent": "super", "messages": copy.deepcopy(self.super_history)}
        }
        self.active_branch = "main"
        self.active_messages = self.super_history
        self.runner = Agent(bus)

    @property
    def agent(self) -> str:
        return self.branches[self.active_branch]["agent"]

    # ---------- 分支/身份管理 ----------

    def save_active(self):
        if self.agent == "super":
            self.branches[self.active_branch]["messages"] = copy.deepcopy(self.super_history)
        else:
            self.branches[self.active_branch]["messages"] = self.active_messages

    def switch_branch(self, name: str) -> tuple[bool, str]:
        if name not in self.branches:
            return False, f"分支 {name!r} 不存在"
        if name == self.active_branch:
            return True, f"已在分支 {name}（agent={self.agent}）"
        self.save_active()
        self.active_branch = name
        if self.agent == "super":
            self.super_history[:] = copy.deepcopy(self.branches[name]["messages"])
            self.active_messages = self.super_history
        else:
            self.active_messages = self.branches[name]["messages"]
        self.runner = Agent(self.bus)  # 分支切换后重置 Agent 状态（pending/delayed_results）
        return True, f"已切换到分支 {name}（agent={self.agent}）"

    def fork_branch(self, name: str) -> tuple[bool, str]:
        if name in self.branches:
            return False, f"分支 {name!r} 已存在"
        self.save_active()
        self.branches[name] = copy.deepcopy(self.branches[self.active_branch])
        return self.switch_branch(name)

    def new_branch(self, name: str) -> tuple[bool, str]:
        if name in self.branches:
            return False, f"分支 {name!r} 已存在"
        self.save_active()
        agent = self.agent
        self.branches[name] = {"agent": agent, "messages": fresh_messages(agent)}
        return self.switch_branch(name)

    def remove_branch(self, name: str) -> tuple[bool, str]:
        if name not in self.branches:
            return False, f"分支 {name!r} 不存在"
        if name == self.active_branch:
            return False, "不能删除当前分支；请先 /branch switch 到其他分支"
        del self.branches[name]
        return True, f"已删除分支 {name}"

    def switch_agent(self, agent: str) -> tuple[bool, str]:
        if agent not in AVAILABLE_AGENTS:
            return False, f"未知 Agent {agent!r}；可用：{', '.join(AVAILABLE_AGENTS)}"
        self.branches[self.active_branch]["agent"] = agent
        if agent == "super":
            self.super_history[:] = fresh_messages("super")
            self.active_messages = self.super_history
            self.branches[self.active_branch]["messages"] = copy.deepcopy(self.super_history)
        else:
            self.active_messages = fresh_messages(agent)
            self.branches[self.active_branch]["messages"] = self.active_messages
        self.runner = Agent(self.bus)  # 身份切换后重置 Agent 状态（pending/delayed_results）
        return True, (
            f"已切换到直接对话 Agent：{agent}（当前分支 {self.active_branch} 的历史已清空；"
            "如需保留旧对话，请先 /branch fork <name> 保存）"
        )

    # ---------- 系统指令 ----------

    async def cmd_db(self):
        memories = await asyncio.to_thread(self.agent_memory.list_memories)
        if not memories:
            await self.bus.io_print("数据库为空。")
            return
        await self.bus.io_print(f"数据库共 {len(memories)} 条记忆：")
        for i, mem in enumerate(memories, 1):
            text = mem.get("text", "")
            meta = mem.get("metadata")
            suffix = f" | meta={meta}" if meta else ""
            await self.bus.io_print(f"{i}. [{mem.get('id', '')[:8]}] {text}{suffix}")

    async def cmd_branch_list(self):
        await self.bus.io_print(f"当前分支：{self.active_branch}（agent={self.agent}）")
        if not self.branches:
            await self.bus.io_print("（无分支）")
            return
        for name, br in self.branches.items():
            n = len(br.get("messages", []))
            mark = "*" if name == self.active_branch else " "
            await self.bus.io_print(f" {mark} {name}  agent={br.get('agent')}  messages={n}")

    async def handle_command(self, line: str):
        cmdline = line[1:].strip()
        if not cmdline:
            return
        if cmdline in ("help", "?"):
            await self.bus.io_print(HELP_TEXT)
            return
        if cmdline in ("db", "memories"):
            await self.cmd_db()
            return
        if cmdline == "agent":
            await self.bus.io_print(
                f"当前直接对话 Agent：{self.agent}\n"
                f"可用 Agent：{', '.join(AVAILABLE_AGENTS)}\n"
                "用法：/agent <name>"
            )
            return
        if cmdline == "agents":
            await self.bus.io_print(f"可用 Agent：{', '.join(AVAILABLE_AGENTS)}")
            return
        if cmdline.startswith("agent "):
            ok, msg = self.switch_agent(cmdline[6:].strip())
            await self.bus.io_print(msg if ok else f"错误：{msg}")
            return
        if cmdline == "pwd":
            await self.bus.io_print(f"当前工作目录：{os.getcwd()}")
            return
        if cmdline.startswith("cd "):
            path = cmdline[3:].strip()
            if not path:
                await self.bus.io_print("错误：/cd 后需要路径，例如 /cd ../latex_output")
                return
            try:
                os.chdir(os.path.expanduser(path))
                await self.bus.io_print(f"工作目录已切换到：{os.getcwd()}")
            except Exception as e:
                await self.bus.io_print(f"切换工作目录失败：{type(e).__name__}: {e}")
            return
        if cmdline == "whoami":
            await self.bus.io_print(f"当前分支：{self.active_branch}，当前 Agent：{self.agent}")
            return
        if cmdline in ("branch", "branch list"):
            await self.cmd_branch_list()
            return
        if cmdline.startswith("branch fork "):
            ok, msg = self.fork_branch(cmdline[len("branch fork "):].strip())
            await self.bus.io_print(msg if ok else f"错误：{msg}")
            return
        if cmdline.startswith("branch new "):
            ok, msg = self.new_branch(cmdline[len("branch new "):].strip())
            await self.bus.io_print(msg if ok else f"错误：{msg}")
            return
        if cmdline.startswith("branch switch "):
            ok, msg = self.switch_branch(cmdline[len("branch switch "):].strip())
            await self.bus.io_print(msg if ok else f"错误：{msg}")
            return
        if cmdline.startswith("branch rm "):
            ok, msg = self.remove_branch(cmdline[len("branch rm "):].strip())
            await self.bus.io_print(msg if ok else f"错误：{msg}")
            return
        await self.bus.io_print(f"未知指令 /{cmdline}；输入 /help 查看帮助")

    async def chat(self, user_text: str):
        agent = self.agent
        self.active_messages.append({"role": "user", "content": user_text})
        out = await self.runner.run_agent(
            self.client,
            self.active_messages,
            MODEL,
            tool_names=agent_tool_names(agent),
            deny_tools=agent_deny_tools(agent),
        )
        if out:
            await self.bus.io_print(f"[{agent}] {out}")
        else:
            await self.bus.io_print(f"[{agent}] （无回复/超时）")


async def main():
    data = await init()  # 加载 Saver + Visal（重模型）

    # 公共工具 + client + bus（与 super.py 同一套注册逻辑）
    tools = Tools()
    register_common_tools(tools, data)
    client = build_client()
    bus = Bus(tools, max_concurrency=4)

    session = TerminalSession(bus, client, data["agent_memory"])
    # 注册专家工具（math_expert 等），让 Super 身份也能路由专家。
    # 传入 session.super_history 作为稳定的对话历史引用：Super 切换分支时通过
    # super_history[:] 原地更新，专家工具始终读取当前 Super 分支的对话。
    build_super_tools(bus, client, session.super_history)

    await bus.io_print("===== terminal.py 多Agent直接对话终端（输入 /help 查看指令，/exit 退出）=====")
    while True:
        line = await bus.io_dialog(f"[{session.active_branch}:{session.agent}] >>> ")
        line = line.strip()
        if not line:
            continue
        if line in ("/exit", "\\exit", "exit", "quit"):
            await bus.io_print("退出。")
            return
        if line.startswith("/") or line.startswith("\\"):
            await session.handle_command(line)
        else:
            await session.chat(line)


if __name__ == "__main__":
    asyncio.run(main())
