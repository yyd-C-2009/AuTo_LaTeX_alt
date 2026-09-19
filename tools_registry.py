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
from config import BASE_URL, KEY_ID, MODEL
from experts import EXPERTS, view_expert_prompts
from latex_tools import write_latex, check_latex, check_tikz, view_theorem_style


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
            lines.append(f"{who}: {content.strip()}")
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


def build_super_tools(bus: Bus, client, conversation_history: list, gateway: CapabilityGateway | None = None) -> Tools:
    '''把每个专家注册为 bus.tools 上的工具函数 (标记为 slow_task) ,
    Super 调用专家时走异步慢任务机制 (挂 pending、占用 IO 锁、等输入时挂起) 。
    专家内部: 独立 Agent 实例 + 独立 messages + 共享 client + 共享 bus.tools。
    conversation_history: Super 的 messages 列表引用, 专家被调用时提取其中的
    「用户 ↔ Super 纯文本对话」作为上下文注入, 打通 PassageWrite 总结对话的数据通路。
    gateway: 传入时额外注册 run_workflow 顺序执行器 (Phase 3+4 接线) 。'''
    super_tools = bus.tools

    for name, (prompt, tool_names) in EXPERTS.items():
        async def expert(
            task: Annotated[str, "交给该专家处理的任务或问题描述"] = "",
            _prompt: str = prompt,
            _name: str = name,
            _names: list = tool_names,   # 专家工具子集 (下划线参数不进 schema, LLM 无法篡改)
        ) -> str:
            # 提取 Super 与用户的纯文本对话历史, 装进结构化 TaskContext (PassageWrite 总结对话依赖它) 。
            # Phase 3 接线: 每次专家调用都对应一个新 Task + TaskContext, 专家上下文取 ctx.brief()。
            history = _dialogue_context(conversation_history)
            ctx = build_task_context(task, holder=_name, history=history)
            user_content = ctx.brief()
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
        expert_timeout = 1800 if name == "listener" else 180
        super_tools.add_tool(expert, time_out=expert_timeout)
        # 关键: 把专家工具标记为慢任务, Super 调用时走异步慢任务机制
        bus.mark_slow([f"{name}_expert"])
        # 专家内部会再 submit 工具: 标记为可重入 (执行时不占用 semaphore, 避免自我死锁)
        bus.mark_reentrant([f"{name}_expert"])

    # Phase 4 接线: 把 run_workflow 顺序执行器注册成 Super 可调用的工具。
    if gateway is not None:
        async def run_workflow(
            goal: Annotated[str, "工作流目标（要完成什么）"],
            stage_tasks: Annotated[list, "有序阶段列表；每项为 {'expert': str, 'task': str}"],
        ) -> str:
            """按阶段顺序调用多个专家完成一个目标：每阶段派发一个专家并派生阶段 passport（能力只收不扩），
            上一阶段结果作为下一阶段背景传入，最后汇总返回。"""
            task = new_task(goal, holder="super")
            # 阶段名编码专家名，便于 runner 反查（Stage 本身不携带 expert 语义）
            stage_experts = {}
            stages = []
            for i, step in enumerate(stage_tasks):
                e = step.get("expert", "")
                if e not in EXPERTS:
                    return f"未知专家 {e!r}；可用：{', '.join(EXPERTS)}"
                stage_name = f"stage-{i}-{e}"
                stage_experts[stage_name] = e
                stages.append(Stage(name=stage_name, tools=tuple(EXPERTS[e][1])))
            wf = Workflow(name=f"wf-{task.id}", stages=stages)

            # 父 passport = Super 全量调度权（tool_names=None → 仅 deny，无白名单）
            parent = LegacyPolicyAdapter(gateway).to_passport("super")

            async def _run_stage(stage, passport, task_obj, prev):
                e = stage_experts[stage.name]
                prompt, _names = EXPERTS[e]
                ctx = build_task_context(task_obj.goal, holder=e)
                if prev:
                    ctx.previous_results.append("上一阶段结果:\n" + str(prev[0]))
                msgs = [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": ctx.brief()},
                ]
                sub_agent = Agent(bus)
                out = await sub_agent.run_agent(
                    client, msgs, MODEL,
                    gateway=gateway, passport=passport,
                )
                return out or ""

            results = await wf.run(gateway, parent, task, runner=_run_stage)
            lines = [f"工作流 {task.id} 完成，共 {len(stages)} 阶段："]
            for i, r in enumerate(results, 1):
                lines.append(f"[阶段{i}] {r}")
            return "\n".join(lines)

        run_workflow.__doc__ = "按阶段顺序执行多专家工作流；每项 stage_tasks 指定 expert 与 task。"
        super_tools.add_tool(run_workflow, time_out=600)
        bus.mark_slow(["run_workflow"])
        # 内部会再 submit 专家工具：标记可重入，否则 run_workflow 持有 semaphore
        # 时子 Agent 调工具会「持锁等锁」死锁（与专家同理）。
        bus.mark_reentrant(["run_workflow"])

    return super_tools


def register_common_tools(tools: Tools, data: dict) -> Tools:
    """把除「专家工具」以外的公共工具注册到 tools（供 super.py 与 terminal.py 共用）。"""
    tools.add_tool(data["agent_memory"].add_memory)
    tools.add_tool(data["agent_memory"].retrieve_context)
    tools.add_tool(data["agent_memory"].delete_memory, time_out=5)
    tools.add_tool(data["agent_memory"].replace_memory, time_out=10)
    tools.add_tool(data["agent_visal"].recognize_doc, time_out=180)
    tools.add_tool(write_latex, time_out=10)
    tools.add_tool(check_tikz, time_out=90)
    tools.add_tool(check_latex, time_out=90)
    tools.add_tool(str_replace_editor, time_out=10)
    tools.add_tool(view_delayed_results, time_out=5)
    tools.add_tool(web_search, time_out=60)
    tools.add_tool(web_fetch, time_out=60)
    tools.add_tool(get_weather, time_out=15)
    tools.add_tool(view_expert_prompts, time_out=5)
    tools.add_tool(view_theorem_style, time_out=5)
    listener = data["agent_listener"]
    tools.add_tool(listener.transcribe_audio, time_out=1500)
    tools.add_tool(listener.start_listening, time_out=10)
    tools.add_tool(listener.stop_listening, time_out=30)
    tools.add_tool(listener.get_listen_result, time_out=5)
    tools.add_tool(listener.get_listen_cursor, time_out=5)
    tools.add_tool(listener.clear_listen_result, time_out=5)
    plan = data["agent_plan"]
    tools.add_tool(plan.add_today_plan, time_out=5)
    tools.add_tool(plan.view_plan, time_out=5)
    tools.add_tool(plan.add_general_plan, time_out=5)
    tools.add_tool(plan.view_general_plan, time_out=5)
    return tools


def build_client() -> openai.AsyncOpenAI:
    """创建共享的 AsyncOpenAI client（读取环境变量 DSH_OPENAI_KEY）。"""
    return openai.AsyncOpenAI(
        api_key=os.environ[KEY_ID], base_url=BASE_URL, timeout=60.0
    )
