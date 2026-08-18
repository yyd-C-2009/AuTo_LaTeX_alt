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

async_client = None   # 惰性：在 agent() 内部、拿到 DSH_OPENAI_KEY 后再创建


def tool_call_add(message:list,content:str,tool_call_id:str):
    message.append({
        'role' : 'tool',
        'content' : content,
        'tool_call_id' : tool_call_id
    })
    return None


def view_delayed_results(
    delayed_results: Annotated[list | None, "你当前 Agent 的延迟结果缓存列表，请勿填写此字段，系统会自动注入"] = None
) -> list:
    '''查看之前提交的慢任务（延迟调用）已经返回的结果。内部参数无需填写，系统会自动注入你当前的缓存；每次读取后缓存会被清空。'''
    if delayed_results is None:
        return []
    return delayed_results


class Agent:
    def __init__(self, bus:Bus):
        self.bus = bus
        self.pending = {}            # tool_call_id -> {task_id, func_name, query}
        self.delayed_results = []   # 慢任务 poll 结果缓存（供 view_delayed_results 读取）

    # TODO: 高级特性——后续再开放，暂注释
    # def immediate_start_ans(self,
    #     Switch_on:Annotated[bool,'设为True则立即开始回答，不等待之后的结果响应'] = False
    #     )  ->  None:
    #     '''选择是否继续等待tools的响应，本函数无返回值'''
    #     if Switch_on:
    #         self.jump_out = True
    #     return None
    #
    # def pending_release(self,
    #     Switch_on:Annotated[bool,'设为True则立即放弃之前对话中所有正在等待的任务'] = False
    #     ) -> None:
    #     '''选择是放弃所有正在执行的tools，本函数无返回值，谨慎使用!'''
    #     if Switch_on:
    #         for _,tid in self.pending.items():
    #             self.bus._released_tasks.update(tid)
    #         self.pending.clear()
    #     return None

    async def run_agent(self,
        async_client, messages: list,
        model: str = 'deepseek-v4-pro', max_step: int = 5,
        tool_names: list[str] | None = None
    ) -> str | None:
        '''单轮次对话循环：驱动 LLM <-> 工具（快任务同步、慢任务异步提交+轮询）
        tool_names: 为 None 使用 bus.tools 全量 schema；给定列表则按名字过滤（schema 级工具子集）'''
        steps = 0

        # 工具 schema（可选子集过滤，一次算好整轮复用）
        schema = self.bus.tools.schema if self.bus is not None else []
        # 执行层鉴权白名单：tool_names 为 None 代表 Super（保留全量调度权），
        # 否则只允许提交白名单内的工具（schema 级过滤只是「让专家看不见」，这里才是「调不动」）
        allow = set(tool_names) if tool_names is not None else None
        if allow is not None:
            # view_delayed_results 始终开放：每个 Agent 都能查看「自己的」延迟结果缓存
            schema = [s for s in schema if s['function']['name'] in allow or s['function']['name'] == 'view_delayed_results']
        while steps < max_step:
            print(f'TASKKKS{steps}')
            steps += 1

            # ① 先 poll 在途慢任务：完成的回填结果并移出 pending
            finished = []
            for tcid, info in list(self.pending.items()):
                result = self.bus.poll(info['task_id'])
                if result.title in ('Done', 'Error'):
                    # 不回填 tool，存入延迟结果缓存，由 view_delayed_results 统一读取
                    self.delayed_results.append({
                        'function': info['func_name'],
                        'query': info['query'],
                        'result': str(result.content),
                    })
                    finished.append(tcid)
            for tcid in finished:
                self.pending.pop(tcid, None)

            # ② 请求 LLM
            try:
                resp = await async_client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=schema,
                    temperature=0,
                    tool_choice="auto"
                )
            except Exception as e:
                print(f"LLM请求异常:{str(e)}")
                continue

            msg = resp.choices[0].message
            messages.append(msg.model_dump())

            # ③ 处理 tool_calls：快任务直接回填，慢任务 submit + 回填占位
            if msg.tool_calls:
                for tool_called in msg.tool_calls:
                    func_name = tool_called.function.name
                    try:
                        kwargs = json.loads(tool_called.function.arguments)
                    except Exception as e:
                        # LLM 返回非法 JSON：回填错误 tool 消息（保持 tool_call_id 配对），让 LLM 下轮自行修正
                        tool_call_add(messages, f"参数 JSON 解析失败({type(e).__name__}): {e}，请修正参数格式后重试", tool_called.id)
                        continue
                    print(f"LLM决定调用{func_name}，参数为{kwargs}")

                    # 特殊拦截：view_delayed_results 读取「自己」的延迟缓存，不经 bus.submit / 鉴权
                    if func_name == 'view_delayed_results':
                        cached = self.delayed_results
                        tool_call_add(messages, str(cached), tool_called.id)
                        self.delayed_results = []   # 读后清空
                        print(f"[延迟结果窗口] 返回 {len(cached)} 条延迟结果")
                        continue

                    # 执行层鉴权：白名单外的工具一律拒绝执行（防止专家自我调用/互相甩锅）
                    if allow is not None and func_name not in allow:
                        tool_call_add(
                            messages,
                            f"拒绝调用 {func_name}：该工具不在你的权限范围内。你的可用工具为：{sorted(allow)}。请在本职责内完成任务，勿尝试调用其他专家或越权工具。",
                            tool_called.id,
                        )
                        print(f"[鉴权拒绝] {func_name} 不在白名单 {sorted(allow)}，已拒绝")
                        continue

                    result = await self.bus.submit(func_name=func_name, **kwargs)

                    if result.title in ('Done', 'Error'):
                        tool_call_add(messages, str(result.content), tool_called.id)
                    elif result.title == 'Submitted':
                        self.pending[tool_called.id] = {
                            'task_id': result.content['id'],
                            'func_name': func_name,
                            'query': kwargs,  # question content sent by LLM
                        }
                        # 慢任务也必须回填占位 tool 消息（assistant->tool 配对），否则 API 报 400
                        tool_call_add(
                            messages,
                            f"任务已提交(task_id={result.content['id']})，处理中；结果就绪后请调用 view_delayed_results 查看",
                            tool_called.id,
                        )

                # 若有在途慢任务，短暂等待后进入下一轮再 poll；否则继续
                if self.pending:
                    await self._wait_pending(messages)
                continue

            # ④ 无工具调用：若还有在途慢任务未完成，等待后再试；否则返回内容
            if self.pending:
                await self._wait_pending(messages)
                continue
            return msg.content

        print("Timeout as agent be stuck in tools calling")   # 超过 max_step 判定死循环
        return None
    async def _wait_pending(self, messages: list, poll_interval: float = 2.0, hard_timeout: float = 240.0):
        '''等待所有在途慢任务完成：只轮询 poll、不消耗 run_agent 的 step 计数，
        避免「等待慢任务」白烧 max_step 导致 Super 提前放弃（Timeout）。
        慢任务完成即把结果（含函数名+提问内容）存入 self.delayed_results 缓存，
        不再往 messages 重复回填 role:tool（避免重复 tool_call_id 导致 400）；
        LLM 通过调用 view_delayed_results 主动读取缓存。超过 hard_timeout 仍完成则跳出兜底。'''
        waited = 0.0
        while self.pending:
            await asyncio.sleep(poll_interval)
            waited += poll_interval
            finished = []
            for tcid, info in list(self.pending.items()):
                result = self.bus.poll(info['task_id'])
                if result.title in ('Done', 'Error'):
                    # 不再往 messages 重复追加 role:tool（会重复使用 tool_call_id 导致 400），
                    # 改为存入 Agent 自己的延迟结果缓存，供 view_delayed_results 读取
                    self.delayed_results.append({
                        'function': info['func_name'],
                        'query': info['query'],
                        'result': str(result.content),
                    })
                    finished.append(tcid)
            for tcid in finished:
                self.pending.pop(tcid, None)
            if waited >= hard_timeout:
                print(f"[慢任务等待硬超时] 仍有 {len(self.pending)} 个任务未完成，先返回让 LLM 处理")
                break
        return None



    async def agent(self, max_step: int = 5,
                    system_prompt: str = (
                        "你是严谨的智能助手。调用工具时，如果缺少必要参数，请在对应位置留空，反问用户补充，"
                        "绝不猜测或虚构。若工具返回错误，你必须根据错误描述调整参数后再试。"
                    ),
                    model: str = "deepseek-v4-flash-ascend",
                    base_url: str = "https://api.llm.ustc.edu.cn/v1/"):
        key = os.environ.get("DSH_OPENAI_KEY")
        if not key:
            raise RuntimeError("缺少环境变量 DSH_OPENAI_KEY")
        async_client = openai.AsyncOpenAI(api_key=key, base_url=base_url, timeout=60.0)
        messages = [{"role": "system", "content": system_prompt}]

        while True:
            # messages = message_cutter(messages)
            requiry = await asyncio.create_task(asyncio.to_thread(input))
            if requiry == "\\exit()":
                return None

            messages.append({"role": "user", "content": requiry})
            out = await self.run_agent(async_client, messages, model, max_step)
            if out:
                print(out)


class Agent_core():
    '''
    真实的工具调用入口（当前形态）：被 Super 通过 bus 路由调用，被动执行一个工具。
    之后会新增独立的 `agent_exec` 作为「常驻挂起入口」（主动 while True 监听总线事件）。
    '''
    def __init__(self, read_only: bool = True):
        self.tool_list = [str]
        self.read_only: bool = read_only
        self.allowed: bool = False

    # TODO: 常驻挂起入口（之后再做）——专家各自 while True 监听 bus，无事件时挂起
    async def agent_exec(self, bus):
        while self.allowed:
            await bus.receive   # 占位：需实现 bus.receive（挂起等待事件的入口）