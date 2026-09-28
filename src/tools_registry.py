'''工具注册层: client 构建、公共工具注册、专家工具注册。

super.py 与 terminal.py 共用这里的三个入口 (build_client / register_common_tools /
build_super_tools), 两个入口的接线差异只在于传进来的 messages 引用与 deny 策略。

_dialogue_context / build_task_context 只服务 build_super_tools, 放在同模块避免
super.py ↔ tools_registry 的循环导入。
'''

import os

import openai
from typing import Annotated

from Agent import Agent, view_delayed_results
from event_bus import Bus
from Tools import Tools
from str_replace_editor import str_replace_editor
from built_in_tool import web_search, web_fetch, get_weather
from runtime.gateway import CapabilityGateway, LegacyPolicyAdapter
from runtime.task import new_task
from runtime.context import TaskContext
from runtime.workflow import Stage, Workflow
from persistence import WorkflowStore
from runtime.templates import instantiate, list_templates
from config import BASE_URL, KEY_ID, MODEL, require
from experts import EXPERTS, view_expert_prompts
from latex_tools import write_latex, check_latex, check_tikz, view_theorem_style
from personalization import turn_context
from python_sandbox import run_python
from pictures import contextual_message_text, public_message_text, referenced_picture_ids


def _dialogue_context(messages: list, limit: int = 50) -> str:
    '''提取 messages 中「用户 ↔ Super/专家」的纯文本对话, 过滤 role:tool 与工具调用过程,
    格式化成可读的对话记录 (仅保留最近 limit 条) , 作为专家的上下文注入。'''
    lines = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        # 只保留 user / assistant 的非空纯文本内容 (assistant 的 tool_calls 轮 content 常为 None)
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            who = "用户" if role == "user" else "Super"
            lines.append(f"{who}: {contextual_message_text(content).strip()}")
    return "\n".join(lines[-limit:]) if lines else ""


def build_task_context(task: str, holder: str | None = None, history: str = "") -> TaskContext:
    """Phase 3 接线: 把一次专家调用包装成结构化 Task + TaskContext。

    goal=task, holder=专家名; 有对话历史时作为 previous_results 注入 (PassageWrite 总结对话依赖)。
    brief() 输出即专家可读摘要, 逐步替代纯文本 _dialogue_context 拼接。
    """
    t = new_task(task, holder=holder)
    ctx = TaskContext(task_id=t.id, goal=task, holder=holder)
    if history:
        ctx.previous_results.append(
            "Super 与用户的对话历史 (作为背景参考；若任务要求总结对话, 须据此为准, 且不要逐字复述) :\n"
            + history
        )
    return ctx


# 鉴权说明: 专家工具子集已通过 Agent.run_agent 的 tool_names 施加「执行层白名单」——
# schema 级过滤只让 LLM 看不见, 执行层校验才真正阻止越权提交 (防止专家自我调用/互相甩锅) 。
# Super 调用 run_agent 时 tool_names=None, 保留全量调度权。


def build_super_tools(
    bus: Bus,
    client,
    conversation_history: list,
    gateway: CapabilityGateway | None = None,
    expert_specs: dict | None = None,
    preference_store=None,
    mode_provider=None,
    discussion_summary_provider=None,
) -> Tools:
    '''把每个专家注册为 bus.tools 上的工具函数 (标记为 slow_task) ,
    Super 调用专家时走异步慢任务机制 (挂 pending、占用 IO 锁、等输入时挂起) 。
    专家内部: 独立 Agent 实例 + 独立 messages + 共享 client + 共享 bus.tools。
    conversation_history: Super 的 messages 列表引用, 专家被调用时提取其中的
    「用户 ↔ Super 纯文本对话」作为上下文注入, 打通 PassageWrite 总结对话的数据通路。
    gateway: 传入时额外注册 run_workflow 顺序执行器 (Phase 3+4 接线) 。'''
    super_tools = bus.tools
    specs = EXPERTS if expert_specs is None else expert_specs

    def view_discussion_source(
        message_number: Annotated[int, "摘要所引用的消息编号，例如 m000123 中的 123"],
    ) -> str:
        """按消息编号回查本分支的原始用户或助手发言。"""
        index = int(message_number) - 1
        if index < 0 or index >= len(conversation_history):
            return "该消息编号不在当前分支历史中。"
        message = conversation_history[index]
        role = message.get("role")
        content = message.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
            return "该编号对应内部工具记录或空消息，无法作为讨论来源展示。"
        return f"[m{index + 1:06d}] {'用户' if role == 'user' else '助手'}：\n{public_message_text(content)[:6000]}"

    super_tools.add_tool(view_discussion_source, time_out=5)

    for name, (prompt, tool_names) in specs.items():
        async def expert(
            task: Annotated[str, "交给该专家处理的任务或问题描述"] = "",
            _prompt: str = prompt,
            _name: str = name,
            _names: list = tool_names,   # 专家工具子集 (下划线参数不进 schema, LLM 无法篡改)
        ) -> str:
            # 提取 Super 与用户的纯文本对话历史, 装进结构化 TaskContext (PassageWrite 总结对话依赖它) 。
            # Phase 3 接线: 每次专家调用都对应一个新 Task + TaskContext, 专家上下文取 ctx.brief()。
            history = _dialogue_context(conversation_history)
            summary = discussion_summary_provider() if discussion_summary_provider else None
            if summary and summary.get("summary"):
                history = (
                    "较早讨论摘要（来源编号可由 Super 使用 view_discussion_source 回查）：\n"
                    + summary["summary"] + ("\n\n最近对话：\n" + history if history else "")
                )
            ctx = build_task_context(task, holder=_name, history=history)
            mode = mode_provider() if mode_provider else "direct"
            user_content = ctx.brief() + "\n\n" + turn_context(
                mode, task, preference_store,
                allow_preference_tools=False,
                allow_memory_retrieval="retrieve_context" in _names,
                allow_history_search="search_discussion_history" in _names,
                allow_python="run_python" in _names,
            )
            msgs = [
                {"role": "system", "content": _prompt},
                {"role": "user", "content": user_content},
            ]
            # 每个专家独立 Agent 实例 (独立 pending / 独立对话历史)
            expert_agent = Agent(bus)
            # 输出通过 bus.io_print 互斥；专家 run_agent 内部 LLM 调工具也走 bus.submit
            out = await expert_agent.run_agent(client, msgs, MODEL, tool_names=_names)   # tool_names 同时做 schema 过滤 + 执行层白名单, 杜绝专家越权/递归调用
            return out if out else ""

        expert.__name__ = f"{name}_expert"
        expert.__doc__ = f"调用 {name} 专家处理任务, 传入具体任务描述。"
        # listener 需要等待长音频转写完成，超时放宽到 1800s；其余专家维持 180s。
        expert_timeout = require("runtime.listener_expert_timeout_seconds") if name == "listener" else require("runtime.expert_timeout_seconds")
        super_tools.add_tool(expert, time_out=expert_timeout)
        # 关键: 把专家工具标记为慢任务, Super 调用时走异步慢任务机制
        bus.mark_slow([f"{name}_expert"])
        # 专家内部会再 submit 工具: 标记为可重入 (执行时不占用 semaphore, 避免自我死锁)
        bus.mark_reentrant([f"{name}_expert"])

    # Phase 4 接线: 把 run_workflow 顺序执行器注册成 Super 可调用的工具。
    if gateway is not None:
        def view_workflow_templates() -> str:
            """查看宿主预定义的受限工作流模板。"""
            return list_templates()

        super_tools.add_tool(view_workflow_templates, time_out=5)

        async def run_workflow(
            goal: Annotated[str, "工作流目标（要完成什么）"],
            stage_tasks: Annotated[list | None, "有序阶段列表；每项为 {'expert': str, 'task': str}；可省略并使用 template"] = None,
            template: Annotated[str, "可选：预定义模板名；与 stage_tasks 二选一"] = "",
        ) -> str:
            """按阶段顺序调用多个专家完成一个目标：每阶段派发一个专家并派生阶段 passport（能力只收不扩），
            上一阶段结果作为下一阶段背景传入，最后汇总返回。"""
            if template:
                if stage_tasks:
                    return "工作流规划未通过：template 与 stage_tasks 只能选择其中一种。"
                try:
                    stage_tasks = instantiate(template, goal)
                except ValueError as exc:
                    return f"工作流规划未通过：{exc}"
            if not stage_tasks:
                return "工作流规划未通过：请提供 stage_tasks，或指定预定义 template。"
            if require("runtime.require_acceptance_stage"):
                keywords = tuple(k.lower() for k in require("runtime.acceptance_keywords"))
                accepted_experts = set(require("runtime.acceptance_stage_experts"))
                has_acceptance = any(
                    index > 0
                    and step.get("expert") in accepted_experts
                    and any(word in str(step.get("task", "")).lower() for word in keywords)
                    for index, step in enumerate(stage_tasks)
                    if isinstance(step, dict)
                )
                if not has_acceptance:
                    return (
                        "工作流规划未通过：必须包含独立验收阶段。请补充一个由 "
                        f"{', '.join(sorted(accepted_experts))} 执行、任务明确包含“验收/验证/检查”的阶段，"
                        "并在其前安排待验收的产物阶段。"
                    )
            task = new_task(goal, holder="super")
            # 阶段名编码专家名，便于 runner 反查（Stage 本身不携带 expert 语义）
            stage_experts = {}
            stages = []
            for i, step in enumerate(stage_tasks):
                e = step.get("expert", "")
                if e not in specs:
                    return f"未知或当前模式不可用的专家 {e!r}；可用：{', '.join(specs)}"
                stage_name = f"stage-{i}-{e}"
                stage_experts[stage_name] = e
                stages.append(Stage(
                    name=stage_name,
                    tools=tuple(specs[e][1]),
                    instruction=str(step.get("task", "")).strip(),
                ))
            wf = Workflow(name=f"wf-{task.id}", stages=stages)

            # 父 passport = Super 全量调度权（tool_names=None → 仅 deny，无白名单）
            parent = LegacyPolicyAdapter(gateway).to_passport("super")

            async def _run_stage(stage, passport, task_obj, prev):
                e = stage_experts[stage.name]
                prompt, _names = specs[e]
                stage_goal = stage.instruction or task_obj.goal
                ctx = build_task_context(stage_goal, holder=e)
                mode = mode_provider() if mode_provider else "artifact"
                ctx.constraints.append(turn_context(
                    mode, stage_goal, preference_store,
                    allow_preference_tools=False,
                    allow_memory_retrieval="retrieve_context" in _names,
                    allow_history_search="search_discussion_history" in _names,
                    allow_python="run_python" in _names,
                ))
                if stage_goal != task_obj.goal:
                    ctx.constraints.append(f"工作流总目标：{task_obj.goal}")
                if prev:
                    ctx.previous_results.append("上一阶段结果:\n" + str(prev[0]))
                summary = discussion_summary_provider() if discussion_summary_provider else None
                if summary and summary.get("summary"):
                    ctx.previous_results.append(
                        "较早讨论摘要（来源编号可回查）：\n" + summary["summary"]
                    )
                msgs = [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": ctx.brief()},
                ]
                sub_agent = Agent(bus)
                out = await sub_agent.run_agent(
                    client, msgs, MODEL,
                    gateway=gateway, passport=passport,
                )
                if not out:
                    raise RuntimeError(f"阶段 {stage.name} 未返回结果")
                return out

            store = WorkflowStore(require("runtime.persistence_database"))
            results = await wf.run(gateway, parent, task, runner=_run_stage, store=store)
            lines = [f"工作流 {task.id} 完成，共 {len(stages)} 阶段："]
            for i, r in enumerate(results, 1):
                lines.append(f"[阶段{i}] {r}")
            return "\n".join(lines)

        run_workflow.__doc__ = "按阶段顺序执行多专家工作流；每项 stage_tasks 指定 expert 与 task。"
        super_tools.add_tool(run_workflow, time_out=require("runtime.workflow_timeout_seconds"))
        bus.mark_slow(["run_workflow"])
        # 内部会再 submit 专家工具：标记可重入，否则 run_workflow 持有 semaphore
        # 时子 Agent 调工具会「持锁等锁」死锁（与专家同理）。
        bus.mark_reentrant(["run_workflow"])

    return super_tools


def register_common_tools(tools: Tools, data: dict) -> Tools:
    """把除「专家工具」以外的公共工具注册到 tools（供 super.py 与 terminal.py 共用）。"""
    preferences = data.get("agent_preferences")
    if preferences is not None:
        def remember_preference(
            scope: Annotated[str, "适用范围：general、academic、planning、discussion"],
            preference: Annotated[str, "简短、稳定的交互偏好；不要保存一次性要求或敏感事实"],
        ) -> str:
            """保存一条稳定偏好；执行前会展示内容并请求用户确认。"""
            preference_id = preferences.add(scope, preference)
            return f"偏好已保存（ID: {preference_id}）；执行前已由用户确认。"

        def view_preferences(
            scope: Annotated[str, "可选范围：general、academic、planning、discussion；留空查看全部"] = "",
        ) -> str:
            """查看当前已确认且仍启用的用户偏好。"""
            items = preferences.list_preferences(scope=scope.strip() or None)
            if not items:
                return "没有符合条件的已确认偏好。"
            return "\n".join(
                f"{item['id']} [{item['scope']}] {item['preference']}"
                for item in items
            )

        def update_preference(
            preference_id: Annotated[str, "待修改偏好的完整 ID"],
            preference: Annotated[str, "修正后的稳定偏好"],
            scope: Annotated[str, "可选新范围；留空保留原范围"] = "",
        ) -> str:
            """修改已确认偏好；执行前系统会向用户请求确认。"""
            if preferences.get(preference_id) is None:
                return "未找到该偏好 ID。"
            changed = preferences.update(preference_id, preference, scope.strip() or None)
            return "偏好已更新。" if changed else "偏好状态已改变，未执行。"

        def forget_preference(
            preference_id: Annotated[str, "待停用偏好的完整 ID"],
        ) -> str:
            """停用一条已确认偏好；执行前系统会向用户请求确认。"""
            changed = preferences.deactivate(preference_id)
            return "偏好已停用。" if changed else "未找到仍启用的偏好 ID。"

        def export_preferences() -> str:
            """以 JSON 导出全部已确认且仍启用的用户偏好。"""
            return preferences.export_json()

        tools.add_tool(remember_preference)
        tools.add_tool(view_preferences)
        tools.add_tool(update_preference)
        tools.add_tool(forget_preference)
        tools.add_tool(export_preferences)

    memory = data.get("agent_memory")
    if memory is not None:
        tools.add_tool(memory.add_memory)
        tools.add_tool(memory.retrieve_context)
        tools.add_tool(memory.delete_memory, time_out=5)
        tools.add_tool(memory.replace_memory, time_out=10)
    conversation_store = data.get("conversation_store")
    if conversation_store is not None:
        tools.add_tool(conversation_store.search_discussion_history, time_out=5)
    picture_service = data.get("picture_service")
    if picture_service is not None:
        async def view_picture(
            picture_id: Annotated[str, "图片编号；不记得编号时留空以列出当前对话的图片"] = "",
            question: Annotated[str, "针对图片要查看的具体内容；列出图片时可留空"] = "",
        ) -> str:
            """通过隐藏的一轮视觉请求查看当前对话中的图片；不需要展示中间分析过程。"""
            picture_id = str(picture_id or "").strip().lower()
            question = str(question or "")
            provider = data.get("picture_context_provider")
            if not provider:
                return "当前没有可用的对话图片上下文。"
            conversation_id, messages = provider()
            available = referenced_picture_ids(messages)
            if not picture_id.strip():
                return picture_service.list_pictures(conversation_id, available)
            if picture_id not in available:
                return "该图片编号不属于当前对话。"
            client = data.get("picture_client")
            if client is None:
                return "图片查看服务尚未就绪。"
            try:
                return await picture_service.view_picture(
                    client, MODEL, conversation_id, picture_id, question,
                )
            except Exception as exc:
                return f"图片查看失败（{type(exc).__name__}）。"

        tools.add_tool(view_picture, time_out=180)
    visual = data.get("agent_visal")
    if visual is not None:
        tools.add_tool(visual.recognize_doc, time_out=180)
    tools.add_tool(write_latex, time_out=10)
    tools.add_tool(check_tikz, time_out=90)
    tools.add_tool(check_latex, time_out=90)
    tools.add_tool(str_replace_editor, time_out=10)
    tools.add_tool(view_delayed_results, time_out=5)
    tools.add_tool(run_python, time_out=10)
    tools.add_tool(web_search, time_out=60)
    tools.add_tool(web_fetch, time_out=60)
    tools.add_tool(get_weather, time_out=15)
    tools.add_tool(view_expert_prompts, time_out=5)
    tools.add_tool(view_theorem_style, time_out=5)
    listener = data.get("agent_listener")
    if listener is not None:
        tools.add_tool(listener.transcribe_audio, time_out=1500)
        tools.add_tool(listener.start_listening, time_out=10)
        tools.add_tool(listener.stop_listening, time_out=30)
        tools.add_tool(listener.get_listen_result, time_out=5)
        tools.add_tool(listener.get_listen_cursor, time_out=5)
        tools.add_tool(listener.clear_listen_result, time_out=5)
    plan = data.get("agent_plan")
    if plan is not None:
        tools.add_tool(plan.add_today_plan, time_out=5)
        tools.add_tool(plan.view_plan, time_out=5)
        tools.add_tool(plan.add_general_plan, time_out=5)
        tools.add_tool(plan.view_general_plan, time_out=5)
    task_store = data.get("agent_tasks")
    if task_store is not None:
        def create_task(
            title: Annotated[str, "任务名称"],
            due_at: Annotated[str, "本地时间 ISO 格式，例如 2026-09-26T20:00"],
            timezone: Annotated[str, "IANA 时区，例如 Asia/Shanghai；必须明确传入以便确认"],
            repeat: Annotated[str, "重复方式：none、daily 或 weekly；必须明确传入以便确认"],
        ) -> str:
            """创建一个持久化提醒；写入前会向用户显示任务与时间并请求确认。"""
            task_id = task_store.create(title, due_at, timezone, repeat)
            return f"任务已创建（ID: {task_id}）。提醒仅在本程序运行时触发。"

        def list_tasks() -> str:
            """查看未完成任务及其下次提醒时间。"""
            items = task_store.list_tasks()
            if not items:
                return "当前没有未完成的提醒任务。"
            from datetime import datetime
            from zoneinfo import ZoneInfo
            return "\n".join(
                f"{item['id']} | {item['title']} | "
                f"{datetime.fromisoformat(item['due_at']).astimezone(ZoneInfo(item['timezone'])).isoformat(timespec='minutes')} "
                f"({item['timezone']}, {item['repeat_rule']})"
                for item in items
            )

        def complete_task(task_id: Annotated[str, "任务完整 ID"]) -> str:
            """完成一个提醒任务并停止后续提醒；执行前会向用户请求确认。"""
            return "任务已完成，后续提醒已停止。" if task_store.complete(task_id) else "未找到仍待办的任务。"

        def defer_task(
            task_id: Annotated[str, "任务完整 ID"],
            due_at: Annotated[str, "新的本地时间 ISO 格式，例如 2026-09-27T20:00"],
            timezone: Annotated[str, "新的时间使用的 IANA 时区；必须明确传入以便确认"],
        ) -> str:
            """延期一个提醒任务；执行前会向用户请求确认。"""
            changed = task_store.defer(task_id, due_at, timezone.strip() or None)
            return "提醒时间已更新。" if changed else "未找到仍待办的任务。"

        tools.add_tool(create_task, time_out=5)
        tools.add_tool(list_tasks, time_out=5)
        tools.add_tool(complete_task, time_out=5)
        tools.add_tool(defer_task, time_out=5)
    return tools


def build_client() -> openai.AsyncOpenAI:
    """创建共享的 AsyncOpenAI client（密钥变量名来自 settings.json）。"""
    api_key = os.environ.get(KEY_ID)
    if not api_key:
        raise RuntimeError(f"未设置 API 密钥环境变量 {KEY_ID!r}（见 settings.json 的 llm.api_key_env）")
    return openai.AsyncOpenAI(
        api_key=api_key, base_url=BASE_URL, timeout=require("llm.timeout_seconds")
    )
