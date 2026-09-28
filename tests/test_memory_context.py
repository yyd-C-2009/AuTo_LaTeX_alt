"""新分支只在首轮注入少量相关长期记忆。"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from discussion import prepare_conversation
from personalization import turn_context


class Memory:
    def __init__(self, result="[memory_id: abc]\n用户正在学习实数构造"):
        self.result = result
        self.calls = []

    def retrieve_context(self, query, top_k):
        self.calls.append((query, top_k))
        return self.result


def test_first_turn_injects_relevant_memory_once():
    memory = Memory()
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "继续讨论 Dedekind 分割"},
    ]

    prepared = asyncio.run(prepare_conversation(None, messages, None, None, "model", memory))

    assert memory.calls == [("继续讨论 Dedekind 分割", 3)]
    assert prepared[1]["role"] == "system"
    assert "用户正在学习实数构造" in prepared[1]["content"]
    assert prepared[-1] == messages[-1]
    assert len(messages) == 2  # 注入内容只属于本次模型上下文，不写入聊天记录。


def test_existing_conversation_does_not_reload_memory():
    memory = Memory()
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "第一轮"},
        {"role": "assistant", "content": "回答"},
        {"role": "user", "content": "继续"},
    ]

    prepared = asyncio.run(prepare_conversation(None, messages, None, None, "model", memory))

    assert prepared is messages
    assert memory.calls == []


def test_memory_policy_is_only_shown_when_retrieval_is_available():
    assert "调用 retrieve_context" in turn_context("direct", "问题", allow_memory_retrieval=True)
    assert "调用 retrieve_context" not in turn_context("direct", "问题", allow_memory_retrieval=False)


if __name__ == "__main__":
    tests = [value for name, value in globals().copy().items() if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} 个记忆上下文测试全部通过。")
