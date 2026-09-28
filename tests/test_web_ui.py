"""网页会话隔离、重复提交与确认输入检查；--preview 提供无模型交互预览。"""
import asyncio
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from web_ui import WebInput, WebRenderer, WebUIServer


class Sessions:
    """模拟现有 TerminalSession 协议，避免加载 OCR/音频模型。"""

    def __init__(self):
        self.active_branch = "微积分笔记"
        self.agent = "math"
        self.active_messages = [{"role": "user", "content": "请推导这个积分。"},
                                {"role": "assistant", "content": r"$$\int_0^1 x^2\,dx=\frac{1}{3}$$"}]
        self.branches = {self.active_branch: {"agent": self.agent, "group": "未分组", "messages": []},
                         "线性代数": {"agent": self.agent, "group": "学习", "messages": [
                             {"role": "user", "content": "解释矩阵的特征值。"}]}}

    def save_active(self):
        self.branches[self.active_branch]["messages"] = copy.deepcopy(self.active_messages)

    def switch_branch(self, name):
        if name not in self.branches:
            return False, "不存在"
        self.save_active()
        self.active_branch = name
        self.active_messages[:] = copy.deepcopy(self.branches[name]["messages"])
        return True, "成功"

    def new_branch(self, name):
        self.branches[name] = {"agent": self.agent, "messages": []}
        return self.switch_branch(name)

    def rename_branch(self, old_name, new_name):
        if old_name not in self.branches or not new_name or (new_name != old_name and new_name in self.branches):
            return False, "无法重命名"
        self.branches[new_name] = self.branches.pop(old_name)
        if self.active_branch == old_name:
            self.active_branch = new_name
        return True, "成功"

    def group_branch(self, name, group_name):
        self.branches[name]["group"] = group_name
        return True, "成功"

    def delete_branch(self, name):
        if len(self.branches) <= 1:
            return False, "至少保留一个"
        if name == self.active_branch:
            self.switch_branch(next(key for key in self.branches if key != name))
        del self.branches[name]
        return True, "成功"


class WebSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_switch_and_duplicate_submission(self):
        r = WebRenderer(asyncio.get_running_loop())
        session = Sessions()
        r.sync_sessions(session)
        waiting = asyncio.create_task(r.input_line("输入"))
        await asyncio.sleep(0)
        self.assertTrue(await r.switch_session({"name": "线性代数"}))
        self.assertEqual(r.snapshot()["body"][0]["text"], "解释矩阵的特征值。")
        self.assertTrue(await r.switch_session({"create": True}))
        self.assertEqual(r.snapshot()["body"], [])
        self.assertTrue(r.submit_input("新的问题\n第二行"))
        self.assertFalse(r.submit_input("重复提交"))
        self.assertEqual(await waiting, "新的问题\n第二行")
        self.assertEqual(len(r.snapshot()["body"]), 1)

    async def test_confirmation_blocks_session_switch(self):
        r = WebRenderer(asyncio.get_running_loop())
        r._session = Sessions()
        waiting = asyncio.create_task(r.input_line("是否删除？y/n"))
        await asyncio.sleep(0)
        self.assertFalse(await r.switch_session({"create": True}))
        r.submit_input("n")
        self.assertEqual(await waiting, "n")

    async def test_auto_mode_confirmation_is_not_saved_as_chat_message(self):
        r = WebRenderer(asyncio.get_running_loop())
        before = len(r.snapshot()["body"])
        waiting = asyncio.create_task(r.input_line("自动方式确认: "))
        await asyncio.sleep(0)
        r.submit_input("确认")
        self.assertEqual(await waiting, "确认")
        self.assertEqual(len(r.snapshot()["body"]), before)

    async def test_reconnect_snapshot_preserves_roles(self):
        r = WebRenderer(asyncio.get_running_loop())
        r.sync_sessions(Sessions())
        event = r.add_client().get_nowait()
        self.assertEqual(event["type"], "snapshot")
        self.assertEqual([m["role"] for m in event["state"]["body"]], ["user", "agent"])

    async def test_group_and_delete_conversation(self):
        r = WebRenderer(asyncio.get_running_loop())
        session = Sessions()
        r.sync_sessions(session)
        waiting = asyncio.create_task(r.input_line("输入"))
        await asyncio.sleep(0)
        self.assertTrue(await r.switch_session({"group": True, "name": "微积分笔记", "group_name": "课程"}))
        item = next(x for x in r.snapshot()["sessions"] if x["name"] == "微积分笔记")
        self.assertEqual(item["group"], "课程")
        self.assertTrue(await r.switch_session({"delete": True, "name": "微积分笔记"}))
        self.assertNotIn("微积分笔记", session.branches)
        self.assertEqual(session.active_branch, "线性代数")
        self.assertFalse(await r.switch_session({"delete": True, "name": "线性代数"}))
        r.submit_input("继续")
        await waiting

    async def test_auto_and_manual_session_naming(self):
        r = WebRenderer(asyncio.get_running_loop())
        session = Sessions()
        session.active_branch = "main"
        session.active_messages = [{"role": "system", "content": "prompt"}]
        session.branches = {"main": {"agent": "math", "messages": session.active_messages}}
        r.sync_sessions(session)
        waiting = asyncio.create_task(r.input_line("输入"))
        await asyncio.sleep(0)
        self.assertTrue(r.submit_input("请帮我推导高斯积分的完整过程"))
        self.assertEqual(await waiting, "请帮我推导高斯积分的完整过程")
        self.assertEqual(session.active_branch, "推导高斯积分的完整过程")

        r.sync_sessions(session)
        waiting = asyncio.create_task(r.input_line("输入"))
        await asyncio.sleep(0)
        self.assertTrue(await r.switch_session({"rename": session.active_branch, "name": "高斯积分"}))
        self.assertEqual(session.active_branch, "高斯积分")
        r.submit_input("继续")
        await waiting

    async def test_auto_mode_names_session_only_after_confirmation(self):
        r = WebRenderer(asyncio.get_running_loop())
        session = Sessions()
        session.active_branch = "main"
        session.active_messages = [{"role": "system", "content": "prompt"}]
        session.branches = {"main": {"agent": "math", "messages": session.active_messages}}
        r.sync_sessions(session)
        r.set_mode("auto")
        waiting = asyncio.create_task(r.input_line("输入"))
        await asyncio.sleep(0)
        text = "讨论数值反演方案"
        r.submit_input(text)
        self.assertEqual(await waiting, text)
        self.assertEqual(session.active_branch, "main")
        r.maybe_rename_session(text)
        self.assertEqual(session.active_branch, "讨论数值反演方案")

    async def test_status_history_keeps_distinct_entries(self):
        r = WebRenderer(asyncio.get_running_loop())
        r.status_set("listener", "开始监听")
        r.status_set("listener", "开始监听")
        r.status_set("listener", "识别到第一句话")
        history = r.snapshot()["status"]["listener"]
        self.assertEqual([item["text"] for item in history], ["开始监听", "识别到第一句话"])
        self.assertTrue(all(item["time"] for item in history))

    async def test_image_attachment_is_returned_as_hidden_payload_and_history_hides_extract(self):
        r = WebRenderer(asyncio.get_running_loop())
        session = Sessions()
        session.active_messages = [{"role": "user", "content": (
            "读图\n\n已附加资料：chart.png\n\n[内部附件上下文]\n"
            "[图片编号: " + "c" * 32 + "] 首次图片信息提取：隐藏摘要\n[/内部附件上下文]"
        )}]
        r.sync_sessions(session)
        shown = r.snapshot()["body"][0]["text"]
        self.assertIn("已附加资料：chart.png", shown)
        self.assertNotIn("内部附件上下文", shown)
        self.assertNotIn("隐藏摘要", shown)

        r._uploads["upload-1"] = {
            "id": "upload-1", "name": "chart.png", "path": "imports/chart.png", "size": 20,
        }
        waiting = asyncio.create_task(r.input_line("输入"))
        await asyncio.sleep(0)
        self.assertTrue(r.submit_input({"text": "请读数", "attachment_ids": ["upload-1"]}))
        result = await waiting
        self.assertIsInstance(result, WebInput)
        self.assertEqual(result.text, "请读数")
        self.assertEqual(result.attachments[0]["id"], "upload-1")
        self.assertIn("已附加资料：chart.png", r.snapshot()["body"][-1]["text"])


async def preview():
    session = Sessions()
    r = WebRenderer(asyncio.get_running_loop())
    server = WebUIServer(r, 0)
    print(server.start(), flush=True)
    try:
        while True:
            r.sync_sessions(session)
            text = await r.input_line("发送至 math")
            session.active_messages.append({"role": "user", "content": text})
            reply = r"这是本地预览。公式示例：\(x_{i_j}+\frac{1}{2}\)。"
            session.active_messages.append({"role": "assistant", "content": reply})
            r.body_write("[math] " + reply)
    finally:
        server.shutdown()


if __name__ == "__main__":
    if "--preview" in sys.argv:
        asyncio.run(preview())
    else:
        unittest.main()
