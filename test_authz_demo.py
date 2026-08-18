"""鉴权白名单 + 慢任务 GC 演示测试（不依赖 openai/pydantic 等重库，用 stub 注入）"""
import sys, types, json, asyncio, time

# ---------- stub 重依赖，仅让 Agent / event_bus 可 import ----------
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
from Agent import Agent, tool_call_add


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


# ---------- 最小 Tools（只要 schema 字段供 run_agent 过滤） ----------
class FakeTools:
    def __init__(self, names):
        self.schema = [{"function": {"name": n}} for n in names]
        self.tool_list = {}
        self.timeout = {}
    async def async_execute(self, func_name, **kw):
        return f"executed:{func_name}"


def make_bus(tool_names_schema, submitted_log):
    tools = FakeTools(tool_names_schema)
    bus = Bus(tools, max_concurrency=4)
    # 拦截 submit：记录被实际提交的工具名，返回 Done（快任务）
    orig_submit = bus.submit
    async def spy_submit(func_name, **kw):
        submitted_log.append(func_name)
        return Message(title='Done', content=f"done:{func_name}")
    bus.submit = spy_submit
    return bus


async def test_authz():
    print("===== 测试 1：执行层鉴权（专家白名单） =====")

    # 专家白名单只允许 recognize_doc / retrieve_context
    allowed = ["recognize_doc", "retrieve_context"]
    total_schema = ["recognize_doc", "retrieve_context", "mathwrite_expert", "write_latex"]

    submitted = []
    bus = make_bus(total_schema, submitted)
    agent = Agent(bus)

    # LLM 第 1 轮越权调用其他专家 -> 应被拒绝且不入 submitted
    # 第 2 轮返回纯文本结束
    script = [
        FakeMsg(tool_calls=[FakeTC("c1", "mathwrite_expert", {"task": "x"})]),
        FakeMsg(content="最终回答"),
    ]
    client = FakeClient(script)
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]

    out = await agent.run_agent(client, messages, model="m", max_step=5, tool_names=allowed)

    print("  越权工具是否被实际提交：", submitted, "(应为空 [])")
    assert submitted == [], "越权工具 mathwrite_expert 不应被提交！"

    # 检查回填的 tool 消息里包含「拒绝调用」
    denied = [m for m in messages if isinstance(m, dict) and m.get('role') == 'tool' and '拒绝调用' in str(m.get('content'))]
    print("  回填拒绝消息数：", len(denied), "(应 >= 1)")
    assert len(denied) >= 1, "应回填一条拒绝调用说明"

    # 第二场景：白名单内工具应正常执行
    submitted2 = []
    bus2 = make_bus(total_schema, submitted2)
    agent2 = Agent(bus2)
    script2 = [
        FakeMsg(tool_calls=[FakeTC("c2", "recognize_doc", {"doc_path": "test.png"})]),
        FakeMsg(content="识别完成"),
    ]
    client2 = FakeClient(script2)
    messages2 = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    out2 = await agent2.run_agent(client2, messages2, model="m", max_step=5, tool_names=allowed)
    print("  白名单内工具实际提交：", submitted2, "(应含 recognize_doc)")
    assert "recognize_doc" in submitted2, "白名单内工具应正常提交"

    print("  ✅ 执行层鉴权测试通过\n")


async def test_super_full():
    print("===== 测试 1b：Super 无白名单可调度全部（tool_names=None） =====")
    total_schema = ["recognize_doc", "mathwrite_expert", "draw_expert"]
    submitted = []
    bus = make_bus(total_schema, submitted)
    agent = Agent(bus)
    script = [
        FakeMsg(tool_calls=[FakeTC("c1", "draw_expert", {"task": "draw"})]),
        FakeMsg(content="done"),
    ]
    client = FakeClient(script)
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    await agent.run_agent(client, messages, model="m", max_step=5)  # tool_names=None
    print("  Super 提交的工具：", submitted, "(应含 draw_expert)")
    assert "draw_expert" in submitted, "Super 无白名单应能调度任意专家"
    print("  ✅ Super 全量调度测试通过\n")


async def test_gc():
    print("===== 测试 2：慢任务结果无人 poll 时过期清理（隐患 9） =====")
    tools = FakeTools(["slow_x"])
    bus = Bus(tools, max_concurrency=4)
    bus.slow_tasks.add("slow_x")   # 标记为慢任务
    # 让 slow_x 快速完成
    async def _fake_slow(id, func_name, **kw):
        bus.task_results[id] = Message(title='Done', content="result")
        bus._task_done_at[id] = time.monotonic()
    bus._run_slow = _fake_slow

    # 提交一个慢任务并等它完成后，不 poll，模拟「无人取走」
    r = await bus.submit("slow_x")
    tid = r.content['id']
    await asyncio.sleep(0.05)  # 让后台完成
    assert bus.task_results.get(tid) is not None, "结果应已写入"

    # 将 TTL 设为一个极小值，模拟过了很久
    bus._task_ttl = -1.0
    # 触发一次 poll（惰性 GC），会清理过期结果；对另一个不存在的 id poll 也触发 GC
    before = dict(bus.task_results)
    bus._gc_task_results()
    after = dict(bus.task_results)
    print("  GC 前 task_results 项数：", len(before), " GC 后：", len(after), "(应减少)")
    assert tid not in after, "过期结果应被自动清理，不再残留内存"
    print("  ✅ 慢任务过期清理测试通过\n")


async def main():
    await test_authz()
    await test_super_full()
    await test_gc()
    print("===== 全部演示测试通过 =====")


if __name__ == "__main__":
    asyncio.run(main())
