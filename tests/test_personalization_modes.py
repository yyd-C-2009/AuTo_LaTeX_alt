import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from personalization import MODE_LABELS, confirm_auto_mode, recommend_mode, turn_context


class FakeCompletions:
    async def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({"mode": "explore", "reason": "正在逐步厘清方案"}, ensure_ascii=False)
        ))])


class FakeBus:
    def __init__(self, answer):
        self.answer = answer
        self.messages = []

    async def io_print(self, text):
        self.messages.append(text)

    async def io_dialog(self, text):
        self.messages.append(text)
        return self.answer


class PersonalizationModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_review_uses_progress_and_requires_confirmation(self):
        completions = FakeCompletions()
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        history = [{"role": "user", "content": "我们先讨论反演算法"}]
        suggestion = await recommend_mode(client, "test-model", history, "先解释下一步")
        self.assertEqual(suggestion["mode"], "explore")
        self.assertIn("反演算法", completions.kwargs["messages"][1]["content"])

        bus = FakeBus("确认")
        mode = await confirm_auto_mode(client, "test-model", history, "先解释下一步", bus)
        self.assertEqual(mode, "explore")
        self.assertTrue(any("自动建议：探索" in item for item in bus.messages))

    async def test_user_can_replace_auto_suggestion(self):
        client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
        self.assertEqual(await confirm_auto_mode(client, "m", [], "生成文件", FakeBus("产出")), "artifact")

    def test_five_requested_mode_labels(self):
        self.assertEqual(list(MODE_LABELS.values()), ["自动", "讨论", "直接", "产出", "探索"])

    def test_explore_is_stepwise_and_complex_calculation_requires_code(self):
        explore = turn_context("explore", "反演算法")
        self.assertIn("一次解释一个关键点", explore)
        self.assertIn("run_python", turn_context("direct", "统计数据"))
        self.assertIn("不得靠心算", turn_context("direct", "统计数据", allow_python=False))


if __name__ == "__main__":
    unittest.main()
