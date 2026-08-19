"""terminal.py：多 Agent 直接对话终端（重置版）。

提供系统指令：
    /db                 查看本地记忆数据库内容
    /db delete <ID> <精确文本>     按 ID+精确文本删除记忆（带确认）
    /db replace <ID> <旧文本> <新文本> 按 ID+旧文本替换记忆（带确认）
    /agent <name>       切换直接对话的 Agent 身份
    /agent              查看当前 Agent 与可用 Agent
    /agents             列出可用 Agent
    /branch             列出全部对话分支
    /branch fork <name> 从当前对话分叉出新分支并切换
    /branch new <name>  用当前 Agent 新建空白分支并切换
    /branch switch <name> 切换对话分支
    /branch rm <name>   删除分支
    /resident start <agent> [interval] 启动常驻 Agent
    /resident stop <name>               停止常驻 Agent
    /resident list                      列出常驻 Agent
    /resident poke <name> <指令>        手动唤醒常驻 Agent
    /cd <path>          切换当前工作目录
    /pwd                显示当前工作目录
    /whoami             查看当前分支与 Agent
    /help               显示本帮助
    /exit               退出

普通文本会直接发送给当前 Agent；切换 Agent 只更换身份，不会删除当前分支历史。
"""

import asyncio
import copy
import os
import shlex

from Agent import Agent
from event_bus import Bus
from Tools import Tools
from initer import init
from resident import ResidentManager, register_resident_tools
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
  /db delete <ID> <精确文本>      按 ID+精确文本删除记忆（带确认）
  /db replace <ID> <旧文本> <新文本> 按 ID+旧文本替换记忆（带确认）
  /agent <name>        切换直接对话的 Agent 身份（super/math/mathwrite/passagewrite/draw/listener）
  /agent               查看当前 Agent 与可用 Agent
  /agents              列出可用 Agent
  /branch              列出全部对话分支
  /branch fork <name>  从当前对话分叉出新分支并切换
  /branch new <name>   用当前 Agent 新建空白分支并切换
  /branch switch <name> 切换对话分支
  /branch rm <name>    删除分支（不能删除当前分支）
  /resident start <agent> [interval] 启动常驻 Agent
  /resident stop <name>               停止常驻 Agent
  /resident list                      列出常驻 Agent
  /resident poke <name> <指令>        手动唤醒常驻 Agent
  /cd <path>           切换当前工作目录
  /pwd                 显示当前工作目录
  /whoami              查看当前分支与 Agent
  /help                显示本帮助
  /exit                退出
普通文本会直接发送给当前 Agent；切换 Agent 只更换身份，不会删除当前分支历史。
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
    """维护当前 Agent、分支与对话历史。

    当前分支的实时消息统一放在 self.super_history 中（即使当前 Agent 不是 Super），
    这样 build_super_tools 闭包引用的 list 始终是当前分支的实时消息；分支切换时保存/恢复
    该 list 的深拷贝快照。切换 Agent 只替换 system prompt，不删除已有 user/assistant 历史。
    """

    def __init__(self, bus: Bus, client, agent_memory, resident_manager=None):
        self.bus = bus
        self.client = client
        self.agent_memory = agent_memory
        self.resident_manager = resident_manager
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

    def _set_agent_prompt(self, agent: str):
        """替换当前消息列表的 system prompt 为指定 Agent 的 prompt；保留其余历史。"""
        if self.super_history and self.super_history[0].get("role") == "system":
            self.super_history[0] = {"role": "system", "content": agent_prompt(agent)}
        else:
            self.super_history.insert(0, {"role": "system", "content": agent_prompt(agent)})

    def save_active(self):
        self.branches[self.active_branch]["messages"] = copy.deepcopy(self.super_history)

    def switch_branch(self, name: str) -> tuple[bool, str]:
        if name not in self.branches:
            return False, f"分支 {name!r} 不存在"
        if name == self.active_branch:
            return True, f"已在分支 {name}（agent={self.agent}）"
        self.save_active()
        self.active_branch = name
        self.super_history[:] = copy.deepcopy(self.branches[name]["messages"])
        self.active_messages = self.super_history
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
        # 切换身份只替换 system prompt，保留已有对话历史，绝不删除。
        self.branches[self.active_branch]["agent"] = agent
        self._set_agent_prompt(agent)
        self.active_messages = self.super_history
        self.branches[self.active_branch]["messages"] = copy.deepcopy(self.super_history)
        self.runner = Agent(self.bus)  # 身份切换后重置 Agent 状态（pending/delayed_results）
        return True, (
            f"已切换到直接对话 Agent：{agent}（当前分支 {self.active_branch} 的对话历史已保留；"
            "可直接让 Super/PassageWrite 基于上面的历史进行记录）"
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

    async def cmd_db_delete(self, memory_id: str, exact_text: str):
        old = await asyncio.to_thread(self.agent_memory.get_memory_by_id, memory_id)
        if old is None:
            await self.bus.io_print(f"删除失败：ID {memory_id} 不存在。")
            return
        if old.get("text", "") != exact_text:
            await self.bus.io_print(f"删除失败：文本不匹配。\n库中该 ID 对应文本：{old.get('text', '')[:120]}")
            return
        await self.bus.io_print(f"待删除：{old['text'][:100]}")
        confirm = await self.bus.io_dialog(f"确认删除 ID {memory_id}？(y/n) ")
        if confirm.strip().lower() not in ("y", "yes"):
            await self.bus.io_print("已取消删除。")
            return
        result = await asyncio.to_thread(self.agent_memory.delete_memory, memory_id, exact_text)
        await self.bus.io_print(result)

    async def cmd_db_replace(self, memory_id: str, old_text: str, new_text: str):
        old = await asyncio.to_thread(self.agent_memory.get_memory_by_id, memory_id)
        if old is None:
            await self.bus.io_print(f"替换失败：ID {memory_id} 不存在。")
            return
        if old.get("text", "") != old_text:
            await self.bus.io_print(f"替换失败：旧文本不匹配。\n库中该 ID 对应文本：{old.get('text', '')[:120]}")
            return
        if not new_text.strip():
            await self.bus.io_print("替换失败：新文本不能为空。")
            return
        await self.bus.io_print(f"旧文本：{old['text'][:100]}")
        await self.bus.io_print(f"新文本：{new_text[:100]}")
        confirm = await self.bus.io_dialog(f"确认替换 ID {memory_id}？(y/n) ")
        if confirm.strip().lower() not in ("y", "yes"):
            await self.bus.io_print("已取消替换。")
            return
        result = await asyncio.to_thread(
            self.agent_memory.replace_memory, memory_id, old_text, new_text.strip()
        )
        await self.bus.io_print(result)

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
        if cmdline.startswith("db delete "):
            try:
                parts = shlex.split(cmdline)
            except ValueError as e:
                await self.bus.io_print(f"参数解析失败：{e}")
                return
            if len(parts) < 4:
                await self.bus.io_print("用法：/db delete <ID> <精确文本>")
                return
            await self.cmd_db_delete(parts[2], " ".join(parts[3:]))
            return
        if cmdline.startswith("db replace "):
            try:
                parts = shlex.split(cmdline)
            except ValueError as e:
                await self.bus.io_print(f"参数解析失败：{e}")
                return
            if len(parts) < 5:
                await self.bus.io_print("用法：/db replace <ID> <旧文本> <新文本>（文本含空格请加引号）")
                return
            await self.cmd_db_replace(parts[2], parts[3], " ".join(parts[4:]))
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
        if cmdline in ("resident", "resident list"):
            if not self.resident_manager:
                await self.bus.io_print("常驻 Agent 管理器未初始化。")
                return
            await self.bus.io_print(self.resident_manager.list_status())
            return
        if cmdline.startswith("resident start "):
            if not self.resident_manager:
                await self.bus.io_print("常驻 Agent 管理器未初始化。")
                return
            rest = cmdline[len("resident start "):].strip()
            parts = rest.split(maxsplit=1)
            agent_type = parts[0].strip()
            interval = 30.0
            if len(parts) == 2:
                try:
                    interval = float(parts[1].strip())
                except ValueError:
                    await self.bus.io_print("用法：/resident start <agent_type> [interval_sec]")
                    return
            await self.bus.io_print(await self.resident_manager.start(agent_type, interval))
            return
        if cmdline.startswith("resident stop "):
            if not self.resident_manager:
                await self.bus.io_print("常驻 Agent 管理器未初始化。")
                return
            name = cmdline[len("resident stop "):].strip()
            await self.bus.io_print(await self.resident_manager.stop(name))
            return
        if cmdline.startswith("resident poke "):
            if not self.resident_manager:
                await self.bus.io_print("常驻 Agent 管理器未初始化。")
                return
            rest = cmdline[len("resident poke "):].strip()
            parts = rest.split(maxsplit=1)
            if len(parts) != 2:
                await self.bus.io_print("用法：/resident poke <name> <指令>")
                return
            await self.bus.io_print(self.resident_manager.poke(parts[0], parts[1]))
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
    bus.mark_dangerous(["delete_memory", "replace_memory"])  # Agent 调用这两个工具前必须 y/n 确认

    # Listener 事件桥：后台转写线程通过 bus.emit_threadsafe 唤醒常驻 Agent。
    data["agent_listener"].attach_bus(bus, asyncio.get_running_loop())
    resident_manager = ResidentManager(
        bus, client, MODEL,
        agent_specs=EXPERTS,
        transcript_provider=data["agent_listener"],
    )
    register_resident_tools(bus.tools, resident_manager)

    session = TerminalSession(bus, client, data["agent_memory"], resident_manager)
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
