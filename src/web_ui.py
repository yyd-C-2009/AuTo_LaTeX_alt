"""本地网页 UI：Markdown/KaTeX 正文、实时状态栏和多行输入。

不依赖 FastAPI 等第三方 Python 包。浏览器与后端的三条数据通路：
  1. GET /              —— 单页界面。
  2. GET /events        —— SSE，正文/状态/提示符实时推送。
  3. POST /api/input    —— 将多行编辑器内容交回正在等待的 input_line。
"""

from __future__ import annotations

import asyncio
import json
import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from config import require
from pictures import public_message_text


# 使用模块绝对路径，切换工作目录后仍能加载界面。
from pathlib import Path

_PAGE = Path(__file__).with_name("web_ui.html").read_text(encoding="utf-8")

_AGENT_PREFIX = re.compile(r"^\[([A-Za-z0-9_:\-]{1,24})\]\s*")
_DEFAULT_SESSION_NAME = re.compile(r"^(?:main|Super|未命名会话|会话\s*\d+)$", re.IGNORECASE)
_ATTACHMENT_BLOCK = re.compile(r"\n\n\[本地附件路径（用于工具调用）\][\s\S]*$", re.DOTALL)
_IMPORT_DIR = Path(require("paths.imports"))
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024
_ALLOWED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg"}


@dataclass(frozen=True)
class WebInput:
    """网页一次发送的正文和临时上传附件；调用方负责处理后再交给 Agent。"""

    text: str
    attachments: tuple[dict, ...]


def suggest_session_name(text: str, max_length: int = 24) -> str:
    """从第一条用户消息生成短标题；纯本地规则，不额外消耗一次 LLM 请求。"""
    value = re.sub(r"```[\s\S]*?```", " 代码 ", text or "")
    value = re.sub(r"https?://\S+", " 链接 ", value)
    value = re.sub(r"[`*_>#\[\]{}]", " ", value)
    value = " ".join(value.split()).strip(" \t，。！？!?；;：:")
    value = re.sub(r"^(?:(?:请你?|麻烦|帮我|能否|可以|我想|我需要|如何)\s*)+", "", value)
    first = re.split(r"[。！？!?；;\n]", value, maxsplit=1)[0].strip()
    candidate = first or value or "新对话"
    if len(candidate) > max_length:
        candidate = candidate[:max_length].rstrip() + "…"
    return candidate


class WebRenderer:
    """实现 Bus 所需的渲染接口；正文以原始 Markdown 传给浏览器。"""

    def __init__(self, loop: asyncio.AbstractEventLoop, status_slots=None):
        self.loop = loop
        self.slots = list(status_slots or ["listener", "debug", "state"])
        self._body: list[dict] = []                # 每条消息保存角色、名称和 Markdown 正文。
        self._status = {slot: [] for slot in self.slots}  # 每类状态保留带时间的历史记录。
        self._prompt = ""                          # 空串代表当前没有输入请求。
        self._pending: asyncio.Future | None = None # 当前唯一的 input_line 等待者。
        self._clients: set[queue.Queue] = set()     # 每个 SSE 浏览器连接各有一个输出队列。
        self._lock = threading.RLock()              # 快照与广播在同一临界区内排序。
        self._session = None
        self._active = "Super"
        self._mode = "direct"
        self._turn_mode = "direct"
        self._uploads_enabled = True
        self._uploads: dict[str, dict] = {}
        self._sessions = [{"name": "Super", "agent": "super"}]
        self._next_switchable = False
        self._can_switch = False
        self._suppress_next_input = False

    def sync_sessions(self, session, force=False):
        """在主对话输入前同步真实分支；工具确认输入不开放会话切换。"""
        self._session = session
        session.save_active()
        with self._lock:
            changed = force or self._active != session.active_branch
            self._active = session.active_branch
            self._sessions = [{"name": name, "agent": data["agent"],
                               "group": data.get("group", "未分组")}
                              for name, data in session.branches.items()]
            if changed:
                # 只展示自然语言对话，不暴露模型系统提示及内部工具消息。
                self._body = [{"role": "user" if m["role"] == "user" else "agent",
                               "name": "你" if m["role"] == "user" else session.agent,
                               "text": _display_history_text(m["content"])}
                              for m in session.active_messages
                              if m.get("role") in ("user", "assistant")
                              and isinstance(m.get("content"), str) and m["content"]]
            self._next_switchable = True
            self._publish({"type": "snapshot", "state": self.snapshot()})

    def maybe_rename_session(self, text: str) -> None:
        text = (text or "").split("\n\n[本地附件路径（用于工具调用）]", 1)[0]
        session = self._session
        if (
            session is None
            or not hasattr(session, "rename_branch")
            or not _DEFAULT_SESSION_NAME.match(session.active_branch)
            or any(m.get("role") == "user" for m in session.active_messages)
        ):
            return
        old_name = session.active_branch
        proposed = suggest_session_name(text)
        existing = set(session.branches) - {old_name}
        candidate = proposed
        number = 2
        while candidate in existing:
            suffix = f" {number}"
            candidate = proposed[:max(1, 60 - len(suffix))] + suffix
            number += 1
        renamed, _ = session.rename_branch(old_name, candidate)
        if renamed:
            with self._lock:
                self._active = session.active_branch
                self._sessions = [{"name": name, "agent": item["agent"],
                                   "group": item.get("group", "未分组")}
                                  for name, item in session.branches.items()]
                self._publish({"type": "snapshot", "state": self.snapshot()})

    async def switch_session(self, data):
        """在 Agent 事件循环中切换，避免 HTTP 工作线程直接操作会话。"""
        if not self._can_switch or self._pending is None or self._pending.done():
            return False
        if data.get("rename"):
            old_name = str(data.get("rename", ""))
            new_name = str(data.get("name", "")).strip()
            if not new_name or len(new_name) > 60 or not hasattr(self._session, "rename_branch"):
                return False
            ok, _ = self._session.rename_branch(old_name, new_name)
            if ok:
                with self._lock:
                    self._active = self._session.active_branch
                    self._sessions = [{"name": name, "agent": item["agent"],
                                       "group": item.get("group", "未分组")}
                                      for name, item in self._session.branches.items()]
                    self._publish({"type": "snapshot", "state": self.snapshot()})
                self._next_switchable = False
            return ok
        if data.get("group"):
            name = str(data.get("name", ""))
            group_name = " ".join(str(data.get("group_name", "")).split()).strip() or "未分组"
            if len(group_name) > 40 or not hasattr(self._session, "group_branch"):
                return False
            ok, _ = self._session.group_branch(name, group_name)
            if ok:
                self.sync_sessions(self._session)
                self._next_switchable = False
            return ok
        if data.get("delete"):
            if not hasattr(self._session, "delete_branch"):
                return False
            ok, _ = self._session.delete_branch(str(data.get("name", "")))
            if ok:
                self._prompt = f"[{self._session.active_branch}:{self._session.agent}]"
                self.sync_sessions(self._session)
                self._next_switchable = False
            return ok
        if data.get("create"):
            number = 1
            while f"会话 {number}" in self._session.branches:
                number += 1
            ok, _ = self._session.new_branch(f"会话 {number}")
        else:
            ok, _ = self._session.switch_branch(str(data.get("name", "")))
        if ok:
            self._prompt = f"[{self._session.active_branch}:{self._session.agent}]"
            self.sync_sessions(self._session)
            self._next_switchable = False
        return ok

    def _publish(self, event: dict) -> None:
        with self._lock:
            for client in tuple(self._clients):
                client.put(event)

    def snapshot(self) -> dict:
        with self._lock:
            status = {key: [dict(entry) for entry in history]
                      for key, history in self._status.items()}
            return {"body": list(self._body), "status": status, "prompt": self._prompt,
                    "active": self._active, "sessions": self._sessions, "can_switch": self._can_switch,
                    "mode": self._mode, "uploads_enabled": self._uploads_enabled}

    def set_uploads_enabled(self, enabled: bool) -> None:
        self._uploads_enabled = bool(enabled)

    def set_mode(self, mode: str) -> bool:
        if mode not in ("auto", "discussion", "direct", "artifact", "explore"):
            return False
        with self._lock:
            self._mode = mode
            self._publish({"type": "mode", "mode": mode})
        return True

    def set_turn_mode(self, mode: str) -> None:
        self._turn_mode = mode if mode in ("discussion", "direct", "artifact", "explore") else "direct"

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def turn_mode(self) -> str:
        return self._turn_mode

    def add_client(self) -> queue.Queue:
        client: queue.Queue = queue.Queue()
        with self._lock:
            self._clients.add(client)
            client.put({"type": "snapshot", "state": self.snapshot()})
        return client

    def remove_client(self, client: queue.Queue) -> None:
        with self._lock:
            self._clients.discard(client)

    def body_write(self, text: str) -> None:
        """写入 Agent/系统消息；行首 [Agent] 标签会转换成结构化说话者信息。"""
        text = text or ""
        match = _AGENT_PREFIX.match(text)
        if match:
            message = {"role": "agent", "name": match.group(1), "text": text[match.end():]}
        else:
            message = {"role": "system", "name": "系统", "text": text}
        with self._lock:
            self._body.append(message)
            self._publish({"type": "body", "message": message})

    def status_set(self, key: str, text: str) -> None:
        if key not in self._status:
            return
        text = " ".join((text or "").split())
        if not text:
            return
        entry = {"text": text, "time": time.strftime("%H:%M:%S")}
        with self._lock:
            history = self._status[key]
            if history and history[-1]["text"] == text:
                return
            history.append(entry)
            del history[:-200]
        self._publish({"type": "status", "key": key, "entry": entry})

    async def input_line(self, prompt: str = "你: ") -> str | WebInput:
        if self._pending is not None and not self._pending.done():
            raise RuntimeError("同一时刻只能等待一个网页输入")
        self._prompt = prompt
        self._suppress_next_input = str(prompt).startswith("自动方式确认")
        self._pending = self.loop.create_future()
        self._can_switch = self._next_switchable
        self._next_switchable = False
        self._publish({"type": "prompt", "text": prompt, "can_switch": self._can_switch})
        try:
            result = await self._pending
            return result.strip() if isinstance(result, str) else result
        finally:
            self._prompt = ""
            self._pending = None
            self._suppress_next_input = False

    def submit_input(self, text: str) -> bool:
        """接收网页输入；先展示用户消息，再唤醒 Bus 正在等待的对话回合。"""
        selected_mode = self._mode
        attachment_ids = []
        if isinstance(text, dict):
            attachment_ids = text.get("attachment_ids", [])
            text = text.get("text", "")
        if not isinstance(text, str) or not isinstance(attachment_ids, list) or len(attachment_ids) > 5:
            return False
        if attachment_ids and not self._can_switch:
            return False
        attachments = []
        for attachment_id in attachment_ids:
            item = self._uploads.get(str(attachment_id))
            if item is None:
                return False
            attachments.append(item)
        if sum(item["size"] for item in attachments) > _MAX_UPLOAD_BYTES:
            return False
        with self._lock:
            pending = self._pending
            if pending is None or pending.done() or not self._prompt:
                return False
            suppress = self._suppress_next_input
            self._suppress_next_input = False
            self._prompt = ""
            self._can_switch = False
            visible_text = text
            if attachments:
                visible_text += ("\n\n" if visible_text else "") + "已附加资料：" + "、".join(item["name"] for item in attachments)
            if not suppress:
                message = {"role": "user", "name": "你", "text": visible_text}
                self._body.append(message)
                self._publish({"type": "body", "message": message})
            self._publish({"type": "prompt", "text": "", "can_switch": False})
        def resolve():
            if not pending.done():
                if not suppress and selected_mode != "auto":
                    self.maybe_rename_session(text)
            pending.set_result(WebInput(text, tuple(attachments)) if attachments else text)
        self.loop.call_soon_threadsafe(resolve)
        return True

    def save_upload(self, name: str, content: bytes) -> dict:
        """将验证过的 PDF/图片写入项目 imports/，返回当前运行期文件 ID。"""
        if not self._uploads_enabled:
            raise ValueError("文件识别未启用；请使用标准模式启动程序")
        safe_name = Path(str(name or "")).name
        suffix = Path(safe_name).suffix.lower()
        if suffix not in _ALLOWED_SUFFIXES:
            raise ValueError("仅支持 PDF、PNG、JPG 图片")
        if not content or len(content) > _MAX_UPLOAD_BYTES:
            raise ValueError("文件为空或超过 20 MB")
        valid = (
            (suffix == ".pdf" and content.startswith(b"%PDF-"))
            or (suffix == ".png" and content.startswith(b"\x89PNG\r\n\x1a\n"))
            or (suffix in (".jpg", ".jpeg") and content.startswith(b"\xff\xd8\xff"))
        )
        if not valid:
            raise ValueError("文件内容与扩展名不匹配，已拒绝保存")
        display_name = re.sub(r"[^\w .()\-\u4e00-\u9fff]", "_", safe_name).strip(" .")[:120] or "上传资料" + suffix
        _IMPORT_DIR.mkdir(parents=True, exist_ok=True)
        upload_id = uuid.uuid4().hex
        target = _IMPORT_DIR / f"{upload_id}{suffix}"
        with target.open("xb") as stream:
            stream.write(content)
        record = {"id": upload_id, "name": display_name, "path": str(target.resolve()), "size": len(content)}
        with self._lock:
            self._uploads[upload_id] = record
        return {key: record[key] for key in ("id", "name", "size")}

    def shutdown(self) -> None:
        if self._pending is not None and not self._pending.done():
            self.loop.call_soon_threadsafe(self._pending.cancel)


def _display_history_text(text: str) -> str:
    """隐藏供工具调用的本机路径和图片提取结果，只在回放中显示附件名。"""
    if "[内部附件上下文]" in (text or ""):
        return public_message_text(text)
    match = _ATTACHMENT_BLOCK.search(text or "")
    if not match:
        return text
    names = []
    for line in match.group(0).splitlines():
        if line.startswith("- "):
            names.append(line[2:].split("：", 1)[0].strip())
    suffix = "已附加资料" + ("：" + "、".join(names) if names else "")
    return (text[:match.start()].rstrip() + "\n\n" + suffix).strip()


class WebUIServer:
    """运行本地 HTTP/SSE 服务；所有请求仅监听 127.0.0.1。"""

    def __init__(self, renderer: WebRenderer, port: int = 8765):
        self.renderer = renderer
        try:
            self._server = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        except OSError:
            # 默认端口被其他进程占用时仍可启动；实际端口会反映在 self.url 中。
            self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self._server.server_port}"
        self._thread = threading.Thread(target=self._server.serve_forever, name="AuToLaTeX-WebUI", daemon=True)

    def _handler(self):
        renderer = self.renderer

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return  # 本地界面不污染原有程序日志。

            def send_json(self, value, status=200):
                data = json.dumps(value, ensure_ascii=False).encode("utf-8")
                self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

            def do_GET(self):
                if self.path == "/":
                    data = _PAGE.encode("utf-8")
                    self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
                if self.path == "/api/state":
                    self.send_json(renderer.snapshot()); return
                if self.path != "/events":
                    self.send_error(404); return
                client = renderer.add_client()
                self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.send_header("Cache-Control", "no-cache"); self.send_header("Connection", "keep-alive"); self.end_headers()
                try:
                    while True:
                        try: event = client.get(timeout=20)
                        except queue.Empty: event = {"type": "ping"}
                        self.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode("utf-8")); self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                finally:
                    renderer.remove_client(client)

            def do_POST(self):
                request_path = urlsplit(self.path).path
                if request_path not in ("/api/input", "/api/session", "/api/mode", "/api/upload"):
                    self.send_error(404); return
                if request_path == "/api/upload":
                    origin = self.headers.get("Origin")
                    if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                        self.send_json({"error": "跨来源上传已拒绝"}, 403); return
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                        if not 0 < length <= _MAX_UPLOAD_BYTES:
                            raise ValueError("文件为空或超过 20 MB")
                        name = parse_qs(urlsplit(self.path).query).get("name", [""])[0]
                        result = renderer.save_upload(name, self.rfile.read(length))
                    except (ValueError, OSError) as exc:
                        self.send_json({"error": str(exc)}, 400); return
                    self.send_json({"accepted": True, **result}); return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 1_000_000:
                        raise ValueError("无效长度")
                    data = json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(data, dict):
                        raise ValueError("无效输入")
                except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                    self.send_json({"error": "无效输入"}, 400); return
                if request_path == "/api/mode":
                    accepted = renderer.set_mode(data.get("mode", ""))
                elif request_path == "/api/session":
                    future = asyncio.run_coroutine_threadsafe(renderer.switch_session(data), renderer.loop)
                    try:
                        accepted = future.result(timeout=10)
                    except TimeoutError:
                        future.cancel()
                        self.send_json({"error": "会话切换超时"}, 503); return
                else:
                    text = data.get("text", "")
                    attachment_ids = data.get("attachment_ids", [])
                    accepted = isinstance(text, str) and (bool(text.strip()) or bool(attachment_ids)) and renderer.submit_input({"text": text, "attachment_ids": attachment_ids})
                self.send_json({"accepted": accepted}, 200 if accepted else 409)

        return Handler

    def start(self) -> str:
        self._thread.start()
        return self.url

    def shutdown(self) -> None:
        self._server.shutdown(); self._server.server_close()


if __name__ == "__main__":
    async def _self_check():
        """离线检查多行输入回传及用户/Agent 消息分类，不启动长期服务。"""
        renderer = WebRenderer(asyncio.get_running_loop())
        waiting = asyncio.create_task(renderer.input_line("你: "))
        await asyncio.sleep(0)  # 让 input_line 创建等待中的 Future。
        assert renderer.submit_input("第一行\n第二行")
        assert await waiting == "第一行\n第二行"
        renderer.body_write("[math] $x^2$")
        messages = renderer.snapshot()["body"]
        assert messages[-2]["role"] == "user"
        assert messages[-1] == {"role": "agent", "name": "math", "text": "$x^2$"}

    asyncio.run(_self_check())
    print("web_ui.py self-check OK")
