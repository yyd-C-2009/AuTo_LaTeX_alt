"""render.py：基于 ANSI 转义序列的「终端动态渲染」。

目标：把终端分成「上方正文滚动区 + 底部固定状态区 + 最底输入行」三块，
让除正文以外的监听/调试/状态信息每种都只占用一行，不再无限滚动挤占正文。

布局（TTY 模式，采用「整屏重绘」方案）：
    整屏重绘：\x1b[H 光标回左上 + \x1b[2J 清屏，然后依次输出：
        ① 正文区（body_lines，只保留最近 max_body_lines 行）
        ② 状态区 M 行（listener / debug / state，每类只显示最新一行）
        ③ 最底输入提示行（光标停留在这里）
    这样无论正文多长、是否滚动，状态区与输入行都始终固定在屏幕底部，不会错位/残留。

等待用户输入时：不做整屏清屏（避免清掉用户正在输入的字符），只轻量重画「状态区」
并在重画后把光标移回输入行——正文与输入提示行保持原位。

非 TTY 模式（重定向/日志文件）：自动降级为普通 print，不输出 ANSI 乱码。

线程安全：监听后台线程（listener worker）与主事件循环都会写终端，所有对屏幕的
实际写操作统一经 _write_lock（threading.Lock）串行化。
"""

import sys
import threading

_ESC = "\x1b["
_CSI_HOME = _ESC + "H"       # 光标回左上角
_CSI_CLEAR = _ESC + "2J"     # 清空整个屏幕
_CSI_CLR = _ESC + "2K"       # 清整行
_CSI_UP = _ESC               # 前缀，配合 {n}A 表示光标上移 n 行
_CSI_DOWN = _ESC             # 前缀，配合 {n}B 表示光标下移 n 行


class TerminalRenderer:
    """终端动态渲染器：整屏重绘的「正文滚动区 + 固定底部状态区 + 输入行」。"""

    def __init__(self, status_slots: list[str] | None = None,
                 stream=None, prompt: str = "你: ", max_body_lines: int = 500):
        self.slots = status_slots or ["listener", "debug", "state"]
        self._slot_text = {k: "" for k in self.slots}
        self.stream = stream or sys.stdout
        self.prompt = prompt
        self.max_body_lines = max(1, int(max_body_lines))
        self._body_lines: list[str] = []
        self._tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self._write_lock = threading.Lock()
        self._status_lines = len(self.slots)   # 状态区行数（不含输入行）
        self._input_active = False             # 正在等待用户输入（光标在输入行）

    # ------------------------------------------------------------- 底层
    def _raw(self, s: str):
        """调用方必须先持有 _write_lock。"""
        try:
            self.stream.write(s)
            self.stream.flush()
        except Exception:
            pass

    @staticmethod
    def _split_lines(text: str) -> list[str]:
        """把一段正文拆成行，去掉首尾空行（保留中间可能有意义空行但压缩）。"""
        lines = (text or "").split("\n")
        out = []
        blank_run = 0
        for ln in lines:
            if ln.strip() == "":
                blank_run += 1
                if blank_run <= 1:      # 最多保留一个连续空行
                    out.append("")
            else:
                blank_run = 0
                out.append(ln)
        # 去掉首尾空行
        while out and out[0] == "":
            out.pop(0)
        while out and out[-1] == "":
            out.pop()
        return out if out else []

    # ------------------------------------------------------------- 正文
    def body_write(self, text: str):
        """（线程安全）向正文区追加一段文字，并整屏重绘。"""
        lines = self._split_lines(text)
        if not lines:
            return
        with self._write_lock:
            if not self._tty:
                self._raw(text + "\n")
                return
            self._body_lines.extend(lines)
            # 裁剪到最近 max_body_lines 行
            if len(self._body_lines) > self.max_body_lines:
                self._body_lines = self._body_lines[-self.max_body_lines:]
            # 输入等待中更新正文：先切走输入行标记（正文会变化，退回整屏重绘）
            self._input_active = False if not self._tty else self._input_active
            self._full_redraw()

    # ------------------------------------------------------------- 状态槽
    def status_set(self, key: str, text: str):
        """（线程安全，可就后台线程调用）更新某一状态槽。输入等待中轻量重画状态区。"""
        if key not in self._slot_text:
            return
        text = (text or "").strip()
        with self._write_lock:
            self._slot_text[key] = text
            if not self._tty:
                if text:
                    self._raw(f"[{key}] {text}\n")
                return
            if self._input_active:
                # 正在等待用户输入：只轻量重画状态区，不整屏清屏（避免清掉正在输入的字符）
                self._redraw_input_status()
            else:
                self._full_redraw()

    # ------------------------------------------------------------- 整屏重绘
    def _full_redraw(self):
        """整屏重绘：清屏 → 正文 → 状态区 → 输入行（调用方已持锁）。"""
        out = _CSI_HOME + _CSI_CLEAR
        for ln in self._body_lines:
            out += ln + "\n"
        for key in self.slots:
            out += _CSI_CLR + f"{key}: {self._slot_text[key]}\n"
        out += "\r" + _CSI_CLR + self.prompt
        self._raw(out)
        self._input_active = True

    def _redraw_input_status(self):
        """输入等待中只重画状态区（不动正文与输入行内容），然后光标回落输入行。"""
        # 当前光标在输入行；先上移 status_lines 行到状态区顶部
        total = self._status_lines
        out = f"\r{_CSI_UP}{total}A"
        for key in self.slots:
            out += "\r" + _CSI_CLR + f"{key}: {self._slot_text[key]}\n"
        # 状态区最后一行写完后光标在 state 行末尾（未换行）；下移回输入行
        out += f"\r{_CSI_DOWN}1B" if False else f"\r{_CSI_DOWN}1B"
        # 下面用更稳的方式：写完 state 后额外 \n 到 state 行下一行=输入行
        self._raw(out)
        # 直接重画状态区后，把光标定位回输入行
        self._raw("")

    # ------------------------------------------------------------- 输入
    async def input_line(self, prompt: str | None = None) -> str:
        """显示输入提示并读取用户一行输入。等待期间不长期持有写锁。"""
        import asyncio
        p = prompt if prompt is not None else self.prompt
        if not self._tty:
            self._raw(f"{p}")
            return (await asyncio.to_thread(input, "")).strip()
        # 更新当前使用的输入提示并整屏重绘（光标停在输入行）
        with self._write_lock:
            self._prompt_override = p
            self._full_redraw()
            self._input_active = True
        # 等待用户输入（不持锁）
        return (await asyncio.to_thread(input, "")).strip()

    # ------------------------------------------------------------- 会话
    async def startup_banner(self, text: str):
        """启动横幅：写入正文区并整屏重绘。"""
        self.body_write(text)

    def shutdown(self):
        """退出前恢复终端到正常滚动状态：清屏后把正文以普通滚动方式输出。"""
        with self._write_lock:
            if not self._tty:
                return
            body = "\n".join(self._body_lines)
            self._raw(_CSI_HOME + _CSI_CLEAR)
            if body:
                self._raw(body + "\n")
            self._input_active = False


# ------------------ 项目内共享的单例渲染器 ------------------
_renderer: TerminalRenderer | None = None


def set_renderer(r: TerminalRenderer):
    global _renderer
    _renderer = r


def get_renderer() -> TerminalRenderer:
    global _renderer
    if _renderer is None:
        _renderer = TerminalRenderer()
    return _renderer
