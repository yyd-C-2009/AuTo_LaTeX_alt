"""Saver 精确删除与检索返回格式的离线回归测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from Saver import Saver


class FakeCollection:
    def __init__(self, memories=None, query_result=None):
        self.memories = dict(memories or {})
        self.query_result = query_result
        self.deleted = []

    def count(self):
        return len(self.memories)

    def get(self, ids, include):
        memory_id = ids[0]
        item = self.memories.get(memory_id)
        if item is None:
            return {"ids": [], "documents": [], "metadatas": []}
        text, metadata = item
        return {
            "ids": [memory_id],
            "documents": [text],
            "metadatas": [metadata],
        }

    def delete(self, ids):
        self.deleted.extend(ids)
        for memory_id in ids:
            self.memories.pop(memory_id, None)

    def query(self, query_embeddings, n_results):
        return self.query_result


def make_saver(collection):
    saver = Saver.__new__(Saver)
    saver.collection = collection
    saver.embed_text = lambda _text: [0.0]
    return saver


def test_delete_derives_id_from_exact_text():
    text = "勾股定理：直角三角形两直角边平方和等于斜边平方。"
    saver = make_saver(FakeCollection())
    memory_id = saver.get_stable_id(text)
    saver.collection.memories[memory_id] = (text, {"source": "test"})

    result = saver.delete_memory(exact_text=text)

    assert result.startswith(f"已删除记忆（ID: {memory_id[:8]}）")
    assert saver.collection.deleted == [memory_id]


def test_delete_rejects_mismatched_id_without_touching_database():
    text = "精确文本"
    saver = make_saver(FakeCollection())

    result = saver.delete_memory(memory_id="0" * 32, exact_text=text)

    assert "ID 与精确文本不匹配" in result
    assert saver.collection.deleted == []


def test_delete_still_checks_stored_text_before_deleting():
    requested_text = "请求删除的文本"
    saver = make_saver(FakeCollection())
    memory_id = saver.get_stable_id(requested_text)
    saver.collection.memories[memory_id] = ("数据库中的其他文本", {})

    result = saver.delete_memory(exact_text=requested_text)

    assert "文本不匹配" in result
    assert saver.collection.deleted == []


def test_retrieve_context_includes_full_ids_and_deduplicates_text():
    first = "第一条记忆"
    second = "第二条记忆"
    first_id = Saver.get_stable_id(None, first)
    second_id = Saver.get_stable_id(None, second)
    collection = FakeCollection(
        memories={first_id: (first, {}), second_id: (second, {})},
        query_result={
            "ids": [[first_id, first_id, second_id]],
            "documents": [[first, first, second]],
        },
    )
    saver = make_saver(collection)

    result = saver.retrieve_context("查询")

    assert result.count(first) == 1
    assert f"[memory_id: {first_id}]\n{first}" in result
    assert f"[memory_id: {second_id}]\n{second}" in result


if __name__ == "__main__":
    tests = [value for name, value in globals().copy().items() if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} 个 Saver 测试全部通过。")
