"""无第三方依赖的 SQLite 状态库。所有写操作均在单个事务内提交。"""

import json
import re
import sqlite3
import uuid
from pathlib import Path
from pictures import contextual_message_text

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class _Store:
    def __init__(self, database_path: str):
        raw_path = Path(database_path)
        # 防御性处理：调用方传相对路径时也绝不跟随运行中的 /cd。
        self.path = raw_path if raw_path.is_absolute() else _PROJECT_ROOT / raw_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init(self):
        with self._connect() as db:
            db.executescript("""
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS conversations (
              id TEXT PRIMARY KEY, entrypoint TEXT NOT NULL, title TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS branches (
              id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
              name TEXT NOT NULL, agent TEXT NOT NULL, parent_branch_id TEXT,
              group_name TEXT NOT NULL DEFAULT '未分组',
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              UNIQUE(conversation_id, name)
            );
            CREATE TABLE IF NOT EXISTS messages (
              id TEXT PRIMARY KEY, branch_id TEXT NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
              sequence_no INTEGER NOT NULL, role TEXT NOT NULL, content TEXT,
              metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              UNIQUE(branch_id, sequence_no)
            );
            CREATE TABLE IF NOT EXISTS workflow_runs (
              id TEXT PRIMARY KEY, task_id TEXT NOT NULL, workflow_name TEXT NOT NULL,
              goal TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS stage_runs (
              id TEXT PRIMARY KEY, workflow_run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
              stage_name TEXT NOT NULL, status TEXT NOT NULL, result TEXT, error TEXT,
              started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS artifacts (
              id TEXT PRIMARY KEY, workflow_run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
              stage_run_id TEXT REFERENCES stage_runs(id) ON DELETE SET NULL,
              path TEXT NOT NULL, kind TEXT NOT NULL, checksum TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS preferences (
              id TEXT PRIMARY KEY, scope TEXT NOT NULL, preference TEXT NOT NULL,
              source TEXT NOT NULL DEFAULT 'user_confirmed', active INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS tasks (
              id TEXT PRIMARY KEY, title TEXT NOT NULL, due_at TEXT NOT NULL,
              timezone TEXT NOT NULL, repeat_rule TEXT NOT NULL DEFAULT 'none',
              status TEXT NOT NULL DEFAULT 'active', source TEXT NOT NULL DEFAULT 'conversation',
              reminder_sent_for TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(status, due_at);
            CREATE TABLE IF NOT EXISTS discussion_summaries (
              branch_id TEXT PRIMARY KEY REFERENCES branches(id) ON DELETE CASCADE,
              covered_until INTEGER NOT NULL DEFAULT 0, summary TEXT NOT NULL,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS pictures (
              id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
              name TEXT NOT NULL, path TEXT NOT NULL, mime_type TEXT NOT NULL,
              size INTEGER NOT NULL, summary TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_pictures_conversation ON pictures(conversation_id, created_at);
            """)
            # 兼容已有的会话库；原分支统一归入“未分组”。
            branch_columns = {row["name"] for row in db.execute("PRAGMA table_info(branches)")}
            if "group_name" not in branch_columns:
                db.execute("ALTER TABLE branches ADD COLUMN group_name TEXT NOT NULL DEFAULT '未分组'")


class ConversationStore(_Store):
    def create_conversation(self, entrypoint="terminal", title="未命名会话") -> str:
        conversation_id = str(uuid.uuid4())
        with self._connect() as db:
            db.execute("INSERT INTO conversations(id, entrypoint, title) VALUES (?, ?, ?)", (conversation_id, entrypoint, title))
        return conversation_id

    def create_branch(self, conversation_id: str, name: str, agent: str, parent_branch_id=None,
                      group_name="未分组") -> str:
        branch_id = str(uuid.uuid4())
        with self._connect() as db:
            db.execute(
                "INSERT INTO branches(id, conversation_id, name, agent, parent_branch_id, group_name) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (branch_id, conversation_id, name, agent, parent_branch_id, group_name),
            )
        return branch_id

    def latest_conversation(self, entrypoint="terminal"):
        with self._connect() as db:
            row = db.execute("SELECT id FROM conversations WHERE entrypoint=? ORDER BY updated_at DESC LIMIT 1", (entrypoint,)).fetchone()
        return row["id"] if row else None

    def branches(self, conversation_id: str) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM branches WHERE conversation_id=? ORDER BY created_at", (conversation_id,)).fetchall()
        return [dict(row) for row in rows]

    def load_messages(self, branch_id: str) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT role, content, metadata_json FROM messages WHERE branch_id=? ORDER BY sequence_no", (branch_id,)).fetchall()
        return [{"role": r["role"], "content": r["content"], **json.loads(r["metadata_json"])} for r in rows]

    def replace_messages(self, branch_id: str, messages: list[dict]) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM messages WHERE branch_id=?", (branch_id,))
            for i, message in enumerate(messages):
                base = {k: v for k, v in message.items() if k not in ("role", "content")}
                db.execute("INSERT INTO messages(id, branch_id, sequence_no, role, content, metadata_json) VALUES (?, ?, ?, ?, ?, ?)", (str(uuid.uuid4()), branch_id, i, message.get("role", "assistant"), message.get("content"), json.dumps(base, ensure_ascii=False, default=str)))
            db.execute("UPDATE conversations SET updated_at=CURRENT_TIMESTAMP WHERE id=(SELECT conversation_id FROM branches WHERE id=?)", (branch_id,))

    def set_agent(self, branch_id: str, agent: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE branches SET agent=? WHERE id=?", (agent, branch_id))

    def rename_branch(self, branch_id: str, name: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE branches SET name=? WHERE id=?", (name, branch_id))

    def set_branch_group(self, branch_id: str, group_name: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE branches SET group_name=? WHERE id=?", (group_name, branch_id))

    def delete_branch(self, branch_id: str) -> None:
        orphaned = []
        with self._connect() as db:
            branch = db.execute("SELECT conversation_id FROM branches WHERE id=?", (branch_id,)).fetchone()
            if branch is None:
                return
            conversation_id = branch["conversation_id"]
            pictures = db.execute(
                "SELECT id, path FROM pictures WHERE conversation_id=?", (conversation_id,)
            ).fetchall()
            db.execute("DELETE FROM branches WHERE id=?", (branch_id,))
            contents = "\n".join(
                row["content"] or ""
                for row in db.execute(
                    "SELECT m.content FROM messages m JOIN branches b ON b.id=m.branch_id "
                    "WHERE b.conversation_id=?", (conversation_id,)
                ).fetchall()
            )
            for picture in pictures:
                if f"[图片编号: {picture['id']}]" not in contents:
                    orphaned.append(picture["path"])
                    db.execute("DELETE FROM pictures WHERE id=?", (picture["id"],))
        if orphaned:
            from config import require
            imports = Path(require("paths.imports")).resolve()
            for raw_path in orphaned:
                path = Path(raw_path).resolve()
                if path.is_relative_to(imports):
                    path.unlink(missing_ok=True)

    def add_picture(self, conversation_id: str, picture_id: str, name: str,
                    path: str, mime_type: str, size: int) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO pictures(id, conversation_id, name, path, mime_type, size) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (picture_id, conversation_id, name, path, mime_type, int(size)),
            )

    def set_picture_summary(self, conversation_id: str, picture_id: str, summary: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE pictures SET summary=? WHERE id=? AND conversation_id=?",
                (summary, picture_id, conversation_id),
            )

    def get_picture(self, conversation_id: str, picture_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM pictures WHERE id=? AND conversation_id=?",
                (picture_id, conversation_id),
            ).fetchone()
        return dict(row) if row else None

    def list_pictures(self, conversation_id: str, picture_ids: set[str]) -> list[dict]:
        if not picture_ids:
            return []
        ids = sorted(picture_ids)
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as db:
            rows = db.execute(
                f"SELECT * FROM pictures WHERE conversation_id=? AND id IN ({placeholders}) ORDER BY created_at",
                (conversation_id, *ids),
            ).fetchall()
        return [dict(row) for row in rows]

    def discussion_summary(self, branch_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT covered_until, summary FROM discussion_summaries WHERE branch_id=?",
                (branch_id,),
            ).fetchone()
        return dict(row) if row else None

    def save_discussion_summary(self, branch_id: str, covered_until: int, summary: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO discussion_summaries(branch_id, covered_until, summary) VALUES (?, ?, ?) "
                "ON CONFLICT(branch_id) DO UPDATE SET covered_until=excluded.covered_until, "
                "summary=excluded.summary, updated_at=CURRENT_TIMESTAMP",
                (branch_id, int(covered_until), summary),
            )

    def search_discussion_history(self, query: str, limit: int = 5) -> str:
        """按主题检索已保存的跨会话用户与助手发言，供 Agent 回忆旧讨论。"""
        query = " ".join(str(query or "").split()).lower()
        if not query:
            return "请提供要查找的主题或关键词。"
        try:
            limit = max(1, min(int(limit), 10))
        except (TypeError, ValueError):
            return "limit 必须是 1 到 10 之间的整数。"

        stop_words = {"我们", "之前", "上次", "讨论", "回顾", "继续", "那个", "这个", "思路"}
        stop_chars = set("我们之前上次讨论回顾继续那个这个思路")
        search_query = query
        for word in sorted(stop_words | {"的"}, key=len, reverse=True):
            search_query = search_query.replace(word, " ")
        terms = {
            term for term in re.findall(r"[a-z0-9_+-]+|[\u4e00-\u9fff]+", search_query)
            if not all(char in stop_chars for char in term)
        }
        for run in re.findall(r"[\u4e00-\u9fff]+", search_query):
            terms.update(
                run[i:i + 2] for i in range(len(run) - 1)
                if not all(char in stop_chars for char in run[i:i + 2])
            )
        terms = sorted(
            (term for term in terms if len(term) > 1 and term not in stop_words),
            key=len, reverse=True,
        )[:12]

        where = " OR ".join("LOWER(m.content) LIKE ?" for _ in terms) if terms else "1=1"
        values = [f"%{term}%" for term in terms]
        with self._connect() as db:
            rows = db.execute(
                "SELECT c.id conversation_id, c.entrypoint, c.title, c.updated_at, "
                "b.name branch_name, m.sequence_no, m.role, m.content "
                "FROM messages m JOIN branches b ON b.id=m.branch_id "
                "JOIN conversations c ON c.id=b.conversation_id "
                f"WHERE m.role IN ('user', 'assistant') AND m.content IS NOT NULL AND ({where}) "
                "ORDER BY c.updated_at DESC, b.created_at DESC, m.sequence_no DESC LIMIT 100",
                values,
            ).fetchall()

        if not rows:
            return "没有找到相关的旧讨论。"
        if terms:
            rows = sorted(
                rows,
                key=lambda row: (
                    sum(str(row["content"]).lower().count(term) for term in terms),
                    row["updated_at"],
                    row["sequence_no"],
                ),
                reverse=True,
            )
        excerpts = []
        for row in rows[:limit]:
            content = contextual_message_text(str(row["content"])).strip()
            positions = [content.lower().find(term) for term in terms if content.lower().find(term) >= 0]
            start = max(0, min(positions) - 300) if positions else 0
            excerpt = content[start:start + 1400]
            if start:
                excerpt = "…" + excerpt
            if len(content) > start + 1400:
                excerpt += "…"
            excerpts.append(
                f"[{row['entrypoint']} / {row['title']} / {row['branch_name']} "
                f"m{int(row['sequence_no']) + 1:06d}] "
                f"{'用户' if row['role'] == 'user' else '助手'}：{excerpt}"
            )
        return "\n---\n".join(excerpts)


class WorkflowStore(_Store):
    def begin(self, task_id: str, workflow_name: str, goal: str) -> str:
        run_id = str(uuid.uuid4())
        with self._connect() as db:
            db.execute("INSERT INTO workflow_runs(id, task_id, workflow_name, goal, status) VALUES (?, ?, ?, ?, 'running')", (run_id, task_id, workflow_name, goal))
        return run_id

    def stage(self, run_id: str, name: str) -> str:
        stage_id = str(uuid.uuid4())
        with self._connect() as db:
            db.execute("INSERT INTO stage_runs(id, workflow_run_id, stage_name, status) VALUES (?, ?, ?, 'running')", (stage_id, run_id, name))
        return stage_id

    def finish_stage(self, stage_id: str, result=None, error=None) -> None:
        status = 'failed' if error else 'done'
        with self._connect() as db:
            db.execute("UPDATE stage_runs SET status=?, result=?, error=?, finished_at=CURRENT_TIMESTAMP WHERE id=?", (status, str(result) if result is not None else None, error, stage_id))

    def finish(self, run_id: str, status: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE workflow_runs SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, run_id))

    def add_artifact(self, run_id: str, stage_id: str, path: str, kind: str, checksum: str) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO artifacts(id, workflow_run_id, stage_run_id, path, kind, checksum) VALUES (?, ?, ?, ?, ?, ?)", (str(uuid.uuid4()), run_id, stage_id, path, kind, checksum))

    def recent_runs(self, limit: int = 10) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM workflow_runs ORDER BY updated_at DESC LIMIT ?", (max(1, limit),)).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["stages"] = [dict(s) for s in db.execute("SELECT stage_name, status, error, started_at, finished_at FROM stage_runs WHERE workflow_run_id=? ORDER BY started_at", (row["id"],)).fetchall()]
                item["artifacts"] = [dict(a) for a in db.execute("SELECT path, kind, checksum FROM artifacts WHERE workflow_run_id=?", (row["id"],)).fetchall()]
                result.append(item)
        return result


class PreferenceStore(_Store):
    """持久化用户明确确认的交互偏好，与语义记忆及对话记录分开。"""

    SCOPES = {"general", "academic", "planning", "discussion"}

    def add(self, scope: str, preference: str) -> str:
        scope = str(scope or "").strip().lower()
        preference = " ".join(str(preference or "").split())
        if scope not in self.SCOPES:
            raise ValueError(f"scope 必须是以下之一：{', '.join(sorted(self.SCOPES))}")
        if not preference:
            raise ValueError("偏好内容不能为空")
        if len(preference) > 500:
            raise ValueError("偏好内容不能超过 500 个字符")
        preference_id = str(uuid.uuid4())
        with self._connect() as db:
            db.execute(
                "INSERT INTO preferences(id, scope, preference) VALUES (?, ?, ?)",
                (preference_id, scope, preference),
            )
        return preference_id

    def list_preferences(self, scope: str | None = None, *, include_inactive: bool = False) -> list[dict]:
        clauses, values = [], []
        if not include_inactive:
            clauses.append("active=1")
        if scope:
            clauses.append("scope=?")
            values.append(scope)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as db:
            rows = db.execute(
                f"SELECT id, scope, preference, source, active, created_at, updated_at "
                f"FROM preferences{where} ORDER BY updated_at DESC, created_at DESC",
                values,
            ).fetchall()
        return [dict(row) for row in rows]

    def get(self, preference_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT id, scope, preference, source, active, created_at, updated_at "
                "FROM preferences WHERE id=? AND active=1",
                (preference_id,),
            ).fetchone()
        return dict(row) if row else None

    def update(self, preference_id: str, preference: str, scope: str | None = None) -> bool:
        preference = " ".join(str(preference or "").split())
        if not preference:
            raise ValueError("偏好内容不能为空")
        if len(preference) > 500:
            raise ValueError("偏好内容不能超过 500 个字符")
        if scope is not None and scope not in self.SCOPES:
            raise ValueError(f"scope 必须是以下之一：{', '.join(sorted(self.SCOPES))}")
        with self._connect() as db:
            if scope is None:
                cursor = db.execute(
                    "UPDATE preferences SET preference=?, updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=? AND active=1",
                    (preference, preference_id),
                )
            else:
                cursor = db.execute(
                    "UPDATE preferences SET scope=?, preference=?, updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=? AND active=1",
                    (scope, preference, preference_id),
                )
        return cursor.rowcount > 0

    def deactivate(self, preference_id: str) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE preferences SET active=0, updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND active=1",
                (preference_id,),
            )
        return cursor.rowcount > 0

    def context_for(self, scope: str, limit: int = 8) -> list[dict]:
        """返回全局偏好和当前场景偏好，优先保留全局规则。"""
        if scope not in self.SCOPES:
            scope = "general"
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, scope, preference FROM preferences "
                "WHERE active=1 AND scope IN ('general', ?) "
                "ORDER BY CASE WHEN scope='general' THEN 0 ELSE 1 END, updated_at DESC "
                "LIMIT ?",
                (scope, max(1, min(int(limit), 20))),
            ).fetchall()
        return [dict(row) for row in rows]

    def export_json(self) -> str:
        return json.dumps(self.list_preferences(), ensure_ascii=False, indent=2)


class TaskStore(_Store):
    """持久化带本地时区和重复规则的提醒事项。时间统一以 UTC 存储。"""

    @staticmethod
    def _normalize_due(due_at: str, timezone: str) -> str:
        from datetime import datetime, timezone as utc
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            zone = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, TypeError) as exc:
            raise ValueError(f"未知时区：{timezone}") from exc
        try:
            value = datetime.fromisoformat(str(due_at).strip())
        except ValueError as exc:
            raise ValueError("时间请使用 ISO 格式，例如 2026-09-26T20:00") from exc
        if value.tzinfo is None:
            value = value.replace(tzinfo=zone)
        return value.astimezone(utc.utc).isoformat(timespec="seconds")

    def create(self, title: str, due_at: str, timezone: str = "Asia/Shanghai", repeat: str = "none") -> str:
        import uuid

        title = " ".join(str(title or "").split())
        repeat = str(repeat or "none").strip().lower()
        if not title:
            raise ValueError("任务标题不能为空")
        if len(title) > 300:
            raise ValueError("任务标题不能超过 300 个字符")
        if repeat not in {"none", "daily", "weekly"}:
            raise ValueError("repeat 只能是 none、daily 或 weekly")
        due = self._normalize_due(due_at, timezone)
        task_id = str(uuid.uuid4())
        with self._connect() as db:
            db.execute(
                "INSERT INTO tasks(id, title, due_at, timezone, repeat_rule) VALUES (?, ?, ?, ?, ?)",
                (task_id, title, due, timezone, repeat),
            )
        return task_id

    def list_tasks(self, *, include_completed: bool = False, limit: int = 50) -> list[dict]:
        where = "" if include_completed else "WHERE status='active'"
        with self._connect() as db:
            rows = db.execute(
                f"SELECT id, title, due_at, timezone, repeat_rule, status, reminder_sent_for "
                f"FROM tasks {where} ORDER BY due_at LIMIT ?",
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def get(self, task_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT id, title, due_at, timezone, repeat_rule, status, reminder_sent_for "
                "FROM tasks WHERE id=?", (task_id,),
            ).fetchone()
        return dict(row) if row else None

    def complete(self, task_id: str) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE tasks SET status='completed', updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND status='active'", (task_id,),
            )
        return cursor.rowcount > 0

    def defer(self, task_id: str, due_at: str, timezone: str | None = None) -> bool:
        task = self.get(task_id)
        if task is None or task["status"] != "active":
            return False
        zone = timezone or task["timezone"]
        due = self._normalize_due(due_at, zone)
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE tasks SET due_at=?, timezone=?, reminder_sent_for=NULL, "
                "updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='active'",
                (due, zone, task_id),
            )
        return cursor.rowcount > 0

    def due_reminders(self, now=None) -> list[dict]:
        """原子领取到期提醒；重启后逾期事项只提醒一次，重复项跳到下一次未来时刻。"""
        from datetime import datetime, timedelta, timezone as utc
        from zoneinfo import ZoneInfo

        now = now or datetime.now(utc.utc)
        now = now.astimezone(utc.utc)
        due_items = []
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT * FROM tasks WHERE status='active' AND due_at<=? "
                "AND (reminder_sent_for IS NULL OR reminder_sent_for<>due_at) ORDER BY due_at",
                (now.isoformat(timespec="seconds"),),
            ).fetchall()
            for row in rows:
                item = dict(row)
                due_items.append(item)
                if item["repeat_rule"] == "none":
                    db.execute(
                        "UPDATE tasks SET reminder_sent_for=due_at, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (item["id"],),
                    )
                    continue
                step_days = 1 if item["repeat_rule"] == "daily" else 7
                zone = ZoneInfo(item["timezone"])
                next_due = datetime.fromisoformat(item["due_at"]).astimezone(zone)
                while next_due.astimezone(utc.utc) <= now:
                    next_due += timedelta(days=step_days)
                next_utc = next_due.astimezone(utc.utc).isoformat(timespec="seconds")
                db.execute(
                    "UPDATE tasks SET due_at=?, reminder_sent_for=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (next_utc, item["id"]),
                )
        return due_items
