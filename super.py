import os
import asyncio
import subprocess
import shutil
import openai
from typing import Annotated

from Agent import run_agent, agent
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


def _filter_schema(schema: list, names: list) -> list:
    return [s for s in schema if s["function"]["name"] in names]


def build_super_tools(agent_tool: Tools, client) -> Tools:
    super_tools = Tools()

    for name, (prompt, tool_names) in EXPERTS.items():
        async def expert(
            task: Annotated[str, "交给该专家处理的任务或问题描述"] = "",
            _prompt: str = prompt,
            _names: list = tool_names,
        ) -> str:
            msgs = [
                {"role": "system", "content": _prompt},
                {"role": "user", "content": task},
            ]
            out = await run_agent(
                agent_tool, client, msgs, MODEL,
                tools=_filter_schema(agent_tool.schema, _names),
            )
            return out if out else ""

        expert.__name__ = f"{name}_expert"
        expert.__doc__ = f"调用 {name} 专家处理任务，传入具体任务描述。"
        super_tools.add_tool(expert, time_out=120)

    return super_tools


async def main():
    data = await init()  # 加载 Saver + Visal（重模型，线程池）

    agent_tool = Tools()
    agent_tool.add_tool(data["agent_memory"].add_memory)
    agent_tool.add_tool(data["agent_memory"].retrieve_context)
    agent_tool.add_tool(data["agent_visal"].recognize_doc, time_out=120)
    agent_tool.add_tool(write_latex, time_out=10)
    agent_tool.add_tool(check_tikz, time_out=90)

    client = openai.OpenAI(
        api_key=os.environ["DSH_OPENAI_KEY"], base_url=BASE_URL, timeout=60.0
    )

    super_tools = build_super_tools(agent_tool, client)
    # Super 直接持有记忆工具，用于复盘与上下文传递（不直接 OCR，交给 math_expert）
    super_tools.add_tool(data["agent_memory"].add_memory)
    super_tools.add_tool(data["agent_memory"].retrieve_context)

    await agent(super_tools, system_prompt=SUPER_PROMPT, model=MODEL, base_url=BASE_URL)


if __name__ == "__main__":
    asyncio.run(main())
