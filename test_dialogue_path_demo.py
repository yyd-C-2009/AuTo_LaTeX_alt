"""PassageWrite 数据通路验证：专家被调用时能拿到「用户 ↔ Super」对话历史。

验证点：
1. _dialogue_context 正确过滤 role:tool / 空 content / tool_calls，只留 user/assistant 纯文本。
2. build_super_tools 生成的专家工具，调用时构造的 messages 里注入了对话历史。
3. 无历史时（空对话）不注入（user_content 保持为原始 task）。

用法（本机，需真实依赖 pydantic/openai 等）：
    python test_dialogue_path_demo.py
"""
import asyncio
import json
import types

from super import build_super_tools, _dialogue_context, EXPERTS, MODEL
from event_bus import Bus
from Tools import Tools


# ---------- fake LLM client：捕获 create 收到的 messages ----------
class FakeMsg:
    def __init__(self, content):
        self.content = content
        self.tool_calls = None
    def model_dump(self):
        return {"role": "assistant", "content": self.content}

class FakeResp:
    def __init__(self, msg):
        self.choices = [types.SimpleNamespace(message=msg)]

class FakeCompletions:
    def __init__(self, captured):
        self.captured = captured
    async def create(self, messages, **kw):
        self.captured["messages"] = messages
        return FakeResp(FakeMsg("专家回答"))

class FakeClient:
    def __init__(self, captured):
        self.chat = types.SimpleNamespace(completions=FakeCompletions(captured))


def _call_expert(fn, task):
    """调用专家工具函数（它是 async），传入 task。"""
    return asyncio.run(fn(task=task))


async def test_1_dialogue_context():
    print("===== 测试 1：_dialogue_context 过滤 =====")
    messages = [
        {"role": "system", "content": "Super"},
        {"role": "user", "content": "帮我总结勾股定理"},
        {"role": "assistant", "content": None, "tool_calls": [{"function": {"name": "x"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "任务已提交"},
        {"role": "assistant", "content": "已完成"},
    ]
    out = _dialogue_context(messages)
    print(out)
    assert "帮我总结勾股定理" in out
    assert "已完成" in out
    assert "task_id" not in out and "tool_calls" not in out
    print("  ✅ 过滤正确\n")


async def test_2_history_injected():
    print("===== 测试 2：专家 messages 注入对话历史 =====")
    captured = {}
    tools = Tools()
    bus = Bus(tools, max_concurrency=4)
    history = [
        {"role": "system", "content": "Super"},
        {"role": "user", "content": "帮我总结一下勾股定理"},
        {"role": "assistant", "content": "好的，我来安排。"},
    ]
    fake_client = FakeClient(captured)
    build_super_tools(bus, fake_client, history)

    # 找到 passagewrite_expert 工具函数
    fn = bus.tools.tool_list["passagewrite_expert"]
    # 直接调用专家（fake client 会捕获它构造的 messages）
    await fn(task="请把上述对话总结成 LaTeX 文档")

    msgs = captured.get("messages")
    assert msgs is not None, "专家未调用 LLM（messages 未被捕获）"
    print("  专家构造的 messages：")
    for m in msgs:
        print(f"    [role={m['role']}] {str(m['content'])[:80]}")
    # system 是 passagewrite 的提示词
    assert msgs[0]["role"] == "system"
    # 第二个 user 消息应包含历史 + 当前任务
    user_content = msgs[1]["content"]
    assert "帮我总结一下勾股定理" in user_content, "历史应注入到 user 消息"
    assert "好的，我来安排。" in user_content, "Super 的回答也应在历史里"
    assert "【当前任务】" in user_content, "应包含当前任务标记"
    print("  ✅ 历史注入成功\n")


async def test_3_no_history():
    print("===== 测试 3：无历史时不注入 =====")
    captured = {}
    tools = Tools()
    bus = Bus(tools, max_concurrency=4)
    fake_client = FakeClient(captured)
    build_super_tools(bus, fake_client, [{"role": "system", "content": "Super"}])  # 只有 system，无实质对话

    fn = bus.tools.tool_list["passagewrite_expert"]
    await fn(task="直接写一段文档")
    msgs = captured.get("messages")
    user_content = msgs[1]["content"]
    print("  无历史时 user_content:", repr(user_content[:60]))
    assert user_content == "直接写一段文档", "无历史时不应注入额外内容"
    print("  ✅ 无历史处理正确\n")


async def main():
    await test_1_dialogue_context()
    await test_2_history_injected()
    await test_3_no_history()
    print("===== PassageWrite 数据通路验证通过 =====")


if __name__ == "__main__":
    asyncio.run(main())
