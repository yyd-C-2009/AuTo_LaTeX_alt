"""terminal.py：多 Agent 直接对话终端（重置版）。

提供系统指令：
    /db                 查看本地记忆数据库内容
    /db delete <ID> <精确文本>     按 ID+精确文本删除记忆（带确认）
    /db replace <ID> <旧文本> <新文本> 按 ID+旧文本替换记忆（带确认）
    /preferences [list|export|add|update|forget] 管理已确认的长期偏好
    /mode [auto|discussion|direct|artifact|explore] 设置本轮交互方式
    /tasks               查看提醒任务
    /task complete <ID>  完成任务并停止提醒
    /task defer <ID> <ISO时间> [时区] 延期任务
    /agent <name>       切换直接对话的 Agent 身份
    /agent              查看当前 Agent 与可用 Agent
    /agents             列出可用 Agent
    /branch             列出全部对话分支
    /branch fork <name> 从当前对话分叉出新分支并切换
    /branch new <name>  用当前 Agent 新建空白分支并切换
    /branch switch <name> 切换对话分支
    /branch group <name> <分组> 设置对话分组
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

普通文本会直接发送给当前 Agent; 切换 Agent 只更换身份，不会删除当前分支历史。
"""

import argparse
import asyncio
import copy
import os
import shlex

from Agent import Agent
from event_bus import Bus
from Tools import Tools
from initer import init
from render import set_renderer
from web_ui import WebInput, WebRenderer, WebUIServer
from resident import ResidentManager, register_resident_tools, build_lecture_note_workflow
from runtime.gateway import CapabilityGateway, LegacyPolicyAdapter
from config import MODEL, require
from experts import SUPER_PROMPT, EXPERTS, LISTENER_STATEFUL_TOOLS, experts_for_tools
from tools_registry import build_client, build_super_tools, register_common_tools
from persistence import ConversationStore, WorkflowStore
from personalization import MODE_LABELS, compose_prompt, confirm_auto_mode, handle_preference_command
from plan import ReminderScheduler, handle_task_command
from discussion import prepare_conversation
from pictures import PictureService

ACTIVE_EXPERTS = EXPERTS
AVAILABLE_AGENTS = ["super"] + list(ACTIVE_EXPERTS.keys())

HELP_TEXT = """===== terminal.py 多Agent直接对话终端 =====
系统指令（以 / 开头）：
  /db                  查看本地记忆数据库内容
  /db delete <ID> <精确文本>      按 ID+精确文本删除记忆（带确认）
  /db replace <ID> <旧文本> <新文本> 按 ID+旧文本替换记忆（带确认）
  /preferences [list|export|add|update|forget] 管理已确认的长期偏好
  /mode [auto|discussion|direct|artifact|explore] 设置本轮交互方式
  /tasks               查看提醒任务
  /task complete <ID>  完成任务并停止提醒
  /task defer <ID> <ISO时间> [时区] 延期任务
  /agent <name>        切换直接对话的 Agent 身份（super/math/mathwrite/passagewrite/draw/listener）
  /agent               查看当前 Agent 与可用 Agent
  /agents              列出可用 Agent
  /branch              列出全部对话分支
  /branch fork <name>  从当前对话分叉出新分支并切换
  /branch new <name>   用当前 Agent 新建空白分支并切换
  /branch switch <name> 切换对话分支
  /branch group <name> <分组> 设置对话分组
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
普通文本会直接发送给当前 Agent; 切换 Agent 只更换身份，不会删除当前分支历史。
"""


def agent_prompt(agent: str) -> str:
    if agent == "super":
        return SUPER_PROMPT
    return ACTIVE_EXPERTS[agent][0]


def agent_tool_names(agent: str):
    if agent == "super":
        return None
    return ACTIVE_EXPERTS[agent][1]


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
    这样 build_super_tools 闭包引用的 list 始终是当前分支的实时消息; 分支切换时保存/恢复
    该 list 的深拷贝快照。切换 Agent 只替换 system prompt，不删除已有 user/assistant 历史。
    """

    def __init__(self, bus: Bus, client, agent_memory, resident_manager=None, store=None,
                 preferences=None, task_store=None, mode_provider=None, mode_setter=None,
                 picture_service=None):
        self.bus = bus
        self.client = client
        self.agent_memory = agent_memory
        self.resident_manager = resident_manager
        self.store = store
        self.preferences = preferences
        self.task_store = task_store
        self.mode_provider = mode_provider
        self.mode_setter = mode_setter
        self.picture_service = picture_service
        self.conversation_id = store.latest_conversation() if store else None
        if not self.conversation_id and store:
            self.conversation_id = store.create_conversation()
        loaded = store.branches(self.conversation_id) if store and self.conversation_id else []
        self.branches = {}
        for branch in loaded:
            # light 模式恢复到曾使用 listener/notetaker 的分支时安全回退到 Super。
            saved_agent = branch["agent"] if branch["agent"] in AVAILABLE_AGENTS else "super"
            self.branches[branch["name"]] = {
                "agent": saved_agent, "group": branch.get("group_name", "未分组"),
                "messages": store.load_messages(branch["id"]), "db_id": branch["id"],
            }
        if not self.branches:
            db_id = store.create_branch(self.conversation_id, "main", "super") if store else None
            self.branches["main"] = {"agent": "super", "group": "未分组",
                                      "messages": fresh_messages("super"), "db_id": db_id}
        initial_branch = "main" if "main" in self.branches else next(iter(self.branches))
        self.super_history = copy.deepcopy(self.branches[initial_branch]["messages"])
        self.active_branch = initial_branch
        self.active_messages = self.super_history
        self.runner = Agent(bus)
        self._set_agent_prompt(self.agent)

    @property
    def agent(self) -> str:
        return self.branches[self.active_branch]["agent"]

    # ---------- 分支/身份管理 ----------

    def _set_agent_prompt(self, agent: str):
        """替换当前消息列表的 system prompt 为指定 Agent 的 prompt; 保留其余历史。"""
        if self.super_history and self.super_history[0].get("role") == "system":
            self.super_history[0] = {"role": "system", "content": agent_prompt(agent)}
        else:
            self.super_history.insert(0, {"role": "system", "content": agent_prompt(agent)})

    def save_active(self):
        self.branches[self.active_branch]["messages"] = copy.deepcopy(self.super_history)
        if self.store:
            self.store.replace_messages(self.branches[self.active_branch]["db_id"], self.super_history)

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
        source = self.branches[self.active_branch]
        group = source.get("group", "未分组")
        db_id = self.store.create_branch(self.conversation_id, name, source["agent"], source.get("db_id"), group) if self.store else None
        self.branches[name] = {"agent": source["agent"], "group": group,
                               "messages": copy.deepcopy(source["messages"]), "db_id": db_id}
        return self.switch_branch(name)

    def new_branch(self, name: str) -> tuple[bool, str]:
        if name in self.branches:
            return False, f"分支 {name!r} 已存在"
        self.save_active()
        agent = self.agent
        source = self.branches[self.active_branch]
        group = source.get("group", "未分组")
        db_id = self.store.create_branch(self.conversation_id, name, agent, source.get("db_id"), group) if self.store else None
        self.branches[name] = {"agent": agent, "group": group,
                               "messages": fresh_messages(agent), "db_id": db_id}
        return self.switch_branch(name)

    def rename_branch(self, old_name: str, new_name: str) -> tuple[bool, str]:
        new_name = " ".join(str(new_name).split()).strip()
        if old_name not in self.branches:
            return False, f"分支 {old_name!r} 不存在"
        if not new_name:
            return False, "分支名称不能为空"
        if new_name != old_name and new_name in self.branches:
            return False, f"分支 {new_name!r} 已存在"
        if new_name == old_name:
            return True, "名称未变化"
        branch = self.branches[old_name]
        items = []
        for name, data in self.branches.items():
            items.append((new_name if name == old_name else name, data))
        self.branches = dict(items)
        if self.active_branch == old_name:
            self.active_branch = new_name
        if self.store:
            self.store.rename_branch(branch["db_id"], new_name)
        return True, f"已重命名为 {new_name}"

    def remove_branch(self, name: str) -> tuple[bool, str]:
        if name not in self.branches:
            return False, f"分支 {name!r} 不存在"
        if name == self.active_branch:
            return False, "不能删除当前分支; 请先 /branch switch 到其他分支"
        if self.store:
            self.store.delete_branch(self.branches[name]["db_id"])
        del self.branches[name]
        return True, f"已删除分支 {name}"

    def group_branch(self, name: str, group_name: str) -> tuple[bool, str]:
        group_name = " ".join(str(group_name).split()).strip() or "未分组"
        if name not in self.branches:
            return False, f"分支 {name!r} 不存在"
        if len(group_name) > 40:
            return False, "分组名称不能超过 40 个字符"
        self.branches[name]["group"] = group_name
        if self.store:
            self.store.set_branch_group(self.branches[name]["db_id"], group_name)
        return True, f"已将 {name} 放入“{group_name}”"

    def delete_branch(self, name: str) -> tuple[bool, str]:
        if name not in self.branches:
            return False, f"分支 {name!r} 不存在"
        if len(self.branches) <= 1:
            return False, "至少保留一个对话"
        if name == self.active_branch:
            target = next(item for item in self.branches if item != name)
            self.switch_branch(target)
        branch = self.branches.pop(name)
        if self.store:
            self.store.delete_branch(branch["db_id"])
        return True, f"已删除分支 {name}"

    def switch_agent(self, agent: str) -> tuple[bool, str]:
        if agent not in AVAILABLE_AGENTS:
            return False, f"未知 Agent {agent!r}; 可用：{', '.join(AVAILABLE_AGENTS)}"
        # 切换身份只替换 system prompt，保留已有对话历史，绝不删除。
        self.branches[self.active_branch]["agent"] = agent
        if self.store:
            self.store.set_agent(self.branches[self.active_branch]["db_id"], agent)
        self._set_agent_prompt(agent)
        self.active_messages = self.super_history
        self.branches[self.active_branch]["messages"] = copy.deepcopy(self.super_history)
        self.runner = Agent(self.bus)  # 身份切换后重置 Agent 状态（pending/delayed_results）
        return True, (
            f"已切换到直接对话 Agent：{agent}（当前分支 {self.active_branch} 的对话历史已保留; "
            "可直接让 Super/PassageWrite 基于上面的历史进行记录）"
        )

    # ---------- 系统指令 ----------

    async def cmd_db(self):
        if self.agent_memory is None:
            await self.bus.io_print("light 模式未启用记忆数据库。请不带 --light 重新启动后使用 /db。")
            return
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
        if self.agent_memory is None:
            await self.bus.io_print("light 模式未启用记忆数据库。")
            return
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
        result = await asyncio.to_thread(
            self.agent_memory.delete_memory, memory_id, exact_text=exact_text
        )
        await self.bus.io_print(result)

    async def cmd_db_replace(self, memory_id: str, old_text: str, new_text: str):
        if self.agent_memory is None:
            await self.bus.io_print("light 模式未启用记忆数据库。")
            return
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

    async def cmd_workflow(self):
        """显示最近工作流、阶段及已记录的产物哈希。"""
        runs = WorkflowStore(require("runtime.persistence_database")).recent_runs()
        if not runs:
            await self.bus.io_print("（尚无工作流记录）")
            return
        for run in runs:
            await self.bus.io_print(f"{run['id'][:8]}  {run['workflow_name']}  {run['status']}  目标：{run['goal']}")
            for stage in run["stages"]:
                suffix = f"；错误：{stage['error']}" if stage["error"] else ""
                await self.bus.io_print(f"  - {stage['stage_name']}: {stage['status']}{suffix}")
            for artifact in run["artifacts"]:
                await self.bus.io_print(f"  - 产物：{artifact['path']} sha256={artifact['checksum'][:12]}")

    async def handle_command(self, line: str):
        cmdline = line[1:].strip()
        if not cmdline:
            return
        if await handle_preference_command(cmdline, self.bus, self.preferences):
            return
        if await handle_task_command(cmdline, self.bus, self.task_store):
            return
        if cmdline == "mode":
            mode = self.mode_provider() if self.mode_provider else "direct"
            await self.bus.io_print(
                f"当前方式：{MODE_LABELS.get(mode, mode)}\n"
                f"用法：/mode {'|'.join(MODE_LABELS)}"
            )
            return
        if cmdline.startswith("mode "):
            mode = cmdline[5:].strip()
            if mode not in MODE_LABELS:
                await self.bus.io_print(f"可用方式：{', '.join(MODE_LABELS)}")
            elif self.mode_setter and self.mode_setter(mode):
                await self.bus.io_print(f"本轮方式已设为：{MODE_LABELS[mode]}")
            else:
                await self.bus.io_print("无法切换本轮方式。")
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
        if cmdline in ("workflow", "workflows"):
            await self.cmd_workflow()
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
        if cmdline.startswith("branch group "):
            try:
                parts = shlex.split(cmdline[len("branch group "):])
            except ValueError as exc:
                await self.bus.io_print(f"参数解析失败：{exc}")
                return
            if len(parts) < 2:
                await self.bus.io_print("用法：/branch group <对话名> <分组名>")
                return
            ok, msg = self.group_branch(parts[0], " ".join(parts[1:]))
            await self.bus.io_print(msg if ok else f"错误：{msg}")
            return
        await self.bus.io_print(f"未知指令 /{cmdline}; 输入 /help 查看帮助")

    async def chat(self, user_text: str, attachments=()):
        agent = self.agent
        user_text = user_text.strip() or ("请查看我附上的资料。" if attachments else "")
        model_user_text = user_text
        if attachments and self.picture_service is not None:
            renderer = self.mode_setter.__self__ if self.mode_setter and hasattr(self.mode_setter, "__self__") else None
            model_user_text = await self.picture_service.prepare_turn(
                self.client, MODEL, self.conversation_id, user_text, list(attachments),
                status=(lambda value: renderer.status_set("state", value)) if renderer else None,
            )
        mode = self.mode_provider() if self.mode_provider else "direct"
        automatic = mode == "auto"
        if mode == "auto":
            branch = self.branches[self.active_branch]
            summary = (
                self.store.discussion_summary(branch["db_id"])
                if self.store and branch.get("db_id") else None
            )
            mode = await confirm_auto_mode(
                self.client, MODEL, self.active_messages, user_text, self.bus,
                discussion_summary=(summary or {}).get("summary", ""),
            )
            if mode is None:
                await self.bus.io_print("本次请求已取消。")
                if self.mode_setter and hasattr(self.mode_setter, "__self__"):
                    renderer = self.mode_setter.__self__
                    if hasattr(renderer, "sync_sessions"):
                        renderer.sync_sessions(self, force=True)
                return
        if self.mode_setter and hasattr(self.mode_setter, "__self__"):
            renderer = self.mode_setter.__self__
            if hasattr(renderer, "set_turn_mode"):
                renderer.set_turn_mode(mode)
            if automatic and hasattr(renderer, "maybe_rename_session"):
                renderer.maybe_rename_session(user_text)
        self.active_messages.append({"role": "user", "content": model_user_text})
        base_prompt = agent_prompt(agent)
        if agent == "super":
            context_prompt = compose_prompt(
                base_prompt, mode, user_text, self.preferences,
                allow_memory_retrieval=self.agent_memory is not None,
                allow_history_search=True,
                allow_python=agent == "super" or "run_python" in (agent_tool_names(agent) or []),
            )
        else:
            from personalization import turn_context
            can_retrieve_memory = (
                self.agent_memory is not None
                and "retrieve_context" in (agent_tool_names(agent) or [])
            )
            can_search_history = "search_discussion_history" in (agent_tool_names(agent) or [])
            context_prompt = (
                f"{base_prompt.rstrip()}\n\n"
                + turn_context(
                    mode, user_text, self.preferences,
                    allow_preference_tools=False,
                    allow_memory_retrieval=can_retrieve_memory,
                    allow_history_search=can_search_history,
                    allow_python=agent == "super" or "run_python" in (agent_tool_names(agent) or []),
                )
            )
        self.active_messages[0]["content"] = context_prompt
        branch_id = self.branches[self.active_branch].get("db_id")
        call_messages = await prepare_conversation(
            self.client, self.active_messages, self.store, branch_id, MODEL,
            memory=self.agent_memory,
        )
        call_message_count = len(call_messages)
        try:
            out = await self.runner.run_agent(
                self.client,
                call_messages,
                MODEL,
                tool_names=agent_tool_names(agent),
                deny_tools=agent_deny_tools(agent),
            )
        finally:
            if call_messages is not self.active_messages:
                self.active_messages.extend(copy.deepcopy(call_messages[call_message_count:]))
            self.active_messages[0]["content"] = base_prompt
            if self.mode_setter and hasattr(self.mode_setter, "__self__"):
                renderer = self.mode_setter.__self__
                if hasattr(renderer, "set_turn_mode"):
                    renderer.set_turn_mode(renderer.mode)
        if out:
            await self.bus.io_print(f"[{agent}] {out}")
        else:
            await self.bus.io_print(f"[{agent}] （无回复/超时）")
        self.save_active()


async def main(light: bool = False):
    global ACTIVE_EXPERTS, AVAILABLE_AGENTS

    data = await init(light=light)
    conversation_store = ConversationStore(require("runtime.persistence_database"))
    data["conversation_store"] = conversation_store
    data["picture_service"] = None if light else PictureService(conversation_store)

    # 公共工具 + client + bus（与 super.py 同一套注册逻辑）
    tools = Tools()
    register_common_tools(tools, data)
    ACTIVE_EXPERTS = experts_for_tools(tools.tool_list, light=light)
    AVAILABLE_AGENTS = ["super"] + list(ACTIVE_EXPERTS)
    client = build_client()
    data["picture_client"] = client

    # 网页渲染器：正文由浏览器排版 Markdown/公式，输入框原生支持多行编辑。
    renderer = WebRenderer(asyncio.get_running_loop(), status_slots=["listener", "debug", "state"])
    renderer.set_uploads_enabled(not light)
    ui_server = WebUIServer(renderer)
    ui_url = ui_server.start()
    set_renderer(renderer)
    print(f"网页界面已启动，请在浏览器打开：{ui_url}")

    bus = Bus(tools, max_concurrency=4, renderer=renderer)
    bus.mark_dangerous([
        "delete_memory", "replace_memory", "add_memory", "remember_preference",
        "update_preference", "forget_preference", "create_task",
        "complete_task", "defer_task",
    ])  # 写入或停用长期数据前要求用户确认

    # CapabilityGateway + LegacyAdapter (与 super.py 同构): run_workflow 走 gateway 授权
    gateway = CapabilityGateway()
    for name in tools.tool_list:
        gateway.register_tool(name, schema=tools.dict_schema[name])

    listener = data.get("agent_listener")
    resident_manager = None
    if listener is not None:
        # Listener 事件桥：后台转写线程通过 bus.emit_threadsafe 唤醒常驻 Agent。
        listener.attach_bus(bus, asyncio.get_running_loop())
        resident_manager = ResidentManager(
            bus, client, MODEL,
            agent_specs=EXPERTS,
            transcript_provider=listener,
            gateway=gateway,
            workflows={"notetaker": build_lecture_note_workflow()},
        )
        register_resident_tools(bus.tools, resident_manager)

    session = TerminalSession(bus, client, data["agent_memory"], resident_manager,
                              conversation_store,
                              preferences=data.get("agent_preferences"),
                              task_store=data.get("agent_tasks"),
                              mode_provider=lambda: renderer.mode,
                              mode_setter=renderer.set_mode,
                              picture_service=data.get("picture_service"))
    data["picture_context_provider"] = lambda: (session.conversation_id, session.active_messages)
    # 注册专家工具（math_expert 等），让 Super 身份也能路由专家。
    # 传入 session.super_history 作为稳定的对话历史引用：Super 切换分支时通过
    # super_history[:] 原地更新，专家工具始终读取当前 Super 分支的对话。
    build_super_tools(
        bus, client, session.super_history, gateway, expert_specs=ACTIVE_EXPERTS,
        preference_store=session.preferences, mode_provider=lambda: renderer.turn_mode,
        discussion_summary_provider=lambda: session.store.discussion_summary(
            session.branches[session.active_branch]["db_id"]
        ),
    )

    mode_note = (
        "\n\n当前为 light 模式：未加载记忆、OCR、语音与常驻笔记能力。"
        if light else ""
    )
    await bus.io_print(f"# 多 Agent 直接对话{mode_note}\n\n输入 `/help` 查看指令，`/exit` 退出。")
    reminder_task = asyncio.create_task(ReminderScheduler(data["agent_tasks"], bus).run())
    try:
        while True:
            # 左栏展示持久化分支；仅主对话等待期间允许切换和新建。
            renderer.sync_sessions(session)
            submitted = await bus.io_dialog(f"[{session.active_branch}:{session.agent}] >>> ")
            attachments = list(submitted.attachments) if isinstance(submitted, WebInput) else []
            line = submitted.text.strip() if isinstance(submitted, WebInput) else submitted.strip()
            if not line and not attachments:
                continue
            if not attachments and line in ("/exit", "\\exit", "exit", "quit"):
                await bus.io_print("退出。")
                return
            if not attachments and (line.startswith("/") or line.startswith("\\")):
                await session.handle_command(line)
            else:
                await session.chat(line, attachments)
    finally:
        reminder_task.cancel()
        await asyncio.gather(reminder_task, return_exceptions=True)
        renderer.shutdown()
        ui_server.shutdown()


def parse_args():
    parser = argparse.ArgumentParser(description="AuTo_LaTeX 多 Agent 直接对话")
    parser.add_argument(
        "--light", action="store_true",
        help="轻量启动：不导入或加载 BGE、Pix2Text、Faster-Whisper",
    )
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main(light=parse_args().light))
