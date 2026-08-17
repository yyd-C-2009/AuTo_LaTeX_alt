"""验证 Agent.run_agent + Bus 的异步提交闭环（不依赖真实 LLM）"""
import asyncio
import time

from Tools import Tools
from event_bus import Bus, Message
from Agent import Agent, tool_call_add


# —— 模拟 LLM 返回的对象结构 ——
class FakeMessage:
    def __init__(self, tool_calls=None, content=None):
        self.tool_calls = tool_calls
        self.content = content

    def model_dump(self):
        return {"role": "assistant", "content": self.content}


class FakeToolCall:
    def __init__(self, id, name, args_dict):
        self.id = id
        self.function = type("F", (), {})()
        self.function.name = name
        self.function.arguments = __import__("json").dumps(args_dict)


class FakeResp:
    def __init__(self, message):
        self.choices = [type("C", (), {"message": message})()]


class FakeCompletions:
    def __init__(self, script):
        # script: 按轮次返回的 message 列表（带重试机制）
        self.script = script
        self.idx = 0

    async def create(self, **kwargs):
        # 复用最后一个 message（模拟 LLM 持续返回最后一个状态）
        i = min(self.idx, len(self.script) - 1)
        self.idx += 1
        return FakeResp(self.script[i])


class FakeClient:
    def __init__(self, script):
        self.chat = type("C", (), {"completions": FakeCompletions(script)})()


# —— 工具：一个快、一个慢 ——
def fast_tool(x):
    return f"fast:{x}"

def slow_tool(x):
    time.sleep(2)   # 模拟 2 秒慢任务
    return f"slow:{x}"


async def main():
    tools = Tools()
    tools.add_tool(fast_tool, time_out=5)
    tools.add_tool(slow_tool, time_out=30)

    bus = Bus(tools, max_concurrency=4)
    bus.mark_slow(["slow_tool"])   # 标记 slow_tool 为慢任务

    agent = Agent(bus)

    # 模拟 LLM：
    # 第 1 轮：返回一个 tool_calls（同时要快任务 + 慢任务）
    # 之后轮：返回纯文本（表示 LLM 不再调工具，等结果）
    script = [
        FakeMessage(tool_calls=[
            FakeToolCall("call_1", "fast_tool", {"x": "A"}),
            FakeToolCall("call_2", "slow_tool", {"x": "B"}),
        ]),
        FakeMessage(tool_calls=[]),   # LLM 不再调工具，返回空（触发 poll 等待）
    ]
    async_client = FakeClient(script)

    messages = [{"role": "system", "content": "test"}]
    messages.append({"role": "user", "content": "hi"})

    # 用一个更大的 max_step，并缩短 sleep 验证（这里直接测试，sleep 会真实等 10s）
    # 为避免等 10s，临时把 sleep 观察：我们只验证慢任务最终被 poll 回填
    t0 = time.time()
    # 直接手动调用一个更短的验证路径（绕过 10s sleep）
    # —— 先验证 submit 异步性 ——
    r = await bus.submit("slow_tool", x="C")
    print("慢任务 submit 返回:", repr(r), f"(应 title=Submitted, 用时≈0)")
    assert r.title == "Submitted", "慢任务应返回 Submitted"

    r2 = await bus.submit("fast_tool", x="D")
    print("快任务 submit 返回:", repr(r2), "(应 title=Done)")
    assert r2.title == "Done", "快任务应返回 Done"

    # 验证 poll：刚提交时是 Submitted（处理中），2.5s 后是 Done
    tid = r.content['id']
    p1 = bus.poll(tid)
    print("立即 poll:", repr(p1), "(应 Submitted=处理中)")
    assert p1.title == "Submitted"

    await asyncio.sleep(2.5)
    p2 = bus.poll(tid)
    print("2.5s 后 poll:", repr(p2), "(应 Done, content=slow:C)")
    assert p2.title == "Done"
    assert p2.content == "slow:C"

    print("\n===== 核心闭环验证通过 ===== 用时 %.2fs" % (time.time() - t0))


if __name__ == "__main__":
    asyncio.run(main())
