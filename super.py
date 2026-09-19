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
import asyncio

from Agent import Agent
from event_bus import Bus
from Tools import Tools
from initer import init
from render import TerminalRenderer, set_renderer
from resident import ResidentManager, register_resident_tools, build_lecture_note_workflow
from runtime.gateway import CapabilityGateway
from config import MODEL
from experts import SUPER_PROMPT, EXPERTS, LISTENER_STATEFUL_TOOLS
from tools_registry import build_client, register_common_tools, build_super_tools


async def main():
    data = await init()  # 加载 Saver + Visal (重模型, 线程池)

    # 所有 Agent 共用的 Tools (挂在 bus 上)
    tools = Tools()
    register_common_tools(tools, data)

    # 共享 client, 注意是AsyncOpenAI
    client = build_client()

    # 终端渲染器：固定底部状态区 + 上方正文滚动区，除正文外每类信息只占一行
    renderer = TerminalRenderer(status_slots=["listener", "debug", "state"])
    set_renderer(renderer)

    # 总线 + 专家工具 (专家标记为 slow_task)
    bus = Bus(tools, max_concurrency=4, renderer=renderer)
    bus.mark_dangerous(["delete_memory", "replace_memory"])  # Agent 调用这两个工具前必须 y/n 确认

    # CapabilityGateway + LegacyAdapter (Phase 1/3+4 接线): 专家调度 / run_workflow 走 gateway 授权
    gateway = CapabilityGateway()
    for name in tools.tool_list:
        gateway.register_tool(name, schema=tools.dict_schema[name])

    # Super 自己也是一个 Agent (只负责路由, 不负责具体读写)
    super_agent = Agent(bus)
    messages = [{"role": "system", "content": SUPER_PROMPT}]

    # 专家工具注册进 bus.tools (标记为 slow_task, Super 调用时异步化) ；
    # 传入 messages 引用, 让专家被调用时能读到「用户 ↔ Super」对话历史 (PassageWrite 总结对话的数据通路)
    build_super_tools(bus, client, messages, gateway)
    # 记忆工具已在上方注册 (见 tools.add_tool(add_memory/retrieve_context)) , Super 复盘直接使用；
    # 切勿重复 add_tool: 同名工具会重复出现在 schema 中 (历史 bug, 已修复)

    # Listener 事件桥：后台转写线程通过 bus.emit_threadsafe 唤醒常驻 Agent。
    data["agent_listener"].attach_bus(bus, asyncio.get_running_loop())
    # 常驻 Agent 管理器：Super 可用 start_resident_agent / stop_resident_agent / resident_status。
    resident_manager = ResidentManager(bus, client, MODEL, agent_specs=EXPERTS,
                                       transcript_provider=data["agent_listener"],
                                       gateway=gateway,
                                       workflows={"notetaker": build_lecture_note_workflow()})
    register_resident_tools(tools, resident_manager)

    bus.mark_slow(tasks=['transcribe_audio','recognize_doc'])

    await bus.io_print("===== Super 多Agent系统启动 (输入 \\exit() 退出) =====")
    try:
        while True:
            # 带 input 的对话回合: 输出提示 + 等输入, 同刻只有一个对话回合
            requiry = await bus.io_dialog("你: ")
            if requiry == "\\exit()":
                await bus.io_print("退出。")
                return

            messages.append({"role": "user", "content": requiry})
            out = await super_agent.run_agent(
                client, messages, MODEL,
                deny_tools=LISTENER_STATEFUL_TOOLS,
            )
            if out:
                await bus.io_print(f"[Super] {out}")
    finally:
        renderer.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
