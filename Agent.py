import openai
import json,inspect,tiktoken,time,os
from pydantic import BaseModel, Field, create_model
from pydantic.fields import FieldInfo
from typing import Optional,Annotated,get_type_hints,get_origin,get_args,Any,Pattern
from annotated_types import Gt,Le,Ge,Lt,MaxLen,MinLen,MultipleOf
from pydantic_core import PydanticUndefined
from functools import wraps
from enum import Enum
from Tools import Tools
from Saver import Saver
import asyncio

client = None

async_client = None

# encoding = tiktoken.get_encoding("cl100k_base")

# def token_counter(List: list):
#     full_text = "".join([m.get("content","") for m in List])
#     return len(encoding.encode(full_text))

# def message_cutter(messages: list,max_tokens: int = 2000) -> None:
#     sys_messages = [x for x in messages if x["role"] == "system"]
#     other_messages = [x for x in messages if x["role"] != "system"]

#     while other_messages and token_counter(sys_messages + other_messages) > max_tokens:
#         print(f"cut out{other_messages.pop(0)["content"]}")

#     return sys_messages + other_messages

# async def call_tool_with_timeout(func_name:str,Agent_tool:Tools,time_out:int = 5,**kwargs):
#     try:
#         return await asyncio.wait_for(
#             asyncio.to_thread(Agent_tool.execute(func_name,**kwargs)),
#             timeout=time_out
#         )
#     except asyncio.TimeoutError as e:
#         raise TimeoutError(f"工具 {func_name} 执行超时 (>{time_out}s)")
#     except Exception as e:
#         raise RuntimeError(f"工具 {func_name} 调用异常：{str(e)}")

async def run_agent(Agent_tool: Tools, async_client, messages: list,
                    model: str, max_step: int = 5, tools: list = None) -> str | None:
    steps = 0
    while steps < max_step:
        steps += 1
        try:
            resp = await async_client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools if tools is not None else Agent_tool.schema,
                temperature=0,
                tool_choice="auto"
            )
        except Exception as e:
            print(f"LLM请求异常:{str(e)}")
            continue

        msg = resp.choices[0].message
        messages.append(msg.model_dump())

        if msg.tool_calls:
            for tool_called in msg.tool_calls:
                func_name = tool_called.function.name
                args = json.loads(tool_called.function.arguments)

                print(f"LLM决定调用{func_name}，参数为{args}")

                try:
                    result = await Agent_tool.async_execute(func_name=func_name, **args)  # 调用外部函数
                    if not isinstance(result, str):
                        result = str(result)
                except Exception as e:
                    result = f"Wrong with the tool:{str(e)},please correct it or ask user for help!"

                messages.append({
                    "role": "tool",
                    "content": result,
                    "tool_call_id": tool_called.id
                })
            continue                # 继续工具嵌套循环

        return msg.content

    print("Timeout as agent be stuck in tools calling")            # 工具嵌套层数过多，判定为Agent在死循环
    return None


async def agent(Agent_tool: Tools, max_step: int = 5,
                system_prompt: str = (
                    "你是严谨的智能助手。调用工具时，如果缺少必要参数，请在对应位置留空，反问用户补充，"
                    "绝不猜测或虚构。若工具返回错误，你必须根据错误描述调整参数后再试。"
                ),
                model: str = "deepseek-v4-flash-ascend",
                base_url: str = "https://api.llm.ustc.edu.cn/v1/"):
    key = os.environ.get("DSH_OPENAI_KEY")
    if not key:
        raise RuntimeError("缺少环境变量 DSH_OPENAI_KEY")
    async_client = openai.OpenAI(api_key=key, base_url=base_url, timeout=60.0)
    messages = [{"role": "system", "content": system_prompt}]

    while True:
        # messages = message_cutter(messages)
        requiry = await asyncio.create_task(asyncio.to_thread(input))
        if requiry == "\\exit()":
            return None

        messages.append({"role": "user", "content": requiry})
        out = await run_agent(Agent_tool, async_client, messages, model, max_step)
        if out:
            print(out)


class Agent_core():
    def __init__(self,read_only:bool = True):
        self.tool_list = [str]
        self.read_onlt:bool = read_only
        self.allowed:bool = False

    async def agent_exec(self,bus):
        while self.allowed:
            await bus.recieve