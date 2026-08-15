from Agent import agent
from Tools import Tools
import inspect,time,asyncio
from pydantic import Field, create_model
from pydantic.fields import FieldInfo
from typing import Annotated,get_origin,get_args,Any,Pattern,Optional
from annotated_types import Gt,Le,Ge,Lt,MaxLen,MinLen,MultipleOf
from pydantic_core import PydanticUndefined
from functools import wraps
from enum import Enum
from initer import init

Agent_tool = Tools()

@Agent_tool.registry(3)
def getweather(
    city: Annotated[str, Field(default=None,description='城市名称')],
    unit: Optional[str] = "celsius"
) -> str:
    '''获取当前的天气情况'''
    if city == None or city == "":
        raise ValueError("城市不能留空，请向用户询问或查找数据库")
    return f"{city}今日25度({unit})，晴转多云"

@Agent_tool.registry(1)
def get_time() -> str:
    '''获取当前user处时间'''
    return time.strftime("%Y-%m-%d %H:%M:%S")

@Agent_tool.registry(5)
def memory_saver(
    text:str = Field(default='',description='the memory need to save'),
    metadata: dict = Field(default=None,description='对text添加元对象')
):
    if metadata is None:
        metadata = {
            "source" : "user_input"
        }

async def terminal():
    print("-----started-----")
    _dict = await init()
    agent_memory = _dict["agent_memory"]
    Agent_tool.ensure.append(agent_memory)
    agent_visal = _dict['agent_visal']
    Agent_tool.ensure.append(agent_visal)
    Agent_tool.add_tool(agent_memory.add_memory)
    Agent_tool.add_tool(agent_memory.retrieve_context)
    Agent_tool.add_tool(agent_visal.recognize_doc,time_out=120)
    while True:
        _Input = input()
        if _Input == "exit":
            break
        if _Input == "insert":
            # agent(Agent_tool=Agent_tool)
            asyncio.run(agent(Agent_tool=Agent_tool))
        if  _Input== 'schema':
            print(Agent_tool.schema[0:])
        if _Input == 'add':
            tmp = input("请输入记忆内容：")
            agent_memory.add_memory(tmp)
        if _Input == 'req':
            tmp = input("请输入检索内容：")
            result = agent_memory.retrieve_context(tmp)
            print(result)
        if _Input == 'run':
            tmp = input('输入目标')
            res = asyncio.run(Agent_tool.async_execute('retrieve_context',tmp))
            print(res)

asyncio.run(terminal())