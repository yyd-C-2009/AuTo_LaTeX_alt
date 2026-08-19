"""listener.py：Listener 音频转写与连续监听（Faster-Whisper 本地模型）。

设计约定：
    - 与 Visal 一致：Listener 是一个长生命周期对象，在 init() 中创建一次，模型加载后常驻内存。
    - 模型默认 Faster-Whisper small（int8, CPU）。可用环境变量覆盖：
        LISTENER_MODEL_SIZE  模型规模（默认 small）
        LISTENER_MODEL_DIR   本地模型目录（默认 ./models/faster-whisper-small）
        HF_ENDPOINT          HuggingFace 镜像（默认 https://hf-mirror.com）
    - 连续监听依赖 sounddevice（麦克风），在后台线程中按段转写，转写结果累积在实例中，
      通过 start_listening / stop_listening / get_listen_result 工具访问。
"""

import asyncio
import os
import queue
import threading
from typing import Annotated

HF_MIRROR = "https://hf-mirror.com"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class Listener:
    """长生命周期监听器：持有 Faster-Whisper 模型，提供单文件转写与连续监听。"""

    def __init__(self, model_size: str | None = None, model_dir: str | None = None,
                 device: str = "cpu", compute_type: str = "int8"):
        os.environ.setdefault("HF_ENDPOINT", HF_MIRROR)
        self.model_size = model_size or os.environ.get("LISTENER_MODEL_SIZE", "small")
        self.model_dir = (
            model_dir
            or os.environ.get("LISTENER_MODEL_DIR")
            or os.path.join(BASE_DIR, "models", f"faster-whisper-{self.model_size}")
        )

        self.model = None
        self.load_error = None
        self.model_path = None
        self._model_lock = threading.Lock()  # 必须在 __init__ 早期初始化，后续转写方法会使用
        try:
            from faster_whisper import WhisperModel

            if os.path.isdir(self.model_dir) and os.listdir(self.model_dir):
                model_path = self.model_dir
                self.model = WhisperModel(model_path, device=device, compute_type=compute_type)
                self.model_path = model_path
                print(f"----------Listener loaded（模型: {model_path}）----------")
            else:
                # 不自动联网下载，避免 HF 网络不通时启动超时；改为明确报错，由 Setup.py 下载模型。
                self.load_error = f"模型目录不存在或为空: {self.model_dir}"
                self.model_path = None
                print(f"Listener 模型未下载：{self.model_dir}")
                print("请运行：python Setup.py --with-listener --listener-source modelscope 下载模型；")
                print("或设置 LISTENER_MODEL_DIR 指向已下载的模型目录后重新启动。")
        except Exception as e:
            self.load_error = f"{type(e).__name__}: {e}"
            self.model_path = None
            print(f"Listener 模型加载失败：{self.load_error}")
            print("请运行：python Setup.py --with-listener --listener-source modelscope 下载模型；")
            print("或设置 LISTENER_MODEL_DIR 指向已下载的模型目录后重新启动。")
        # 连续监听状态
        self._listen_thread: threading.Thread | None = None
        self._listen_stop = threading.Event()
        self._listen_queue: "queue.Queue" = queue.Queue()
        self._listen_transcript: list[dict] = []
        self._listen_seq = 0  # 单调递增的转写条目序号；clear 只清列表不清序号，保证游标不倒退
        self._max_transcript_entries = max(1, int(os.environ.get("LISTENER_MAX_TRANSCRIPT_ENTRIES", "200")))
        self._transcript_lock = threading.Lock()
        self.bus = None
        self.loop = None

    def attach_bus(self, bus, loop=None):
        """绑定 Bus 与事件循环，用于从后台线程向常驻 Agent 发布转写更新事件。"""
        self.bus = bus
        self.loop = loop

    def _notify_transcript(self, count: int):
        if self.bus is None or self.loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.bus.publish("listener.transcript_updated", {"new_lines": count}),
                self.loop,
            )
        except Exception as e:
            print(f"[Listener] 转写事件通知失败：{type(e).__name__}: {e}")

    # ---------- 转写辅助 ----------

    def _segments_to_lines(self, segments) -> list[str]:
        lines = []
        for seg in segments:
            start = getattr(seg, "start", 0.0)
            end = getattr(seg, "end", 0.0)
            text = (seg.text or "").strip()
            if text:
                lines.append(f"[{start:.1f}-{end:.1f}] {text}")
        return lines

    def _segments_to_text(self, segments, info, title: str = "音频") -> str:
        lines = self._segments_to_lines(segments)
        header = (
            f"{title} 转写结果"
            f"（语言: {getattr(info, 'language', '?')}, 时长: {getattr(info, 'duration', 0.0):.1f}s）：\n"
        )
        return header + ("\n".join(lines) if lines else "(未识别到有效语音内容)")

    def _transcribe_array(self, audio) -> list[str]:
        import numpy as np

        arr = np.asarray(audio, dtype=np.float32).squeeze()
        if arr.ndim == 2:
            arr = arr.mean(axis=1)
        if arr.size == 0:
            return ["(空音频)"]

        try:
            with self._model_lock:
                segments, info = self.model.transcribe(
                    arr, language=None, task="transcribe", vad_filter=True, beam_size=5
                )
        except TypeError:
            with self._model_lock:
                segments, info = self.model.transcribe(
                    arr, language=None, task="transcribe", beam_size=5
                )
        return self._segments_to_lines(segments)

    # ---------- 单文件转写工具 ----------

    def transcribe_audio(
        self,
        audio_path: Annotated[str, "音频文件路径（wav/mp3/m4a 等）"],
        language: Annotated[str, "语言：auto=自动检测，zh=中文，en=英文"] = "auto",
        task: Annotated[str, "任务：transcribe=转写原文语言，translate=翻译为英文"] = "transcribe",
    ) -> str:
        """使用常驻 Faster-Whisper 模型把音频文件转写为文字（带时间戳）。"""
        if self.model is None:
            return (
                f"Listener 模型未加载（{self.load_error}）。"
                "请先运行：python Setup.py --with-listener --listener-source modelscope 下载模型；"
                "或设置 LISTENER_MODEL_DIR 指向已下载的模型目录。"
            )
        if not audio_path or not os.path.isfile(audio_path):
            return f"错误: 音频文件不存在 {audio_path}"

        lang = None if language in ("auto", "") else language
        use_task = task if task in ("transcribe", "translate") else "transcribe"

        try:
            with self._model_lock:
                segments, info = self.model.transcribe(
                    audio_path, language=lang, task=use_task, vad_filter=True, beam_size=5
                )
        except TypeError:
            with self._model_lock:
                segments, info = self.model.transcribe(
                    audio_path, language=lang, task=use_task, beam_size=5
                )
        except Exception as e:
            return f"转写失败：{type(e).__name__}: {e}"

        return self._segments_to_text(segments, info, os.path.basename(audio_path))

    # ---------- 连续监听工具 ----------

    def _audio_callback(self, indata, frames, time_info, status):
        self._listen_queue.put(indata.copy())

    def _listen_worker(self, segment_duration: float, sample_rate: int):
        import numpy as np
        import sounddevice as sd

        buffer: list = []
        buffered_sec = 0.0
        try:
            with sd.InputStream(
                samplerate=sample_rate,
                channels=1,
                dtype="float32",
                callback=self._audio_callback,
            ):
                while not self._listen_stop.is_set():
                    try:
                        block = self._listen_queue.get(timeout=1.0)
                    except queue.Empty:
                        continue
                    buffer.append(block)
                    buffered_sec += len(block) / sample_rate
                    if buffered_sec >= segment_duration:
                        audio = np.concatenate(buffer)
                        buffer, buffered_sec = [], 0.0
                        lines = self._transcribe_array(audio)
                        self._append_transcript_lines(lines)
        except Exception as e:
            self._append_transcript_lines([f"[连续监听异常] {type(e).__name__}: {e}"])
        finally:
            if buffer:
                try:
                    import numpy as np

                    audio = np.concatenate(buffer)
                    lines = self._transcribe_array(audio)
                    self._append_transcript_lines(lines)
                except Exception as e:
                    self._append_transcript_lines([f"[收尾转写异常] {type(e).__name__}: {e}"])

    def _append_transcript_lines(self, lines):
        appended = 0
        for line in lines:
            line = (line or "").strip()
            if not line:
                continue
            with self._transcript_lock:
                self._listen_transcript.append({"seq": self._listen_seq, "text": line})
                self._listen_seq += 1
                appended += 1
                # 自动截断：只保留最近 N 条，与常驻消息截断策略一致。
                if len(self._listen_transcript) > self._max_transcript_entries:
                    del self._listen_transcript[: len(self._listen_transcript) - self._max_transcript_entries]
            print(f"[Listener] {line[:120]}")
        if appended:
            self._notify_transcript(appended)

    def _format_transcript(self, include_timestamps: bool, since_index: int = 0) -> str:
        import re

        with self._transcript_lock:
            if not self._listen_transcript:
                return "（暂无连续监听转写内容）"
            oldest_seq = self._listen_transcript[0]["seq"] if self._listen_transcript else 0
            # 若请求的游标早于自动截断后仍保留的最早序号，只返回仍保留的内容，避免重复。
            effective_since = max(since_index, oldest_seq)
            entries = [e for e in self._listen_transcript if e["seq"] >= effective_since]
        if not entries:
            return "（无新增转写）"
        lines = [e["text"] for e in entries]
        if include_timestamps:
            text = "\n".join(lines)
        else:
            text = "\n".join(
                re.sub(r"^\[[0-9.]+-[0-9.]+\]\s*", "", line) for line in lines
            )
        if since_index > 0 and oldest_seq > since_index:
            text = "[提示] 部分早期转写已自动截断，以下为最近保留内容。\n" + text
        return text

    def start_listening(
        self,
        segment_duration: Annotated[float, "每段转写秒数（默认 8.0）"] = 8.0,
        sample_rate: Annotated[int, "采样率（默认 16000）"] = 16000,
    ) -> str:
        """启动麦克风连续监听：后台按段录音并转写，结果累积在内存中。"""
        if self.model is None:
            return (
                f"Listener 模型未加载（{self.load_error}）。"
                "请先运行：python Setup.py --with-listener --listener-source modelscope 下载模型；"
                "或设置 LISTENER_MODEL_DIR 指向已下载的模型目录。"
            )
        if self._listen_thread and self._listen_thread.is_alive():
            return "连续监听已在运行中，请勿重复启动。"

        try:
            import sounddevice  # noqa: F401
        except ImportError as e:
            return f"缺少 sounddevice 依赖，请先运行 python Setup.py 安装：{e}"

        self._listen_stop.clear()
        self._listen_thread = threading.Thread(
            target=self._listen_worker,
            args=(segment_duration, sample_rate),
            name="listener-worker",
            daemon=True,
        )
        self._listen_thread.start()
        return (
            f"连续监听已启动：每 {segment_duration:.1f}s 转写一段，"
            f"采样率 {sample_rate}Hz。可用 get_listen_result 查看，stop_listening 停止。"
        )

    def stop_listening(
        self,
        include_timestamps: Annotated[bool, "True=返回带时间戳文本；False=返回纯文本"] = True,
    ) -> str:
        """停止连续监听，并返回当前累积的转写文本（不清空，可再次 get_listen_result 读取）。"""
        if not (self._listen_thread and self._listen_thread.is_alive()):
            return "连续监听未在运行。"

        self._listen_stop.set()
        self._listen_thread.join(timeout=30)
        if self._listen_thread.is_alive():
            return "连续监听停止超时，请稍后再试。"
        return self._format_transcript(include_timestamps)

    def get_listen_cursor(self) -> str:
        """返回当前转写游标（已产生的转写条目总数，单调递增）。常驻笔记 Agent 用它读取增量。"""
        with self._transcript_lock:
            return str(self._listen_seq)

    def get_listen_result(
        self,
        include_timestamps: Annotated[bool, "True=返回带时间戳文本；False=返回纯文本"] = True,
        since_index: Annotated[int, "只返回序号 >= 该游标的转写；0=返回当前保留的全部"] = 0,
    ) -> str:
        """查看累积的连续监听转写文本（只读，不会取走或清空内容）。支持游标增量读取。"""
        return self._format_transcript(include_timestamps, since_index)

    def clear_listen_result(
        self,
        confirm: Annotated[bool, "必须为 True 才会清空累积的连续监听转写"] = False,
    ) -> str:
        """清空当前累积的连续监听转写文本（需 confirm=True 确认）。游标序号保持不变。"""
        if not confirm:
            return "clear_listen_result 需要 confirm=True 才会清空累积转写。"
        with self._transcript_lock:
            self._listen_transcript = []
        return "已清空连续监听转写（游标序号保持不变）。"


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python listener.py <音频文件> [语言 auto/zh/en] [任务 transcribe/translate]")
    else:
        listener = Listener()
        path = sys.argv[1]
        lang = sys.argv[2] if len(sys.argv) > 2 else "auto"
        task = sys.argv[3] if len(sys.argv) > 3 else "transcribe"
        print(listener.transcribe_audio(path, lang, task))
