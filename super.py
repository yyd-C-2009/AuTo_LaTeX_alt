import os
import asyncio
import subprocess
import shutil
import openai
from typing import Annotated

from Agent import Agent
from event_bus import Bus
from Tools import Tools
from initer import init

MODEL = "deepseek-v4-flash-ascend"
BASE_URL = "https://api.llm.ustc.edu.cn/v1/"
OUT_DIR = "latex_output"
TIKZ_DIR = "tikz_output"

SUPER_PROMPT = (
    "你是Super，一个多Agent系统的调度者。你接收用户请求，理解其意图，"
    "选择并调用合适的专家Agent（通过工具），汇总结果。每次对话结束时复盘："
    "判断哪些功能值得在之后被集成为新工具，并调用 add_memory 保存这份复盘。"
    "可用专家：math_expert(数学判断/讨论)、mathwrite_expert(LaTeX转写)、"
    "passagewrite_expert(篇章结构与总结)、draw_expert(Tikz绘图)。"
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
        "完成后调用 write_latex 将代码写入文件。",
        ["write_latex"],
    ),
    "passagewrite": (
        "你是PassageWrite专家：设计篇章结构，管理MathWrite的编写位置，"
        "将Super/Math与用户的对话总结（非摘录）成重点突出的LaTeX文档，"
        "完成后调用 write_latex 写入文件。",
        ["write_latex", "retrieve_context"],
    ),
    "draw": (
        "你是Draw专家：负责Tikz绘图。你只能输出 LaTeX 字符串作为最终回答"
        "（仅 tikzpicture 内容，不含 documentclass/usepackage）。"
        "输出前必须调用 check_tikz 验证其能成功编译；若返回编译失败，"
        "根据日志修改后重新验证，直到通过。",
        ["check_tikz"],
    ),
}


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
            cwd=TIKZ_DIR, capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return "编译超时(>60s)"
    if proc.returncode == 0:
        return "编译成功 (PDF: " + os.path.join(TIKZ_DIR, name + ".pdf") + ")"
    log = proc.stdout or ""
    return "编译失败：\n" + "\n".join(log.splitlines()[-15:])


# TODO: 旧版「不同 Agent 不同工具子集」机制，现改为所有 Agent 共用 bus.tools，
# 「不同 Agent 不同工具」由之后的鉴权系统实现。先保留注释，待鉴权系统落地后删除。
# def _filter_schema(schema: list, names: list) -> list:
#     return [s for s in schema if s["function"]["name"] in names]


def build_super_tools(bus: Bus, client) -> Tools:
    '''把每个专家注册为 bus.tools 上的工具函数（标记为 slow_task），
    Super 调用专家时走异步慢任务机制（挂 pending、占用 IO 锁、等输入时挂起）。
    专家内部：独立 Agent 实例 + 独立 messages + 共享 client + 共享 bus.tools。'''
    super_tools = bus.tools

    for name, (prompt, tool_names) in EXPERTS.items():
        async def expert(
            task: Annotated[str, "交给该专家处理的任务或问题描述"] = "",
            _prompt: str = prompt,
            _name: str = name,
            _names: list = tool_names,   # TODO: 鉴权系统落地后，专家工具子集由此给出
        ) -> str:
            msgs = [
                {"role": "system", "content": _prompt},
                {"role": "user", "content": task},
            ]
            # 每个专家独立 Agent 实例（独立 pending / 独立对话历史）
            expert_agent = Agent(bus)
            # 输出通过 bus.io_print 互斥；专家 run_agent 内部 LLM 调工具也走 bus.submit
            out = await expert_agent.run_agent(client, msgs, MODEL)
            if out:
                await bus.io_print(f"[{_name}] {out}")
            return out if out else ""

        expert.__name__ = f"{name}_expert"
        expert.__doc__ = f"调用 {name} 专家处理任务，传入具体任务描述。"
        super_tools.add_tool(expert, time_out=180)
        # 关键：把专家工具标记为慢任务，Super 调用时走异步慢任务机制
        bus.mark_slow([f"{name}_expert"])

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

    # 共享 client，注意是AsyncOpenAI
    client = openai.AsyncOpenAI(
        api_key=os.environ["DSH_OPENAI_KEY"], base_url=BASE_URL, timeout=60.0
    )

    # 总线 + 专家工具（专家标记为 slow_task）
    bus = Bus(tools, max_concurrency=4)

    # 专家工具注册进 bus.tools（标记为 slow_task，Super 调用时异步化）
    build_super_tools(bus, client)
    # 记忆工具已在上方注册（见 tools.add_tool(add_memory/retrieve_context)），Super 复盘直接使用；
    # 切勿重复 add_tool：同名工具会重复出现在 schema 中（历史 bug，已修复）

    # Super 自己也是一个 Agent（只负责路由，不负责具体读写）
    super_agent = Agent(bus)
    messages = [{"role": "system", "content": SUPER_PROMPT}]

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
