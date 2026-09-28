"""跨会话历史讨论的 SQLite 检索。"""

import sys
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from persistence import ConversationStore


class InMemoryConversationStore(ConversationStore):
    def __init__(self):
        self.path = Path(":memory:")
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._init()

    def _connect(self):
        return self.connection


class LegacyConversationStore(InMemoryConversationStore):
    def __init__(self):
        self.path = Path(":memory:")
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript("""
          CREATE TABLE conversations(id TEXT PRIMARY KEY, entrypoint TEXT NOT NULL, title TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
          CREATE TABLE branches(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
            name TEXT NOT NULL, agent TEXT NOT NULL, parent_branch_id TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(conversation_id, name));
          INSERT INTO conversations(id, entrypoint, title) VALUES ('old-conversation', 'super', '旧对话');
          INSERT INTO branches(id, conversation_id, name, agent) VALUES ('old-branch', 'old-conversation', 'main', 'super');
        """)
        self._init()


def test_search_finds_older_topic_and_source_number():
    store = InMemoryConversationStore()
    conversation = store.create_conversation("super", "实数构造")
    branch = store.create_branch(conversation, "main", "super")
    store.replace_messages(branch, [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "我们讨论了 Dedekind cuts 的定义和实数构造。"},
        {"role": "assistant", "content": "下一步要证明每个非空有上界的 cut 有最小上界。"},
    ])

    result = store.search_discussion_history("上次的 Dedekind cuts 思路")

    assert "Dedekind cuts" in result
    assert "m000002" in result
    assert "实数构造" in result


def test_generic_recall_returns_recent_saved_discussion():
    store = InMemoryConversationStore()
    conversation = store.create_conversation("terminal", "最近计划")
    branch = store.create_branch(conversation, "main", "super")
    store.replace_messages(branch, [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "每周日整理下一周的学习计划。"},
    ])

    result = store.search_discussion_history("继续上次的思路")

    assert "学习计划" in result


def test_branch_group_persists_and_delete_removes_chat_records():
    store = InMemoryConversationStore()
    conversation = store.create_conversation("super", "课程对话")
    branch = store.create_branch(conversation, "积分", "math")
    store.set_branch_group(branch, "微积分")
    store.replace_messages(branch, [{"role": "user", "content": "积分问题"}])
    store.save_discussion_summary(branch, 1, "已讨论换元法")

    item = store.branches(conversation)[0]
    assert item["group_name"] == "微积分"
    store.delete_branch(branch)
    assert store.branches(conversation) == []
    assert store.discussion_summary(branch) is None


def test_existing_database_gets_default_group_column():
    store = LegacyConversationStore()
    item = store.branches("old-conversation")[0]
    assert item["group_name"] == "未分组"


if __name__ == "__main__":
    tests = [value for name, value in globals().copy().items() if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} 个历史讨论检索测试全部通过。")
