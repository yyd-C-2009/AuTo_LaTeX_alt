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
    """终端动态渲染器：整屏重绘的「正文滚动区 + 固定底部状态区 + 输入行」。

    职责概述：
      - 把终端纵向划分为三层：① 上方「正文滚动区」② 底部「状态区」（每类状态仅占一行）
        ③ 最底部「输入行」（用户在此输入）。
      - 通过 ANSI 转义序列实现整屏重绘或局部重绘，保证状态区/输入行永不滚出屏幕。
      - 所有实际写终端的操作都由内部 _write_lock 串行化，保证多线程（主循环 + 后台
        listener worker）同时写屏不交错。
      - 非 TTY（重定向/日志文件）时自动降级为普通 print，不输出 ANSI 乱码。
    """

    def __init__(self, status_slots: list[str] | None = None,
                 stream=None, prompt: str = "你: ", max_body_lines: int = 500):
        """构造渲染器。

        参数：
          - status_slots: 状态区要显示的「槽位名」列表，每个名字在底部状态区各占固定
            一行，缺省为 ["listener", "debug", "state"]（监听 / 调试 / 状态）。
          - stream:       输出流，缺省为 sys.stdout。
          - prompt:       底部输入行的提示符，缺省 "你: "。
          - max_body_lines: 正文滚动区最多保留的行数（内存/屏幕裁剪，防无限累积）。
        内部状态：
          - self.slots:        状态槽位名列表（顺序即底部显示顺序）。
          - self._slot_text:   每个状态槽当前显示的最新文本（dict）。
          - self._body_lines:  正文区已写入的行（list[str]，超限后被裁剪）。
          - self._tty:         是否为真正终端（isatty），决定走 ANSI 重绘还是纯 print。
          - self._write_lock:  串行化所有终端写操作的线程锁。
          - self._status_lines: 状态区总行数 = 状态槽个数（不含输入行）。
          - self._input_active: 是否正处于「等待用户输入」状态（决定重绘策略）。
        """
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
        """最底层的「往终端写字符串」原始操作。

        说明：
          - 这是所有终端输出的唯一出口，内部加 try/except 兜底（写失败不崩溃）。
          - 调用方（通常是 _full_redraw / _redraw_input_status / 各公开方法）
            必须自行先持有 _write_lock，本函数不负责加锁。
          - 作用：把整段 ANSI 指令 + 文本一次性写入 stream 并 flush，保证一条消息
            不会在屏幕中间被其他线程插入而错位。
        """
        try:
            self.stream.write(s)
            self.stream.flush()
        except Exception:
            pass

    @staticmethod
    def _split_lines(text: str) -> list[str]:
        """把一段正文拆分成「干净的行」列表，供正文滚动区缓存。

        说明：
          - 按 \\n 拆行。
          - 过滤清理：首尾空行被去掉；中间连续空行最多保留一个（避免大片空白
            占据屏幕/内存）。
          - 返回空列表表示「没有可显示的有效内容」，调用方可据此跳过重绘。
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
        # 去掉首尾空行
        while out and out[0] == "":
            out.pop(0)
        while out and out[-1] == "":
            out.pop()
        return out if out else []

    # ------------------------------------------------------------- 正文
    def body_write(self, text: str):
        """向「上方正文滚动区」追加一段文字（线程安全，可被多 Agent/后台线程调用）。

        功能流程：
          1. 用 _split_lines 把传入文本拆成干净的行；若为空则直接返回（无内容不重绘）。
          2. 加写锁后：
             - 非 TTY：直接以普通 print 形式输出（不涉及屏幕布局）。
             - TTY：把新行追加进 self._body_lines，并裁剪到最近 max_body_lines 行；
               然后调用 _full_redraw 整屏重绘（正文 / 状态 / 输入行全部刷新）。
          3. 由于正文变了，屏幕结构也随之改变，因此这里统一走「整屏重绘」路径，
             即使在输入等待中也不例外（_input_active 会被重置以便走全量重绘）。
        """
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
        """更新底部「状态区」中某一槽位的最新一行文本（线程安全，后台线程可调用）。

        区别于 body_write：状态信息（如监听/调试/状态）每次只更新「最新一行」，
        不会像正文那样无限累积滚动，因此每种状态只占屏幕固定一行。

        流程：
          1. 如果 key 不在预定义的 slots 里则忽略（未知槽位不显示）。
          2. 加写锁，更新 self._slot_text[key]。
          3. 非 TTY：直接以 [key] text 形式 print 一行。
          4. TTY：
             - 若正在等待用户输入（_input_active）：只做「轻量重画状态区」——
               用 _redraw_input_status 只刷新底部状态几行、光标回落输入行，
               避免清掉用户正在输入的内容。
             - 否则：正文/状态/输入全部变了，走 _full_redraw 整屏重绘。
        """
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
        """整屏重绘（私有，调用方必须已持 _write_lock）。

        策略（TTY 模式的主重绘路径）——把所有内容一次性重建：
          1. 发出 ANSI「光标回家 + 清屏」（\\x1b[H + \\x1b[2J）——整个屏幕清空。
          2. 依次写出当前缓存的所有正文行 self._body_lines（上方滚动区）。
          3. 依次写出每个状态槽一行：「{key}: {text}」（底部状态区，固定 M 行）。
          4. 最后写输入提示行（\\r + 清行 + prompt），光标自然落在输入行。
        完成后置 self._input_active = True，表示当前光标处于「等待输入」状态，
        以便后续 status_set 走轻量重画而不是再清屏（避免打断用户输入）。
        """
        out = _CSI_HOME + _CSI_CLEAR
        for ln in self._body_lines:
            out += ln + "\n"
        for key in self.slots:
            out += _CSI_CLR + f"{key}: {self._slot_text[key]}\n"
        out += "\r" + _CSI_CLR + self.prompt
        self._raw(out)
        self._input_active = True

    def _redraw_input_status(self):
        """输入等待中的「轻量重绘」：只刷新底部状态区，正文与输入行保持原位。

        为什么需要它：
          - 整屏重绘（_full_redraw）会「清屏」，会清掉用户正在输入行里键入的字符，
            所以在等待用户输入期间，正文/输入发生状态更新时不能整屏清屏。
          - 因此只把光标上移到状态区顶部，逐行清行并重写每个状态槽，最后光标回落
            到输入行——用户输入内容原样保留。

        步骤：
          1. 记录当前光标在输入行，先向上移动 status_lines 行到状态区首行。
          2. 对每个状态槽：回到行首 + 清行 + 写「{key}: {text}」。
          3. 重画完状态区后把光标重新定位回输入行（保持用户输入不被影响）。
        """
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
        """显示输入提示，并异步读取用户的一行输入（等待期间不长期持锁）。

        这是「对话回合」的输入侧接口，与 io_dialog 配合使用：
          - 在底部输入行绘制提示符 p（缺省用构造时的 self.prompt），并整屏重绘一次。
          - 然后通过 asyncio.to_thread(input, "") 把阻塞的 input 放到线程池，等待期间
            不占用事件循环、也不持有 _write_lock，因此后台 listener 线程仍可随时
            status_set 刷新状态区（走轻量重绘）。
          - 非 TTY：直接写提示符 + 用普通 input 读取（无 ANSI 绘制）。
        返回用户去掉首尾空白后的输入字符串。
        """
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
        """启动时展示的横幅/欢迎信息。

        本质上是 body_write 的一个便捷别名：把一段欢迎文字写入正文滚动区，
        立即触发整屏重绘，让用户一进入程序就看到排版好的界面。
        """
        self.body_write(text)

    def shutdown(self):
        """程序退出前的收尾：把终端恢复成「普通滚动」状态，避免留下 ANSI 残影。

        原因：运行时通过持续整屏重绘维持「固定布局」，退出时若直接结束，终端会
        停留在最后一块清屏后的画面，看不见之前的正文。
        做法：
          1. 清屏（光标回家 + 清空屏幕）。
          2. 把之前缓存的全部正文行以「普通输出」方式重新打印（可正常滚动）。
          3. 置 _input_active = False，结束对终端的抢占式布局控制。
        """
        with self._write_lock:
            if not self._tty:
                return
            body = "\n".join(self._body_lines)
            self._raw(_CSI_HOME + _CSI_CLEAR)
            if body:
                self._raw(body + "\n")
            self._input_active = False


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
    一个可用的默认渲染器（默认状态槽 listener/debug/state、500 行正文缓存）。
    """
    global _renderer
    if _renderer is None:
        _renderer = TerminalRenderer()
    return _renderer
