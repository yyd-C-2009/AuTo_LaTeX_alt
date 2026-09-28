'''
组织者 Agent 的控制程序, 负责判断任务内容是否明确清晰, 推测补全所需的细节计划, 同时对各个 Agent 的返回做出判定

上下文管理模式:
用户上下文+自身计划完成情况上下文 = 一般上下文

一般上下文 + 分配任务的结果上下文 = 鉴定完成情况上下文

鉴定完成情况上下文 + 自省提示词 = 复盘上下文

本文件只做启动接线 + Super REPL; 各部件分居:
    config.py         模型端点与输出目录
    experts.py        SUPER_PROMPT / EXPERTS / LISTENER_STATEFUL_TOOLS
    latex_tools.py    write_latex / check_latex / check_tikz / view_theorem_style
    tools_registry.py build_client / register_common_tools / build_super_tools
'''
import argparse
import asyncio
import copy

from Agent import Agent
from event_bus import Bus
from Tools import Tools
from initer import init
from render import set_renderer
from web_ui import WebInput, WebRenderer, WebUIServer
from resident import ResidentManager, register_resident_tools, build_lecture_note_workflow
from runtime.gateway import CapabilityGateway
from config import MODEL, require
from experts import SUPER_PROMPT, EXPERTS, LISTENER_STATEFUL_TOOLS, experts_for_tools
from tools_registry import build_client, register_common_tools, build_super_tools
from personalization import MODE_LABELS, compose_prompt, confirm_auto_mode, handle_preference_command
from plan import handle_task_command
from persistence import ConversationStore
from discussion import prepare_conversation
from pictures import PictureService


class SuperSessions:
    """Super 入口的持久化会话；原地切换消息列表以保留专家工具的引用。"""

    def __init__(self, messages, store):
        self.store = store
        self.conversation_id = store.latest_conversation("super") or store.create_conversation("super")
        branches = store.branches(self.conversation_id)
        self.branches = {
            item["name"]: {
                "agent": item["agent"],
                "group": item.get("group_name", "未分组"),
                "messages": store.load_messages(item["id"]),
                "db_id": item["id"],
            }
            for item in branches
        }
        if not self.branches:
            branch_id = store.create_branch(self.conversation_id, "main", "super")
            self.branches["main"] = {"agent": "super", "group": "未分组",
                                      "messages": copy.deepcopy(messages), "db_id": branch_id}
        for branch in self.branches.values():
            if not branch["messages"]:
                branch["messages"] = [{"role": "system", "content": SUPER_PROMPT}]
        self.active_branch = "main" if "main" in self.branches else next(iter(self.branches))
        self.agent = "super"
        self.active_messages = messages
        self.active_messages[:] = copy.deepcopy(self.branches[self.active_branch]["messages"])
        self.save_active()

    def save_active(self):
        self.branches[self.active_branch]["messages"] = copy.deepcopy(self.active_messages)
        self.store.replace_messages(self.branches[self.active_branch]["db_id"], self.active_messages)

    def switch_branch(self, name):
        if name not in self.branches:
            return False, "会话不存在"
        self.save_active()
        self.active_branch = name
        self.active_messages[:] = copy.deepcopy(self.branches[name]["messages"])
        return True, "已切换"

    def new_branch(self, name):
        if name in self.branches:
            return False, "会话已存在"
        parent = self.branches[self.active_branch]["db_id"]
        group = self.branches[self.active_branch].get("group", "未分组")
        branch_id = self.store.create_branch(self.conversation_id, name, "super", parent, group)
        self.branches[name] = {
            "agent": "super", "group": group,
            "messages": [{"role": "system", "content": SUPER_PROMPT}],
            "db_id": branch_id,
        }
        return self.switch_branch(name)

    def group_branch(self, name, group_name):
        group_name = " ".join(str(group_name).split()).strip() or "未分组"
        if name not in self.branches:
            return False, "会话不存在"
        if len(group_name) > 40:
            return False, "分组名称不能超过 40 个字符"
        branch = self.branches[name]
        branch["group"] = group_name
        self.store.set_branch_group(branch["db_id"], group_name)
        return True, "已更新分组"

    def delete_branch(self, name):
        if name not in self.branches:
            return False, "会话不存在"
        if len(self.branches) <= 1:
            return False, "至少保留一个会话"
        if name == self.active_branch:
            target = next(item for item in self.branches if item != name)
            self.switch_branch(target)
        branch = self.branches.pop(name)
        self.store.delete_branch(branch["db_id"])
        callback = getattr(self, "on_delete", None)
        if callback:
            callback(name)
        return True, "已删除会话"

    def rename_branch(self, old_name, new_name):
        new_name = " ".join(str(new_name).split()).strip()
        if old_name not in self.branches:
            return False, "会话不存在"
        if not new_name:
            return False, "会话名称不能为空"
        if new_name != old_name and new_name in self.branches:
            return False, "会话名称已存在"
        if new_name == old_name:
            return True, "名称未变化"
        items = []
        for name, data in self.branches.items():
            items.append((new_name if name == old_name else name, data))
        self.branches = dict(items)
        self.store.rename_branch(self.branches[new_name]["db_id"], new_name)
        if self.active_branch == old_name:
            self.active_branch = new_name
        callback = getattr(self, "on_rename", None)
        if callback:
            callback(old_name, new_name)
        return True, "已重命名"


async def main(light: bool = False):
    data = await init(light=light)
    conversation_store = ConversationStore(require("runtime.persistence_database"))
    data["conversation_store"] = conversation_store
    data["picture_service"] = None if light else PictureService(conversation_store)

    # 所有 Agent 共用的 Tools (挂在 bus 上)
    tools = Tools()
    register_common_tools(tools, data)
    expert_specs = experts_for_tools(tools.tool_list, light=light)

    # 共享 client, 注意是AsyncOpenAI
    client = build_client()
    data["picture_client"] = client

    # 网页渲染器：主区 Markdown/KaTeX，右栏状态，底部多行输入；所有行数由 CSS 自适应。
    renderer = WebRenderer(asyncio.get_running_loop(), status_slots=["listener", "debug", "state"])
    renderer.set_uploads_enabled(not light)
    ui_server = WebUIServer(renderer)
    ui_url = ui_server.start()
    set_renderer(renderer)
    print(f"网页界面已启动，请在浏览器打开：{ui_url}")

    # 总线 + 专家工具 (专家标记为 slow_task)
    bus = Bus(tools, max_concurrency=4, renderer=renderer)
    bus.mark_dangerous([
        "delete_memory", "replace_memory", "add_memory", "remember_preference",
        "update_preference", "forget_preference", "create_task",
        "complete_task", "defer_task",
    ])  # 写入或停用长期数据前要求用户确认

    # CapabilityGateway + LegacyAdapter (Phase 1/3+4 接线): 专家调度 / run_workflow 走 gateway 授权
    gateway = CapabilityGateway()
    for name in tools.tool_list:
        gateway.register_tool(name, schema=tools.dict_schema[name])

    # Super 自己也是一个 Agent (只负责路由, 不负责具体读写)
    super_agent = Agent(bus)
    messages = [{"role": "system", "content": SUPER_PROMPT}]
    sessions = SuperSessions(messages, conversation_store)
    data["picture_context_provider"] = lambda: (sessions.conversation_id, sessions.active_messages)
    agents = {name: Agent(bus) for name in sessions.branches}  # 各会话隔离 Agent 的延迟任务状态。
    sessions.on_rename = lambda old, new: agents.__setitem__(new, agents.pop(old)) if old in agents else None
    sessions.on_delete = lambda name: agents.pop(name, None)

    # 专家工具注册进 bus.tools (标记为 slow_task, Super 调用时异步化) ；
    # 传入 messages 引用, 让专家被调用时能读到「用户 ↔ Super」对话历史 (PassageWrite 总结对话的数据通路)
    preferences = data.get("agent_preferences")
    build_super_tools(
        bus, client, messages, gateway, expert_specs=expert_specs,
        preference_store=preferences, mode_provider=lambda: renderer.turn_mode,
        discussion_summary_provider=lambda: sessions.store.discussion_summary(
            sessions.branches[sessions.active_branch]["db_id"]
        ),
    )
    # 记忆工具已在上方注册 (见 tools.add_tool(add_memory/retrieve_context)) , Super 复盘直接使用；
    # 切勿重复 add_tool: 同名工具会重复出现在 schema 中 (历史 bug, 已修复)

    listener = data.get("agent_listener")
    if listener is not None:
        # Listener 事件桥：后台转写线程通过 bus.emit_threadsafe 唤醒常驻 Agent。
        listener.attach_bus(bus, asyncio.get_running_loop())
        # 常驻 Agent 依赖 Listener；light 模式不注册这些控制工具。
        resident_manager = ResidentManager(
            bus, client, MODEL, agent_specs=EXPERTS,
            transcript_provider=listener,
            gateway=gateway,
            workflows={"notetaker": build_lecture_note_workflow()},
        )
        register_resident_tools(tools, resident_manager)

    slow_tools = [name for name in ("transcribe_audio", "recognize_doc") if name in tools.tool_list]
    bus.mark_slow(tasks=slow_tools)
    from plan import ReminderScheduler
    reminder_task = asyncio.create_task(ReminderScheduler(data["agent_tasks"], bus).run())

    mode_note = (
        "\n\n当前为 light 模式：未加载记忆、OCR、语音与常驻笔记能力。"
        if light else ""
    )
    await bus.io_print(
        f"# Super 多 Agent 系统{mode_note}\n\n"
        "可在网页顶部选择本轮方式；输入 `/preferences` 管理偏好、`/tasks` 查看提醒，`\\exit()` 退出。"
    )
    try:
        while True:
            renderer.sync_sessions(sessions)
            # 带 input 的对话回合: 输出提示 + 等输入, 同刻只有一个对话回合
            submitted = await bus.io_dialog("你: ")
            attachments = list(submitted.attachments) if isinstance(submitted, WebInput) else []
            requiry = submitted.text.strip() if isinstance(submitted, WebInput) else submitted.strip()
            if not requiry and not attachments:
                continue
            if attachments and not requiry:
                requiry = "请查看我附上的资料。"
            if not attachments and requiry == "\\exit()":
                await bus.io_print("退出。")
                return
            if not attachments and requiry.startswith("/"):
                command = requiry[1:].strip()
                if await handle_preference_command(command, bus, preferences):
                    continue
                if await handle_task_command(command, bus, data.get("agent_tasks")):
                    continue
                if command == "mode":
                    await bus.io_print(
                        f"当前方式：{MODE_LABELS.get(renderer.mode, renderer.mode)}\n"
                        f"用法：/mode {'|'.join(MODE_LABELS)}"
                    )
                    continue
                if command.startswith("mode "):
                    mode = command[5:].strip()
                    if mode in MODE_LABELS and renderer.set_mode(mode):
                        await bus.io_print(f"本轮方式已设为：{MODE_LABELS[mode]}")
                    else:
                        await bus.io_print(f"用法：/mode {'|'.join(MODE_LABELS)}")
                    continue

            branch_id = sessions.branches[sessions.active_branch]["db_id"]
            model_user_text = requiry
            if attachments and data.get("picture_service") is not None:
                model_user_text = await data["picture_service"].prepare_turn(
                    client, MODEL, sessions.conversation_id, requiry, attachments,
                    status=lambda value: renderer.status_set("state", value),
                )
            summary = sessions.store.discussion_summary(branch_id)
            mode = renderer.mode
            if mode == "auto":
                mode = await confirm_auto_mode(
                    client, MODEL, messages, requiry, bus,
                    discussion_summary=(summary or {}).get("summary", ""),
                )
                if mode is None:
                    await bus.io_print("本次请求已取消。")
                    renderer.sync_sessions(sessions, force=True)
                    continue
                renderer.maybe_rename_session(requiry)
            renderer.set_turn_mode(mode)
            messages.append({"role": "user", "content": model_user_text})
            if sessions.active_branch not in agents:
                agents[sessions.active_branch] = Agent(bus)
            super_agent = agents[sessions.active_branch]
            messages[0]["content"] = compose_prompt(
                SUPER_PROMPT, mode, requiry, preferences,
                allow_memory_retrieval=data.get("agent_memory") is not None,
                allow_history_search=True,
                allow_python="run_python" in tools.tool_list,
            )
            call_messages = await prepare_conversation(
                client, messages, sessions.store, branch_id, MODEL,
                memory=data.get("agent_memory"),
            )
            call_message_count = len(call_messages)
            try:
                out = await super_agent.run_agent(
                    client, call_messages, MODEL,
                    deny_tools=LISTENER_STATEFUL_TOOLS,
                )
            finally:
                if call_messages is not messages:
                    messages.extend(copy.deepcopy(call_messages[call_message_count:]))
                messages[0]["content"] = SUPER_PROMPT
                sessions.save_active()
                renderer.set_turn_mode(renderer.mode)
            if out:
                await bus.io_print(f"[Super] {out}")
    finally:
        reminder_task.cancel()
        await asyncio.gather(reminder_task, return_exceptions=True)
        renderer.shutdown()
        ui_server.shutdown()


def parse_args():
    parser = argparse.ArgumentParser(description="AuTo_LaTeX Super 多 Agent 系统")
    parser.add_argument(
        "--light", action="store_true",
        help="轻量启动：不导入或加载 BGE、Pix2Text、Faster-Whisper",
    )
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main(light=parse_args().light))
