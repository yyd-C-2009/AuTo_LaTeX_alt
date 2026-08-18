import os
import asyncio
import subprocess
import shutil
import openai
from typing import Annotated

from Agent import Agent, view_delayed_results
from event_bus import Bus
from Tools import Tools
from initer import init
from str_replace_editor import str_replace_editor

MODEL = "deepseek-v4-flash"
BASE_URL = "https://api.deepseek.com"
OUT_DIR = "latex_output"
TIKZ_DIR = "tikz_output"

SUPER_PROMPT = (
    "你是Super，一个多Agent系统的调度者。你接收用户请求，理解其意图，"
    "选择并调用合适的专家Agent（通过工具），汇总结果。每次对话结束时复盘："
    "判断哪些功能值得在之后被集成为新工具，并调用 add_memory 保存这份复盘。"
    "同时，诊断当前对于各个专家的提示词是否合理，是否需要调整。"
    "可用专家：math_expert(数学判断/讨论)、mathwrite_expert(LaTeX转写)、"
    "passagewrite_expert(篇章结构与总结)、draw_expert(Tikz绘图)。"
    "注意：各专家每次被调用都是无状态的，不会记住你之前的对话或它们之前的回答；"
    "因此调用专家时，必须一次性把完成任务所需的全部上下文写进 task 参数。"
    "某个专家任务提交后系统会耐心等待其完成，请勿在上一轮尚无结果时重复提交等价任务。"
)

# name -> (系统提示词, 允许使用的工具名列表)
EXPERTS = {
    "math": (
        "你是Math专家：对OCR结果做逻辑判断，只能读取文件、不能写入代码。"
        "讲解时保持亲和力，不断追问确保用户理解正确，可设置谬误引导用户思考本质。",
        ["recognize_doc", "retrieve_context"],
    ),
    "mathwrite": (
        "你是MathWrite专家：将他人的输出转写为正确的LaTeX代码。"
        "书写定理、引理、定义、证明时必须按规范填写，并确保LaTeX代码正确。"
        "新建代码用 write_latex 写入文件；修改已有 .tex 文件请用 str_replace_editor 精修。",
        ["write_latex", "str_replace_editor"],
    ),
    "passagewrite": (
        "你是PassageWrite专家：设计篇章结构，管理MathWrite的编写位置，"
        "将Super/Math与用户的对话总结（非摘录）成重点突出的LaTeX文档，"
        "新建用 write_latex 写入；调整篇章结构/位置可用 str_replace_editor 精修已有文件。",
        ["write_latex", "str_replace_editor", "retrieve_context"],
    ),
    "draw": (
        "你是Draw专家：负责Tikz绘图。你只能输出 LaTeX 字符串作为最终回答"
        "（仅 tikzpicture 内容，不含 documentclass/usepackage）。"
        "输出前必须调用 check_tikz 验证其能成功编译；若返回编译失败，"
        "根据日志修改后重新验证，直到通过。",
        ["check_tikz"],
    ),
}


def _dialogue_context(messages: list, limit: int = 50) -> str:
    '''提取 messages 中「用户 ↔ Super/专家」的纯文本对话，过滤 role:tool 与工具调用过程，
    格式化成可读的对话记录（仅保留最近 limit 条），作为专家的上下文注入。'''
    lines = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        # 只保留 user / assistant 的非空纯文本内容（assistant 的 tool_calls 轮 content 常为 None）
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            who = "用户" if role == "user" else "Super"
            lines.append(f"{who}: {content.strip()}")
    return "\n".join(lines[-limit:]) if lines else ""


def write_latex(content: Annotated[str, "要写入的LaTeX内容"], filename: Annotated[str, "文件名(可省略)"] = "output.tex") -> str:
    '''将LaTeX内容写入 latex_output/ 目录下的 .tex 文件，返回保存路径'''
    name = os.path.basename(filename) or "output.tex"
    if not name.endswith(".tex"):
        name += ".tex"
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def check_tikz(code: Annotated[str, "待验证的 TikZ 绘图代码（仅 tikzpicture 内容）"], filename: Annotated[str, "文件名(可省略)"] = "figure") -> str:
    '''编译 TikZ 代码验证其能否成功：成功返回 PDF 路径，失败返回编译日志'''
    exe = shutil.which("pdflatex")
    if not exe:
        return "错误：找不到 pdflatex，请确认 LaTeX 已安装且在 PATH 中"
    os.makedirs(TIKZ_DIR, exist_ok=True)
    name = os.path.splitext(os.path.basename(filename) or "figure")[0]
    tex = (
        "\\documentclass[border=2pt]{standalone}\n"
        "\\usepackage{tikz}\n"
        "\\usepackage{amsmath,amssymb}\n"
        "\\begin{document}\n"
        f"{code}\n"
        "\\end{document}\n"
    )
    with open(os.path.join(TIKZ_DIR, name + ".tex"), "w", encoding="utf-8") as f:
        f.write(tex)
    try:
        proc = subprocess.run(
            [exe, "-interaction=nonstopmode", "-halt-on-error", name + ".tex"],
            cwd=TIKZ_DIR, capture_output=True, text=True, timeout=60, encoding="utf-8"
        )
    except subprocess.TimeoutExpired:
        return "编译超时(>60s)"
    if proc.returncode == 0:
        return "编译成功 (PDF: " + os.path.join(TIKZ_DIR, name + ".pdf") + ")"
    log = proc.stdout or ""
    return "编译失败：\n" + "\n".join(log.splitlines()[-15:])


# 鉴权说明：专家工具子集已通过 Agent.run_agent 的 tool_names 施加「执行层白名单」——
# schema 级过滤只让 LLM 看不见，执行层校验才真正阻止越权提交（防止专家自我调用/互相甩锅）。
# Super 调用 run_agent 时 tool_names=None，保留全量调度权。


def build_super_tools(bus: Bus, client, conversation_history: list) -> Tools:
    '''把每个专家注册为 bus.tools 上的工具函数（标记为 slow_task），
    Super 调用专家时走异步慢任务机制（挂 pending、占用 IO 锁、等输入时挂起）。
    专家内部：独立 Agent 实例 + 独立 messages + 共享 client + 共享 bus.tools。
    conversation_history：Super 的 messages 列表引用，专家被调用时提取其中的
    「用户 ↔ Super 纯文本对话」作为上下文注入，打通 PassageWrite 总结对话的数据通路。'''
    super_tools = bus.tools

    for name, (prompt, tool_names) in EXPERTS.items():
        async def expert(
            task: Annotated[str, "交给该专家处理的任务或问题描述"] = "",
            _prompt: str = prompt,
            _name: str = name,
            _names: list = tool_names,   # 专家工具子集（下划线参数不进 schema，LLM 无法篡改）
        ) -> str:
            # 提取 Super 与用户的纯文本对话历史，注入为上下文（PassageWrite 总结对话依赖它）
            history = _dialogue_context(conversation_history)
            user_content = task
            if history:
                user_content = (
                    "以下是 Super 与用户的对话历史（作为背景参考；若任务要求总结对话，须据此为准，且不要逐字复述）：\n"
                    f"{history}\n\n"
                    f"【当前任务】{task}"
                )
            msgs = [
                {"role": "system", "content": _prompt},
                {"role": "user", "content": user_content},
            ]
            # 每个专家独立 Agent 实例（独立 pending / 独立对话历史）
            expert_agent = Agent(bus)
            # 输出通过 bus.io_print 互斥；专家 run_agent 内部 LLM 调工具也走 bus.submit
            out = await expert_agent.run_agent(client, msgs, MODEL, tool_names=_names)   # tool_names 同时做 schema 过滤 + 执行层白名单，杜绝专家越权/递归调用
            if out:
                await bus.io_print(f"[{_name}] {out}")
            return out if out else ""

        expert.__name__ = f"{name}_expert"
        expert.__doc__ = f"调用 {name} 专家处理任务，传入具体任务描述。"
        super_tools.add_tool(expert, time_out=180)
        # 关键：把专家工具标记为慢任务，Super 调用时走异步慢任务机制
        bus.mark_slow([f"{name}_expert"])
        # 专家内部会再 submit 工具：标记为可重入（执行时不占用 semaphore，避免自我死锁）
        bus.mark_reentrant([f"{name}_expert"])

    return super_tools


async def main():
    data = await init()  # 加载 Saver + Visal（重模型，线程池）

    # 所有 Agent 共用的 Tools（挂在 bus 上）
    tools = Tools()
    tools.add_tool(data["agent_memory"].add_memory)
    tools.add_tool(data["agent_memory"].retrieve_context)
    tools.add_tool(data["agent_visal"].recognize_doc, time_out=180)
    tools.add_tool(write_latex, time_out=10)
    tools.add_tool(check_tikz, time_out=90)
    tools.add_tool(str_replace_editor, time_out=10)
    tools.add_tool(view_delayed_results, time_out=5)

    # 共享 client，注意是AsyncOpenAI
    client = openai.AsyncOpenAI(
        api_key=os.environ["DS_API_KEY"], base_url=BASE_URL, timeout=60.0
    )

    # 总线 + 专家工具（专家标记为 slow_task）
    bus = Bus(tools, max_concurrency=4)

    # Super 自己也是一个 Agent（只负责路由，不负责具体读写）
    super_agent = Agent(bus)
    messages = [{"role": "system", "content": SUPER_PROMPT}]

    # 专家工具注册进 bus.tools（标记为 slow_task，Super 调用时异步化）；
    # 传入 messages 引用，让专家被调用时能读到「用户 ↔ Super」对话历史（PassageWrite 总结对话的数据通路）
    build_super_tools(bus, client, messages)
    # 记忆工具已在上方注册（见 tools.add_tool(add_memory/retrieve_context)），Super 复盘直接使用；
    # 切勿重复 add_tool：同名工具会重复出现在 schema 中（历史 bug，已修复）

    await bus.io_print("===== Super 多Agent系统启动（输入 \\exit() 退出）=====")
    while True:
        # 带 input 的对话回合：输出提示 + 等输入，同刻只有一个对话回合
        requiry = await bus.io_dialog("你: ")
        if requiry == "\\exit()":
            await bus.io_print("退出。")
            return

        messages.append({"role": "user", "content": requiry})
        out = await super_agent.run_agent(client, messages, MODEL)
        if out:
            await bus.io_print(f"[Super] {out}")


if __name__ == "__main__":
    asyncio.run(main())
