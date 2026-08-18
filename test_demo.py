_OLD_DEMO = r'''  # 旧版交互式 demo 已废弃：整体保留为死字符串，无副作用，可安全删除
MCP 流程对话测试 demo：
只加载 OCR（Visal），把 recognize_doc 注册为工具，通过 Agent + 真实 LLM 对话，
用户可以让 Agent 读取图片（如 test2.png）并识别其中的文字与 LaTeX 公式。

用法：设置环境变量 DSH_OPENAI_KEY 后运行
    python test_demo.py
对话中输入图片路径让 Agent 识别，输入 \exit() 退出。
"""
import os
import asyncio
import openai

from Tools import Tools
from event_bus import Bus
from Agent import Agent
from Visal import Visal

MODEL = "deepseek-v4-flash-ascend"
BASE_URL = "https://api.llm.ustc.edu.cn/v1/"

SYSTEM_PROMPT = (
    "你是严谨的智能助手。你可以调用 recognize_doc 工具读取图片/PDF并识别其中的文字与数学公式。"
    "当用户提到要识别某张图片时，请调用 recognize_doc 并传入图片路径。"
    "调用工具时若缺少必要参数，留空并反问用户，绝不猜测或虚构。"
)


async def main():
    # 只加载 OCR（重模型，丢线程池），不加载 Saver
    print("正在加载 Visal（OCR 模型）...")
    visal = await asyncio.to_thread(Visal)
    print("Visal 加载完成")

    # 注册工具：recognize_doc（同步函数，走 to_thread，不阻塞事件循环）
    tools = Tools()
    tools.add_tool(visal.recognize_doc, time_out=180)

    # 组装总线 + Agent
    bus = Bus(tools, max_concurrency=4)
    agent = Agent(bus)

    # 真实 LLM 客户端
    key = os.environ.get("DSH_OPENAI_KEY")
    if not key:
        raise RuntimeError("缺少环境变量 DSH_OPENAI_KEY")
    async_client = openai.AsyncOpenAI(api_key=key, base_url=BASE_URL, timeout=120.0)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    print("\n===== MCP 对话测试开始 =====\n"
          "提示：输入图片路径（如 test2.png）让 Agent 识别，输入 \\exit() 退出\n")

    while True:
        requiry = await asyncio.to_thread(input, "你: ")
        if requiry == "\\exit()":
            print("退出。")
            return

        messages.append({"role": "user", "content": requiry})
        out = await agent.run_agent(async_client, messages, MODEL, max_step=8)
        if out:
            print(f"Agent: {out}")


'''
