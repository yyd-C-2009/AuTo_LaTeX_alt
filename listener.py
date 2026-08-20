"""listener.py：Listener 音频转写与连续监听（Faster-Whisper 本地模型）。

设计约定：
    - 与 Visal 一致：Listener 是一个长生命周期对象，在 init() 中创建一次，模型加载后常驻内存。
    - 模型默认 Faster-Whisper small（int8, CPU）。可用环境变量覆盖：
        LISTENER_MODEL_SIZE  模型规模（默认 medium）
        LISTENER_MODEL_DIR   本地模型目录（默认 ./models/faster-whisper-medium）
        HF_ENDPOINT          HuggingFace 镜像（默认 https://hf-mirror.com）
    - 连续监听依赖 sounddevice（麦克风），在后台线程中按段转写，转写结果累积在实例中，
      通过 start_listening / stop_listening / get_listen_result 工具访问。
    - 连续监听具备"防落后/追实时"机制：当单次转写耗时较长、待处理音频不断积压时，
      按 LISTENER_MAX_LAG_SEC（默认 20s）限制输出落后上限；超过上限自动丢弃最旧音频，
      只转写最近内容，避免输出落后讲话 1~2 分钟。
      相关环境变量：LISTENER_MAX_LAG_SEC、LISTENER_DROP_OLDEST_ON_LAG。
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
                 device: str = "cpu", compute_type: str = "float32"):
        os.environ.setdefault("HF_ENDPOINT", HF_MIRROR)
        self.model_size = model_size or os.environ.get("LISTENER_MODEL_SIZE", "medium")
        self.model_dir = (
            model_dir
            or os.environ.get("LISTENER_MODEL_DIR")
            or os.path.join(BASE_DIR, "models", f"faster-whisper-{self.model_size}")
        )

        self.model = None
        self.load_error = None
        self.model_path = None
        self.language = os.environ.get("LISTENER_LANGUAGE", "zh")  # 默认中文，避免噪声下语言检测乱跳
        self.initial_prompt = os.environ.get("LISTENER_INITIAL_PROMPT", "")  # 默认不放中文提示词，避免被 Whisper 当作幻觉回显
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
        self._rms_threshold = float(os.environ.get("LISTENER_RMS_THRESHOLD", "0.01"))
        # 连续监听"实时性"控制：
        #   _max_lag_sec   允许输出落后实时音频的最大秒数。超过该值时丢弃最旧的存量音频，
        #                 只转写最近的音频，避免"转写慢 → 队列越积越多 → 落后无上限"。
        #   _drop_oldest_on_lag  True=落后超标时丢弃旧音频强追实时；False=仅限制队列深度（仍会积压）。
        self._max_lag_sec = float(os.environ.get("LISTENER_MAX_LAG_SEC", "20.0"))
        self._drop_oldest_on_lag = os.environ.get("LISTENER_DROP_OLDEST_ON_LAG", "1") not in (
            "", "0", "false", "False", "no",
        )
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
            self.bus.io_status("state", f"Listener 转写事件通知失败: {type(e).__name__}: {e}")

    # ---------- 转写辅助 ----------

    @staticmethod
    def _is_low_quality(seg) -> bool:
        """过滤 Whisper 在噪声/静音下产生的幻觉片段。
        返回 True 表示该片段质量低，应丢弃。
        """
        no_speech_prob = getattr(seg, "no_speech_prob", 0.0)
        avg_logprob = getattr(seg, "avg_logprob", 0.0)
        compression_ratio = getattr(seg, "compression_ratio", 0.0)
        # no_speech_prob 越高越可能是非语音；avg_logprob 越低越不可信；
        # compression_ratio 过高通常表示重复/异常文本。
        if no_speech_prob > 0.4:
            return True
        if avg_logprob < -0.6:
            return True
        if compression_ratio > 2.0:
            return True
        return False

    def _segments_to_lines(self, segments, offset: float = 0.0) -> list[str]:
        """把 Whisper 片段转成带时间戳的文本行。

        offset：录音块的全局起始秒数。Whisper 的 start/end 是块内相对时间，
        叠加 offset 后得到相对整场录音的全局时间，便于后续去重/对齐。
        """
        lines = []
        for seg in segments:
            start = offset + getattr(seg, "start", 0.0)
            end = offset + getattr(seg, "end", 0.0)
            text = (seg.text or "").strip()
            if not text or self._is_low_quality(seg):
                continue
            lines.append(f"[{start:.1f}-{end:.1f}] {text}")
        return lines

    def _segments_to_text(self, segments, info, title: str = "音频") -> str:
        lines = self._segments_to_lines(segments)
        header = (
            f"{title} 转写结果"
            f"（语言: {getattr(info, 'language', '?')}, 时长: {getattr(info, 'duration', 0.0):.1f}s）：\n"
        )
        return header + ("\n".join(lines) if lines else "(未识别到有效语音内容)")

    def _transcribe_array(self, audio, offset: float = 0.0,
                          max_duration: float | None = None,
                          sample_rate: int = 16000) -> list[str]:
        """转写一段 numpy 音频数组，返回带（全局）时间戳的文本行。

        offset：该段音频相对整场录音的全局起始秒数（用于时间戳对齐）。
        max_duration：可选，若大于 0，转写前把音频截断到该秒数（每段预算），
        避免转写超大块导致单次推理时间过长、落后越积越多。
        sample_rate：连续监听的采样率（用于 max_duration 换算）。
        """
        import numpy as np

        arr = np.asarray(audio, dtype=np.float32).squeeze()
        if arr.ndim == 2:
            arr = arr.mean(axis=1)
        if arr.size == 0:
            return []

        if max_duration and max_duration > 0 and arr.size > int(max_duration * sample_rate):
            # 只取最近 max_duration 秒，offset 相应前移，保证转写的是"最新"内容（追实时）。
            keep = int(max_duration * sample_rate)
            orig_size = arr.size
            arr = arr[-keep:]
            offset = max(0.0, offset + (orig_size - keep) / sample_rate)

        try:
            with self._model_lock:
                segments, info = self.model.transcribe(
                    arr,
                    language=self.language,
                    task="transcribe",
                    initial_prompt=self.initial_prompt,
                    condition_on_previous_text=False,
                    vad_filter=True,
                    vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 200},
                    no_speech_threshold=0.4,
                    logprob_threshold=-0.6,
                    compression_ratio_threshold=2.0,
                    beam_size=5,
                    temperature=0.0,  # 避免随机性，保证同一音频每次转写结果一致
                )
        except TypeError:
            # 旧版 faster-whisper 可能不支持 vad_parameters，降级重试
            try:
                with self._model_lock:
                    segments, info = self.model.transcribe(
                        arr,
                        language=self.language,
                        task="transcribe",
                        initial_prompt=self.initial_prompt,
                        condition_on_previous_text=False,
                        vad_filter=True,
                        beam_size=5,
                    )
            except TypeError:
                with self._model_lock:
                    segments, info = self.model.transcribe(
                        arr,
                        language=self.language,
                        task="transcribe",
                        condition_on_previous_text=False,
                        beam_size=5,
                    )
        return self._segments_to_lines(segments, offset)

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

        # auto 时也使用实例默认语言（默认 zh），避免噪声下自动检测乱跳
        lang = self.language if language in ("auto", "") else language
        use_task = task if task in ("transcribe", "translate") else "transcribe"

        try:
            with self._model_lock:
                segments, info = self.model.transcribe(
                    audio_path,
                    language=lang,
                    task=use_task,
                    initial_prompt=self.initial_prompt,
                    condition_on_previous_text=False,
                    vad_filter=True,
                    vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 200},
                    no_speech_threshold=0.4,
                    logprob_threshold=-0.6,
                    compression_ratio_threshold=2.0,
                    beam_size=5,
                    temperature=0.0,  # 避免随机性，保证同一音频每次转写结果一致
                )
        except TypeError:
            # 旧版 faster-whisper 可能不支持 vad_parameters，降级重试
            try:
                with self._model_lock:
                    segments, info = self.model.transcribe(
                        audio_path,
                        language=lang,
                        task=use_task,
                        initial_prompt=self.initial_prompt,
                        condition_on_previous_text=False,
                        vad_filter=True,
                        beam_size=5,
                    )
            except TypeError:
                with self._model_lock:
                    segments, info = self.model.transcribe(
                        audio_path,
                        language=lang,
                        task=use_task,
                        condition_on_previous_text=False,
                        beam_size=5,
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

        # 每个录音块在全局录音中的起始秒数（即从监听开始累计的偏移）。
        # 严格等于：已消费并转写(或丢弃)的音频秒数。
        offset_sec = 0.0
        # 待转写的音频块缓冲；放在 try 外初始化，确保 finally 里始终可用。
        pending: list = []

        def _drain_queue() -> list:
            """取走队列中当前全部录音块，返回 (blocks, 块总秒数)。"""
            blocks = []
            total = 0.0
            while True:
                try:
                    block = self._listen_queue.get_nowait()
                except queue.Empty:
                    break
                blocks.append(block)
                total += len(block) / sample_rate
            return blocks, total

        def _trim_front(blocks: list, drop_sec: float) -> tuple[list, float]:
            """从最旧处丢弃 drop_sec 秒，返回 (剩余blocks, 实际丢弃秒数)。"""
            dropped = 0.0
            i = 0
            while i < len(blocks) and dropped + len(blocks[i]) / sample_rate <= drop_sec + 1e-9:
                dropped += len(blocks[i]) / sample_rate
                i += 1
            if i:
                blocks = blocks[i:]
            return blocks, dropped

        try:
            with sd.InputStream(
                samplerate=sample_rate,
                channels=1,
                dtype="float32",
                callback=self._audio_callback,
            ):
                while not self._listen_stop.is_set():
                    new_blocks, new_sec = _drain_queue()
                    if new_blocks:
                        pending.extend(new_blocks)
                        # 落后（即累计待处理音频）超过预算时，丢弃最旧的音频以追实时，
                        # 避免"转写慢 → 待处理越积越多 → 落后无上限（1~2 分钟）"。
                        if self._drop_oldest_on_lag and self._max_lag_sec > 0:
                            pending_sec = sum(len(b) / sample_rate for b in pending)
                            over = pending_sec - (segment_duration + self._max_lag_sec)
                            if over > 0:
                                pending, dropped = _trim_front(pending, over)
                                offset_sec += dropped

                    if not pending:
                        # 没有足够的新音频，稍等再取。
                        self._listen_stop.wait(timeout=0.2)
                        continue

                    # 从最旧处凑满一个 segment_duration 的块来转写。
                    take_sec = 0.0
                    take_idx = 0
                    while take_idx < len(pending) and take_sec < segment_duration:
                        take_sec += len(pending[take_idx]) / sample_rate
                        take_idx += 1
                    if take_sec < min(segment_duration, 0.5):
                        # 音频不足一句，继续攒。
                        self._listen_stop.wait(timeout=0.2)
                        continue

                    chunk = pending[:take_idx]
                    pending = pending[take_idx:]
                    block_offset = offset_sec
                    offset_sec += take_sec

                    audio = np.concatenate(chunk)
                    arr = np.asarray(audio, dtype=np.float32).squeeze()
                    if arr.ndim == 2:
                        arr = arr.mean(axis=1)
                    # 能量过低视为静音/噪声，直接跳过，避免 Whisper 对无语音段产生幻觉
                    if arr.size == 0 or float(np.sqrt(np.mean(arr ** 2))) < self._rms_threshold:
                        continue
                    # max_duration 作为单次转写的强约束：确保一次推理不会因为音频过长
                    # 而耗时远超实时，从而把落后控制在 _max_lag_sec 内。
                    lines = self._transcribe_array(
                        audio, offset=block_offset,
                        max_duration=segment_duration, sample_rate=sample_rate,
                    )
                    self._append_transcript_lines(lines)
        except Exception as e:
            self._append_transcript_lines([f"[连续监听异常] {type(e).__name__}: {e}"])
        finally:
            # 收尾：把 pending 里剩余音频（若有）转写完。
            if pending:
                try:
                    import numpy as np

                    audio = np.concatenate(pending)
                    lines = self._transcribe_array(audio, offset=offset_sec)
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
            # 转写结果只更新到终端底部状态区「listener」槽（每类仅占一行），
            # 不再用 print 刷屏挤占正文；无渲染器时退化为普通 print。
            if self.bus is not None:
                self.bus.io_status("listener", line[:120])
            else:
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
        drop_hint = "超出自动丢弃旧音频以追实时" if self._drop_oldest_on_lag else "仅限制处理量"
        return (
            f"连续监听已启动：每 {segment_duration:.1f}s 转写一段，"
            f"采样率 {sample_rate}Hz；落后上限 {self._max_lag_sec:.0f}s（{drop_hint}）。"
            f"可用 get_listen_result 查看，stop_listening 停止。"
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
