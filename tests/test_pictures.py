"""图片的一轮提取、按需回看与对话文件清理；全程使用假模型，不访问网络。"""

import asyncio
import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from persistence import ConversationStore
from pictures import PictureService, referenced_picture_ids


class ClosingConversationStore(ConversationStore):
    def __init__(self, database_path):
        self.connections = []
        super().__init__(database_path)

    def _connect(self):
        connection = super()._connect()
        self.connections.append(connection)
        return connection

    def close(self):
        for connection in self.connections:
            connection.close()
        self.connections.clear()


class FakeCompletions:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=f"分析结果 {len(self.calls)}"))])


class PictureTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_extract_is_private_text_context_and_view_resends_image(self):
        test_id = uuid.uuid4().hex
        temp = Path(__file__).resolve().parent
        database_path = temp / f".picture-test-{test_id}.db"
        try:
            store = ClosingConversationStore(str(database_path))
            conversation_id = store.create_conversation("super", "图片测试")
            image_path = temp / f"chart-{test_id}.png"
            image_path.write_bytes(b"\x89PNG\r\n\x1a\nimage-bytes")
            client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
            service = PictureService(store)
            image = {"id": "a" * 32, "name": "chart.png", "path": str(image_path), "size": image_path.stat().st_size}

            main_text = await service.prepare_turn(
                client, "deepseek-flash", conversation_id, "请读出图中的数据", [image]
            )

            self.assertIn("图片编号: " + image["id"], main_text)
            self.assertIn("分析结果 1", main_text)
            self.assertNotIn(str(image_path), main_text)
            self.assertNotIn("data:image", main_text)
            self.assertEqual(client.chat.completions.calls[0]["messages"][1]["content"][0]["text"].split("用户本轮问题：", 1)[1], "请读出图中的数据")
            self.assertIn("data:image/png;base64,", client.chat.completions.calls[0]["messages"][1]["content"][1]["image_url"]["url"])
            saved = store.get_picture(conversation_id, image["id"])
            self.assertEqual(saved["summary"], "分析结果 1")

            ids = referenced_picture_ids([{"role": "user", "content": main_text}])
            self.assertEqual(ids, {image["id"]})
            self.assertIn(image["id"], service.list_pictures(conversation_id, ids))
            viewed = await service.view_picture(
                client, "deepseek-flash", conversation_id, image["id"], "读取右上角数值"
            )
            self.assertEqual(viewed, "分析结果 2")
            self.assertIn("读取右上角数值", client.chat.completions.calls[1]["messages"][1]["content"][0]["text"])
        finally:
            if "store" in locals():
                store.close()
            database_path.unlink(missing_ok=True)
            (temp / f"chart-{test_id}.png").unlink(missing_ok=True)

    async def test_branch_delete_removes_unreferenced_image_but_keeps_forked_reference(self):
        imports = Path(__file__).resolve().parents[1] / "imports"
        imports.mkdir(exist_ok=True)
        test_id = uuid.uuid4().hex
        temp_path = Path(__file__).resolve().parent
        database_path = temp_path / f".picture-test-{test_id}.db"
        image_path = imports / f"picture-test-{test_id}.png"
        try:
            store = ClosingConversationStore(str(database_path))
            conversation_id = store.create_conversation("super", "清理测试")
            source = store.create_branch(conversation_id, "source", "super")
            fork = store.create_branch(conversation_id, "fork", "super", source)
            picture_id = "b" * 32
            image_path.write_bytes(b"image")
            store.add_picture(conversation_id, picture_id, "saved.png", str(image_path), "image/png", 5)
            message = {"role": "user", "content": f"[图片编号: {picture_id}]"}
            store.replace_messages(source, [message])
            store.replace_messages(fork, [message])

            store.delete_branch(source)
            self.assertIsNotNone(store.get_picture(conversation_id, picture_id))
            self.assertTrue(image_path.exists())

            store.delete_branch(fork)
            self.assertIsNone(store.get_picture(conversation_id, picture_id))
            self.assertFalse(image_path.exists())
        finally:
            if "store" in locals():
                store.close()
            database_path.unlink(missing_ok=True)
            image_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
