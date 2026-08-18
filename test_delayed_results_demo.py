"""延迟结果窗口（view_delayed_results）验证：
1. 慢任务完成后结果进入 Agent 自己的 delayed_results（含 function/query/result），
   不再往 messages 重复回填 role:tool（避免重复 tool_call_id 的 400 错）。
2. LLM 调用 view_delayed_results 时被拦截，返回当前缓存并清空（不经 bus.submit/鉴权）。

不依赖真实 LLM，用 stub 注入 openai/pydantic；Linux 也能跑验证纯逻辑。
"""
import sys, types, json, asyncio, time

def stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m

stub('openai', AsyncOpenAI=object)
stub('tiktoken')
stub('pydantic', BaseModel=object, Field=object, create_model=object)
stub('pydantic.fields', FieldInfo=object)
stub('annotated_types', Gt=object, Le=object, Ge=object, Lt=object, MaxLen=object, MinLen=object, MultipleOf=object)
stub('pydantic_core', PydanticUndefined=object)
stub('Saver', Saver=object)
import pydantic
pydantic.fields = sys.modules['pydantic.fields']

from event_bus import Bus, Message
from Agent import Agent, tool_call_add, view_delayed_results


# ---------- fake LLM ----------
class FakeTC:
    def __init__(self, id, name, args):
        self.id = id
        self.function = types.SimpleNamespace(name=name, arguments=json.dumps(args))

class FakeMsg:
    def __init__(self, tool_calls=None, content=None):
        self.tool_calls = tool_calls or []
        self.content = content
    def model_dump(self):
        return {"role": "assistant", "content": self.content}

class FakeResp:
    def __init__(self, msg):
        self.choices = [types.SimpleNamespace(message=msg)]

class FakeCompletions:
    def __init__(self, script):
        self.script = script
        self.idx = 0
    async def create(self, **kw):
        i = min(self.idx, len(self.script) - 1)
        self.idx += 1
        return FakeResp(self.script[i])

class FakeClient:
    def __init__(self, script):
        self.chat = types.SimpleNamespace(completions=FakeCompletions(script))


class FakeTools:
    def __init__(self):
        # schema 含 slow_tool + view_delayed_results
        self.schema = [
            {"function": {"name": "slow_tool"}},
            {"function": {"name": "view_delayed_results"}},
        ]
        self.tool_list = {}
        self.timeout = {}
    async def async_execute(self, func_name, **kw):
        return f"executed:{func_name}"


def slow_tool(x):
    time.sleep(0.3)
    return f"slow:{x}"


async def main():
    # 构造 bus：慢任务 slow_tool
    tools = FakeTools()
    bus = Bus(tools, max_concurrency=4)
    bus.mark_slow(["slow_tool"])
    # 让 slow_tool 能真正被执行（替换 tools.async_execute 为真实调用 slow_tool）
    from Tools import Tools as _RealTools_unused  # noqa

    class RunTools:
        def __init__(self):
            self.schema = tools.schema
        async def async_execute(self, func_name, **kw):
            if func_name == 'slow_tool':
                return slow_tool(**kw)
            return f"executed:{func_name}"
    bus.tools = RunTools()

    agent = Agent(bus)
    # 缩短 _wait_pending 的 poll 间隔，加速测试
    agent._wait_pending = _fast_wait_pending.__get__(agent, Agent)

    # LLM 脚本：
    # 第 1 轮：调用 slow_tool（慢任务）
    # 第 2 轮：调用 view_delayed_results（读取缓存）
    # 第 3 轮：纯文本收尾
    script = [
        FakeMsg(tool_calls=[FakeTC("call_1", "slow_tool", {"x": "hello"})]),
        FakeMsg(tool_calls=[FakeTC("call_2", "view_delayed_results", {})]),
        FakeMsg(content="最终答案：收到结果"),
    ]
    client = FakeClient(script)
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]

    out = await agent.run_agent(client, messages, model="m", max_step=8)

    print("最终输出:", out)
    print("=== 检查 messages 中的 tool 消息 ===")
    tool_msgs = [m for m in messages if isinstance(m, dict) and m.get('role') == 'tool']
    for tm in tool_msgs:
        print("  tool:", str(tm.get('content'))[:80])
    # 关键断言：slow_tool 的 tool_call_id 只回填一次占位，不出现第二条约 call_1 的 tool
    call1_tool = [m for m in tool_msgs if m.get('tool_call_id') == 'call_1']
    print("call_1 的 tool 消息条数:", len(call1_tool), "(应为 1，占位)")
    assert len(call1_tool) == 1, "call_1 不应被重复回填（否则 400）"

    # view_delayed_results 拦截后应返回缓存内容（含 slow_tool 结果 + 函数名 + 提问）
    view_result = [m for m in tool_msgs if m.get('tool_call_id') == 'call_2']
    assert len(view_result) == 1, "call_2(view_delayed_results) 应回填一次"
    content = view_result[0]['content']
    print("\nview_delayed_results 返回:", content)
    assert 'slow_tool' in content, "返回内容应包含函数名 slow_tool"
    assert 'hello' in content, "返回内容应包含提问内容 hello"
    assert 'slow:hello' in content, "返回内容应包含结果 slow:hello"

    # 缓存读后清空
    assert agent.delayed_results == [], "读后应清空缓存"
    print("\n✅ 延迟结果窗口闭环验证通过")


async def _fast_wait_pending(self, messages, poll_interval=0.01, hard_timeout=5.0):
    waited = 0.0
    while self.pending:
        await asyncio.sleep(poll_interval)
        waited += poll_interval
        finished = []
        for tcid, info in list(self.pending.items()):
            result = self.bus.poll(info['task_id'])
            if result.title in ('Done', 'Error'):
                self.delayed_results.append({
                    'function': info['func_name'],
                    'query': info['query'],
                    'result': str(result.content),
                })
                finished.append(tcid)
        for tcid in finished:
            self.pending.pop(tcid, None)
        if waited >= hard_timeout:
            break
    return None


if __name__ == "__main__":
    asyncio.run(main())
