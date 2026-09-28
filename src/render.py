"""render.py：基于 ANSI 转义序列的「终端固定底栏」渲染。

目标：终端上半部是正文（正常滚动，滚出屏幕的行进入终端滚动历史，可往上翻阅），
最底部固定「状态区 + 多行输入区」若干行，后台线程刷新状态时既不挤走正文也不闪屏。

布局（TTY 模式，用 DECSTBM 限制滚动区）：
    \\x1b[1;{body_bottom}r  把滚动区限定为第 1..body_bottom 行
    ┌──────────────────────────────┐
    │ 正文区：写 \n 自然上滚；        │  ← 滚出滚动区顶部的行进终端 scrollback
    │                              │
    ├──────────────────────────────┤  ← body_bottom，滚动区下边界
    │ listener: ...                │
    │ debug: ...                   │  ← 状态区：每个槽位固定一行，不参与滚动
    │ state: ...                   │
    │ 你: ▏                        │  ← 输入区（默认四行，自动换行）
    │                              │
    │                              │
    │                              │
    └──────────────────────────────┘

为什么不用整屏重绘：旧实现每写一段正文就 \\x1b[H\\x1b[2J 清屏后重写全部缓存行，
一来清掉了终端滚动历史（往上翻看不到刚才的输出），二来每次后台状态刷新都闪一次。
滚动区方案下正文「写一次就留在屏幕上」，刷新状态只需「存光标 → 按绝对行号改那几行
→ 恢复光标」（\\x1b7 / \\x1b8），不碰正文、不闪、不丢历史。

非 TTY 模式（重定向/日志文件）：自动降级为普通 print，不输出 ANSI 乱码。
线程安全：所有对终端的写操作统一经 _write_lock（threading.Lock）串行化。
"""

import os
import re
import shutil
import sys
import threading
import unicodedata
import zlib

_ESC = "\x1b["
_SAVE = "\x1b7"              # DECSC：保存光标位置
_RESTORE = "\x1b8"           # DECRC：恢复光标位置
_CLR = _ESC + "2K"           # 清整行
_RESET_REGION = _ESC + "r"   # 滚动区恢复为整屏
_BOLD = "\x1b[1m"
_DIM = "\x1b[2m"
_ITALIC = "\x1b[3m"
_UNDERLINE = "\x1b[4m"
_COLOR_OFF = "\x1b[0m"
_CYAN = _ESC + "36m"
_YELLOW = _ESC + "33m"
_MAGENTA = _ESC + "35m"

# 标签/状态名 → 颜色。用 crc32 而非 hash()：内置 hash 每进程随机加盐，
# 每次启动同一个 agent 会变色。
_COLORS = (31, 32, 33, 34, 35, 36, 91, 92, 93, 94, 95, 96)
# 只认 [Name] 这种「名字型」标签。故意排除 \[ x^2 \] 这类行首 LaTeX 公式，
# 否则正文里的行间公式会被当成标签染色。
_TAG = re.compile(r"^\[([A-Za-z0-9_:\-]{1,24})\]")
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[78])")
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_MD_LIST = re.compile(r"^(\s*)[-*+]\s+(.+)$")
_MD_ORDERED_LIST = re.compile(r"^(\s*)(\d+)[.)]\s+(.+)$")
_MD_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_MD_RULE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^ )]+)(?:\s+[^)]*)?\)")
_MD_CODE = re.compile(r"`([^`]+)`")


class TerminalRenderer:
    """终端渲染器：上方自由滚动的正文区 + 底部固定状态区 + 多行输入区。

    职责概述：
      - 用 DECSTBM 把滚动区限制在正文区，保证底栏永不滚出屏幕、正文照常进 scrollback。
      - 刷新状态/正文时不做整屏重绘，只用绝对行定位改动的行，避免闪烁。
      - 正文自动渲染常见 Markdown（标题、强调、代码、引用、列表、链接与分隔线）。
      - 行首 [Name] 标签按名字取稳定颜色，区分是哪个 agent 在说话（NO_COLOR 可关闭）。
      - 所有写终端的操作由 _write_lock 串行化（主循环 + 后台 listener 线程）。
      - 非 TTY 时降级为普通 print，不输出 ANSI 乱码。
    """

    def __init__(self, status_slots: list[str] | None = None,
                 stream=None, prompt: str = "你: ", input_rows: int = 4):
        """构造渲染器。

        参数：
          - status_slots: 状态区槽位名列表，每个槽位固定占一行，缺省
            ["listener", "debug", "state"]。
          - stream:       输出流，缺省 sys.stdout。
          - prompt:       输入区提示符，缺省 "你: "；input_line 传入新提示符时会覆盖它。
          - input_rows:   输入区预留行数，缺省 4；输入内容可在其中自动换行。
        内部状态：
          - self._slot_text:    各状态槽当前显示的最新文本（dict）。
          - self._tty:          是否真终端（isatty）——决定走 ANSI 布局还是纯 print。
          - self._color:        是否上色（TTY 且未设 NO_COLOR）。
          - self._size/_cols/_rows/_body_bottom: 当前布局，由 _ensure_layout 按终端实际
            尺寸重算，支持中途改窗口大小。
        """
        self.slots = list(status_slots or ["listener", "debug", "state"])
        self._slot_text = {k: "" for k in self.slots}
        self.stream = stream or sys.stdout
        self.prompt = prompt
        self.input_rows = max(1, int(input_rows))
        self._tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self._color = self._tty and not os.environ.get("NO_COLOR")
        self._write_lock = threading.Lock()
        self._size: tuple[int, int] | None = None   # (cols, rows)，None = 尚未布局
        self._cols, self._rows = 80, 24
        self._body_bottom = 20                      # 正文区最后一行（滚动区下边界）
        self._input_top = 21                        # 输入区首行（由 _ensure_layout 重算）

    # ------------------------------------------------------------- 底层
    def _raw(self, s: str):
        """往终端写字符串并 flush。这是所有输出的唯一出口，内部吞异常（写失败不崩）。

        调用方必须已持 _write_lock，本函数不加锁——一次性整段写入可保证一条消息
        不会在屏幕中间被其他线程插入而错位。
        """
        try:
            self.stream.write(s)
            self.stream.flush()
        except Exception:
            pass

    @staticmethod
    def _split_lines(text: str) -> list[str]:
        """把一段正文拆成「干净的行」列表。

        按 \\n 拆行；中间连续空行最多保留一个，首尾空行去掉（避免大片空白把真正
        的内容顶进滚动历史）。返回空列表表示没有可显示内容，调用方据此跳过写入。
        """
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
        while out and out[0] == "":
            out.pop(0)
        while out and out[-1] == "":
            out.pop()
        return out

    # ------------------------------------------------------------- 上色
    @staticmethod
    def _colorize(label: str, color: bool) -> str:
        """把标签染成按名字确定的颜色（加粗），color=False 时原样返回。"""
        if not color:
            return label
        c = _COLORS[zlib.crc32(label.encode("utf-8")) % len(_COLORS)]
        return f"{_BOLD}{_ESC}{c}m{label}{_COLOR_OFF}"

    def _tag_line(self, line: str) -> str:
        """给正文行行首的 [Name] 标签上色；没有标签则原样返回。"""
        if not self._color:
            return line
        m = _TAG.match(line)
        if not m:
            return line
        tag = m.group(0)
        return self._colorize(tag, True) + line[len(tag):]

    def _prompt_text(self) -> str:
        """输入行提示符：若有 [Name] 前缀同样上色，并按终端宽度截断防换行。"""
        p = self._truncate_cells(self.prompt, max(0, self._cols - 1))
        m = _TAG.match(p)
        if not m:
            return p
        tag = m.group(0)
        return self._colorize(tag, self._color) + p[len(tag):]

    def _status_cell(self, key: str, text: str) -> str:
        """状态区一行：彩色槽位名 + 截断到终端宽度的文本。

        必须截断——状态行超宽会换行，把固定底栏顶乱。
        """
        text = self._truncate_cells(text, max(0, self._cols - self._cell_width(key) - 2))
        return self._colorize(key, self._color) + f": {text}"

    @staticmethod
    def _cell_width(text: str) -> int:
        """返回文本的终端显示格数（中日韩全角字符占两格）。"""
        width = 0
        for char in text:
            if unicodedata.combining(char):
                continue
            width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        return width

    @classmethod
    def _truncate_cells(cls, text: str, limit: int) -> str:
        """按终端显示格数截断，不让任何一个字符跨过右边界。"""
        out = []
        used = 0
        for char in text:
            char_width = 0 if unicodedata.combining(char) else (
                2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
            )
            if used + char_width > limit:
                break
            out.append(char)
            used += char_width
        return "".join(out)

    @staticmethod
    def _clean_status_text(text: str) -> str:
        """将状态内容规整为安全的一行，避免控制码改写固定底栏。"""
        compact = " ".join((text or "").split())
        return "".join(char for char in compact if char.isprintable())

    # ------------------------------------------------------------- Markdown
    def _style(self, text: str, *codes: str) -> str:
        """仅在支持颜色的终端附加 ANSI 样式；非 TTY 保持纯文本。"""
        return "".join(codes) + text + _COLOR_OFF if self._color else text

    def _markdown_inline(self, text: str) -> str:
        """渲染一行内的 Markdown；刻意保持轻量，不依赖第三方包。"""
        # 模型输出不应拥有终端控制权，先移除可能改变光标/颜色的 ANSI 序列。
        text = _ANSI.sub("", text)
        code_parts: list[str] = []

        def save_code(match):
            code_parts.append(self._style(match.group(1), _YELLOW))
            # 私有区字符不会被后续的粗体/斜体正则当作 Markdown 标记。
            return f"\ue000{len(code_parts) - 1}\ue001"

        text = _MD_CODE.sub(save_code, text)
        text = _MD_LINK.sub(
            lambda m: self._style(m.group(1), _UNDERLINE, _CYAN) +
            self._style(f" <{m.group(2)}>", _DIM), text,
        )
        # 粗体必须先于斜体处理，避免 **text** 被拆成两段空强调。
        text = re.sub(r"\*\*(.+?)\*\*|__(.+?)__",
                      lambda m: self._style(m.group(1) or m.group(2), _BOLD), text)
        text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)|(?<!_)_([^_\n]+)_(?!_)",
                      lambda m: self._style(m.group(1) or m.group(2), _ITALIC), text)
        for i, code in enumerate(code_parts):
            text = text.replace(f"\ue000{i}\ue001", code)
        return text

    def render_markdown(self, text: str) -> str:
        """将常用 Markdown 转换为适合 ANSI 终端显示的文本。

        支持 ATX 标题、粗体/斜体、行内及围栏代码、引用、无序/有序列表、链接、
        分隔线和表格边框。未知 Markdown 不会报错，按普通正文显示。
        """
        rendered: list[str] = []
        in_code_fence = False
        for raw_line in (text or "").splitlines():
            line = raw_line.rstrip()
            if line.lstrip().startswith("```") or line.lstrip().startswith("~~~"):
                in_code_fence = not in_code_fence
                continue
            if in_code_fence:
                rendered.append(self._style("  " + _ANSI.sub("", line), _YELLOW))
                continue
            if _MD_RULE.match(line):
                rendered.append(self._style("─" * max(3, min(60, self._cols)), _DIM))
                continue
            heading = _MD_HEADING.match(line)
            if heading:
                level, title = len(heading.group(1)), heading.group(2)
                prefix = "#" * level + " "
                rendered.append(self._style(prefix + self._markdown_inline(title), _BOLD, _MAGENTA))
                continue
            quote = _MD_QUOTE.match(line)
            if quote:
                rendered.append(self._style("│ ", _DIM) + self._markdown_inline(quote.group(1)))
                continue
            bullet = _MD_LIST.match(line)
            if bullet:
                rendered.append(bullet.group(1) + self._style("• ", _CYAN) + self._markdown_inline(bullet.group(2)))
                continue
            ordered = _MD_ORDERED_LIST.match(line)
            if ordered:
                rendered.append(ordered.group(1) + self._style(ordered.group(2) + ". ", _CYAN) +
                                self._markdown_inline(ordered.group(3)))
                continue
            # Markdown 表格的分隔行没有信息量；删掉它让正文更紧凑。
            if "|" in line and re.fullmatch(r"\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*", line):
                continue
            if line.lstrip().startswith("|") and line.rstrip().endswith("|"):
                cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
                rendered.append(self._style("│ ", _DIM) + self._style(" │ ".join(
                    self._markdown_inline(cell) for cell in cells), _DIM) + self._style(" │", _DIM))
                continue
            rendered.append(self._markdown_inline(line))
        return "\n".join(rendered)

    # ------------------------------------------------------------- 布局
    def _ensure_layout(self) -> bool:
        """按当前终端尺寸重算布局；尺寸变化（含首次）时重设滚动区并返回 True。

        调用方必须已持 _write_lock。尺寸变化后正文区末行可能残留旧的底栏内容，
        会在新正文上滚时被带走，不做整屏重绘（重绘正是本文件想避免的）。
        """
        cols, rows = shutil.get_terminal_size(fallback=(80, 24))
        if (cols, rows) == self._size:
            return False
        if self._size is None:
            # 首次布局：先把终端里已有内容顶进滚动历史，光标落到屏幕末尾，
            # 免得底栏画在屏幕中间、上方留一段别的内容。
            self._raw("\n" * rows)
        self._size = (cols, rows)
        self._cols, self._rows = cols, rows
        # 状态区与输入区均固定在底部；正文只在其上方的 DECSTBM 区域中滚动。
        self._body_bottom = max(1, rows - len(self.slots) - self.input_rows)
        self._input_top = self._body_bottom + len(self.slots) + 1
        self._raw(f"{_ESC}1;{self._body_bottom}r")
        return True

    def _footer(self):
        """重画底部「状态区 + 多行输入区」（绝对行定位）。调用方必须已持 _write_lock。"""
        out = ""
        for i, key in enumerate(self.slots):
            out += f"{_ESC}{self._body_bottom + 1 + i};1H{_CLR}"
            if self._slot_text[key]:
                out += self._status_cell(key, self._slot_text[key])
        # 每次显示新提示符前清掉整块输入区，避免上一次长输入换行后留下残字。
        for row in range(self._input_top, self._rows + 1):
            out += f"{_ESC}{row};1H{_CLR}"
        out += f"{_ESC}{self._input_top};1H{self._prompt_text()}"
        self._raw(out)

    # ------------------------------------------------------------- 正文
    def body_write(self, text: str):
        """向正文区追加一段文字（线程安全，可被多 Agent / 后台线程调用）。

        光标先存起来，跳到滚动区末行再写，正文自然上滚——滚出滚动区顶部的行由终端
        存进 scrollback，往上翻仍能看到；最后恢复光标，用户键入的内容不受影响。
        """
        lines = self._split_lines(self.render_markdown(text))
        if not lines:
            return
        with self._write_lock:
            if not self._tty:
                self._raw("\n".join(lines) + "\n")
                return
            relayout = self._ensure_layout()
            out = _SAVE + f"{_ESC}{self._body_bottom};1H"
            for ln in lines:
                out += self._tag_line(ln) + "\n"
            out += _RESTORE
            self._raw(out)
            if relayout:
                self._footer()

    # ------------------------------------------------------------- 状态槽
    def status_set(self, key: str, text: str):
        """更新状态区中某个槽位的最新一行（线程安全，后台线程可调用）。

        与 body_write 的区别：状态只保留「最新一行」，每次只重写该槽位所在的固定
        行，不动正文、不清屏，因此后台转写线程高频刷新也不会闪。
        key 不在预定义 slots 里则忽略；正文里的换行会被压成空格（否则会顶乱底栏）。
        """
        if key not in self._slot_text:
            return
        text = self._clean_status_text(text)
        with self._write_lock:
            self._slot_text[key] = text
            if not self._tty:
                if text:
                    self._raw(f"[{key}] {text}\n")
                return
            if self._ensure_layout():
                self._footer()      # 尺寸变了：所有槽位都换了行号，整条底栏重画
                return
            row = self._body_bottom + 1 + self.slots.index(key)
            self._raw(_SAVE + f"{_ESC}{row};1H{_CLR}{self._status_cell(key, text)}" + _RESTORE)

    # ------------------------------------------------------------- 输入
    async def input_line(self, prompt: str | None = None) -> str:
        """在底部多行输入区绘制提示符，并异步读取用户的一行输入。

        等待期间不持 _write_lock，也不占用事件循环（input 丢进线程池），因此后台
        listener 线程仍可随时 status_set 刷新状态区。输入内容会在预留区域内自然自动
        换行；按 Enter 后返回去掉首尾空白的输入。
        """
        import asyncio
        p = prompt if prompt is not None else self.prompt
        if not self._tty:
            self._raw(p)
            return (await asyncio.to_thread(input, "")).strip()
        with self._write_lock:
            self.prompt = p         # 旧实现写进 _prompt_override 但重绘时读 self.prompt，覆盖从未生效
            self._ensure_layout()
            self._footer()
        return (await asyncio.to_thread(input, "")).strip()

    # ------------------------------------------------------------- 会话
    def shutdown(self):
        """退出前恢复终端：解除滚动区限制、复位颜色、光标落到屏幕末尾换一行。

        不重印正文——正文本来就在屏幕上（滚动历史也在），清屏反而会把它们抹掉。
        """
        with self._write_lock:
            if not self._tty:
                return
            self._raw(_RESET_REGION + _COLOR_OFF + f"{_ESC}{self._rows};1H{_CLR}\n")


# ------------------ 项目内共享的单例渲染器 ------------------
# 项目内所有模块（Agent、事件总线、主循环）通过这组函数共享同一个渲染器实例，
# 避免各自 new 一个导致多份终端布局互相打架。
_renderer: TerminalRenderer | None = None


def set_renderer(r: TerminalRenderer):
    """设置全局唯一的 TerminalRenderer 实例。

    通常在程序入口（super.py / terminal.py）创建好渲染器后调用一次，
    把渲染器绑定到全局，之后任何模块 get_renderer() 都拿到同一个对象。
    """
    global _renderer
    _renderer = r


def get_renderer() -> TerminalRenderer:
    """获取全局共享的渲染器；若尚未设置则惰性创建一个默认实例。

    这样即使某个模块在入口尚未显式 set_renderer 之前需要渲染，也能拿到
    一个可用的默认渲染器（默认状态槽 listener/debug/state）。
    """
    global _renderer
    if _renderer is None:
        _renderer = TerminalRenderer()
    return _renderer


if __name__ == "__main__":
    # 离线自检：不连模型、不开终端，只验渲染逻辑（着色/截断/布局/降级）。
    import io

    r = TerminalRenderer(status_slots=["listener", "debug", "state"],
                         stream=io.StringIO())          # 非 TTY
    assert not r._tty
    r.status_set("listener", "转写中\n很长的第二行")     # 非 TTY：普通打印
    assert r.stream.getvalue() == "[listener] 转写中 很长的第二行\n", r.stream.getvalue()
    r.status_set("unknown", "x")                        # 未知槽位忽略
    assert "unknown" not in r.stream.getvalue()
    r.body_write("a\n\n\n\nb\n\n")                      # 连续空行压成一个、首尾空行去掉
    assert r.stream.getvalue().endswith("a\n\nb\n"), r.stream.getvalue()
    plain_md = r.render_markdown("# 标题\n\n- **重点** 与 `code`\n> 引用\n[官网](https://example.com)")
    assert plain_md == "# 标题\n\n• 重点 与 code\n│ 引用\n官网 <https://example.com>", plain_md

    r._tty, r._color = True, True
    # 标签染色：同一名字恒定同色，且 [Super] 与 [math] 不同色
    a, b = r._tag_line("[Super] hi"), r._tag_line("[Super] yo")
    assert a.split("]")[0] == b.split("]")[0] and "hi" in a
    assert r._tag_line("[math] x").split("]")[0] != a.split("]")[0]
    # 行首行间公式不能被当成标签（LaTeX 正文常见）
    assert r._tag_line(r"\[ x^2 \]") == r"\[ x^2 \]"
    assert r._tag_line("plain") == "plain"
    rich_md = r.render_markdown("## 标题\n- **重点**\n```python\nx = 1\n```")
    assert _BOLD in rich_md and _YELLOW in rich_md and "x = 1" in rich_md
    # 截断：状态行/提示符超宽会顶乱底栏；中文全角字符占两格。
    assert "\x1b[0m: " in r._status_cell("debug", "y" * 999)
    assert r._cell_width(re.sub(r"\x1b\[[0-9;]*m", "", r._status_cell("debug", "y" * 999))) \
        == r._cols, "状态行必须按终端宽度截断"
    r._cols = 10
    assert r._cell_width(re.sub(r"\x1b\[[0-9;]*m", "", r._status_cell("debug", "中文中文中文"))) <= 10
    assert r._clean_status_text("a\x1b[2J\nb") == "a[2J b"
    r._cols, r._rows, r.slots = 80, 24, ["listener", "debug", "state"]
    r._size = None
    with r._write_lock:
        assert r._ensure_layout() is True           # 首次布局
        assert (r._body_bottom, r._input_top, r._rows) == (17, 21, 24), \
            (r._body_bottom, r._input_top, r._rows)
        assert r._ensure_layout() is False          # 尺寸没变不重复设滚动区
    print("render.py self-check OK")
