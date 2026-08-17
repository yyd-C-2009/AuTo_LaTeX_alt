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
from event_bus import Bus,Message
import asyncio

client = None

async_client = None


def tool_call_add(message:list,content:str,tool_call_id:str):
    message.append({
        'role' : 'tool',
        'content' : content,
        'tool_call_id' : tool_call_id
    })
    return None

class Agent:
    def __init__(self,bus:Bus,model_list:list):
        self.jump_out = False
        self.bus = bus
        self.pending =  {}

    def immediate_start_ans(self,
        Switch_on:Annotated[bool,'设为True则立即开始回答，不等待之后的结果响应'] = False
        )  ->  None:
        '''选择是否继续等待tools的响应，本函数无返回值'''
        if Switch_on:
            self.jump_out = True
        return None
    
    def pending_release(self,
        Switch_on:Annotated[bool,'设为True则立即放弃之前对话中所有正在等待的任务'] = False
        ) -> None:              #暂未确定是否应当把这个功能开放给Agent
        '''选择是放弃所有正在执行的tools，本函数无返回值，谨慎使用!'''
        if Switch_on:
            for _,tid in self.pending.items():
                self.bus._released_taks.update(tid)
            self.pending.clear()
        return None
    
    async def run_agent(self,
        async_client, messages: list,
        model: str, max_step: int = 5
    ) -> str | None:
        '''单轮次对话系统，没有多轮功能'''
        steps = 0
        tool_worked =  True                         # 首轮默认不等待
        while steps < max_step:
            steps += 1
            try:
                resp = await async_client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=[] if self.jump_out else self.bus.tools if self.bus is not None else [],      #如果选择了跳过，那么系统直接不在基于tools强制回答
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
                    kwargs = json.loads(tool_called.function.arguments)

                    print(f"LLM决定调用{func_name}，参数为{kwargs}")

                    result = await self.bus.submit(func_name=func_name,**kwargs)

                    if result.title in ['Done','Error']:
                        tool_call_add(message=messages,content=str(result.content),tool_call_id=tool_called.id)
                    elif result.title == 'Submitted':
                        self.pending[tool_called.id] = result.content['id']
            else:
                return msg.content              #没有工具调用则返回内容
            finished = []
            if tool_worked is False:
                await asyncio.sleep(10)
            tool_worked = False
            for tcid,tid in self.pending.items():
                result = self.bus.poll(tid)
                if result.title in ['Done','Error']:
                    finished.append(tcid)
                    tool_call_add(message=messages,content=str(result.content),tool_call_id=tcid)
                    tool_worked = True
                elif self.jump_out:
                    tool_call_add(message=messages,content='已跳过本工具的响应',tool_call_id=tcid)
            for _end in finished:
                self.pending.pop(_end)

        print("Timeout as agent be stuck in tools calling")            # 工具嵌套层数过多，判定为Agent在死循环
        return None


    async def agent(self,Agent_tool: Tools, max_step: int = 5,
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
            out = await self.srun_agent(Agent_tool, async_client, messages, model, max_step)
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