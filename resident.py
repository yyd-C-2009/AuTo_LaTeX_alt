"""resident.py：常驻 Agent 运行时与 Super/终端集成。

支持：
    - 定时唤醒 + Listener 事件唤醒（Phase 1 + Phase 2）
    - 常驻 Agent 消息自动截断（保留 system + 最近 N 条）
    - Super 工具 start_resident_agent / stop_resident_agent / resident_status（Phase 3）
    - Phase 5：Notetaker 以显式 Workflow 驱动（LectureNoteTaking），
      Listener 游标只在 check_latex 验收通过后才推进。
"""

import asyncio
from typing import Annotated

from Agent import Agent
from runtime.context import TaskContext
from runtime.gateway import CapabilityGateway, LegacyPolicyAdapter
from runtime.task import ArtifactRef, new_task
from runtime.workflow import Stage, StageAbort, Workflow

# 笔记产物（notetaker 的唯一交付物）。check_latex 以 latex_output/ 为工作目录，
# 因此这里区分「bus 工具参数用的文件名」与「文档/日志里展示的相对路径」。
NOTES_FILENAME = "notes.tex"
NOTES_RELPATH = "latex_output/notes.tex"

# check_latex 成功时的固定前缀（见 super.py:check_latex 的返回契约）。
_LATEX_OK_MARK = "语法检查通过"


def build_lecture_note_workflow() -> Workflow:
    """Notetaker 的 LectureNoteTaking 工作流定义（TASK.md Phase 5 / §14）。

    阶段语义：
      observe       —— 读取 Listener 增量转写（host 侧执行，不消耗 LLM）
      write_notes   —— Agent 起草/更新笔记，并在阶段内自查 check_latex
      verify_latex  —— host 侧独立复跑 check_latex 做验收（不信任 LLM 自述）
      commit_cursor —— 纯控制阶段：验收通过才推进游标，否则不提交

    注意：observe 由 host 直接完成，understand 与 write 合并在同一次 LLM 调用里
    （阶段越细 LLM 调用次数越多，而这两步在提示词层面本就不可分割），
    因此每轮唤醒只产生 1 次 LLM 调用，与引入工作流前的开销一致。
    """
    return Workflow(
        name="LectureNoteTaking",
        stages=[
            Stage("observe", tools=("get_listen_result", "get_listen_cursor")),
            Stage(
                "write_notes",
                tools=(
                    "write_latex",
                    "str_replace_editor",
                    "check_latex",
                    "view_theorem_style",
                    "retrieve_context",
                ),
            ),
            Stage("verify_latex", tools=("check_latex",)),
            Stage("commit_cursor"),  # 不授权任何工具：纯提交/中止决策
        ],
    )


class ResidentAgent:
    """常驻 Agent：持有同一个 Agent 实例，循环等待唤醒并处理任务。"""

    def __init__(
        self,
        name: str,
        agent_type: str,
        bus,
        client,
        model: str,
        prompt: str,
        tool_names: list[str] | None,
        interval_sec: float = 30.0,
        deny_tools: list[str] | None = None,
        transcript_provider=None,
        max_messages: int = 40,
        gateway: CapabilityGateway | None = None,
        workflow: Workflow | None = None,
        principal_id: str | None = None,
    ):
        self.name = name
        self.agent_type = agent_type
        self.bus = bus
        self.client = client
        self.model = model
        self.prompt = prompt
        self.tool_names = tool_names
        self.interval_sec = max(1.0, float(interval_sec))
        self.deny_tools = deny_tools
        self.transcript_provider = transcript_provider
        self.max_messages = max_messages
        self.gateway = gateway
        self.workflow = workflow
        self.principal_id = principal_id or agent_type
        self.messages = [{"role": "system", "content": prompt}]
        self.agent = Agent(bus)
        self.stop_event = asyncio.Event()
        self.wake_event = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.last_cursor = 0

    async def _on_listener_event(self, content: dict | None = None):
        # Listener 后台线程通过 bus.publish 调用，常驻 Agent 立即唤醒（Phase 2 事件驱动）
        self.wake_event.set()

    async def run(self):
        self.bus.on("listener.transcript_updated", self._on_listener_event)
        self.bus.io_status("state", f"[resident:{self.name}] 已启动 (interval={self.interval_sec:.0f}s)")
        try:
            while not self.stop_event.is_set():
                try:
                    await asyncio.wait_for(self.wake_event.wait(), timeout=self.interval_sec)
                except asyncio.TimeoutError:
                    pass  # 定时唤醒
                if self.stop_event.is_set():
                    break
                self.wake_event.clear()
                await self._tick()
        finally:
            self.bus.off("listener.transcript_updated", self._on_listener_event)
            self.bus.io_status("state", f"[resident:{self.name}] 已退出")

    async def _tick(self):
        """一次唤醒的处理。

        Phase 5：有 workflow + gateway 时走 LectureNoteTaking 工作流
        （验收通过才推进 Listener 游标）；否则退回原有的直接 run_agent 路径。
        """
        if self.workflow is not None and self.gateway is not None:
            await self._tick_workflow()
            return

        if self.workflow is not None and self.gateway is None:
            # 配了 workflow 却没有 gateway：工作流无法派生阶段护照，只能退回
            # legacy 路径（游标将失去「验收后才提交」的保证）。显式告警而非静默降级。
            self.bus.io_status(
                "state",
                f"[resident:{self.name}] 配置警告：已设置 workflow 但缺少 gateway，"
                "本轮退回 legacy 路径（游标不再受 check_latex 验收保护）",
            )

        trigger, commit_cursor = self._build_trigger()
        self.messages.append({"role": "user", "content": trigger})
        try:
            out = await self.agent.run_agent(
                self.client,
                self.messages,
                self.model,
                tool_names=self.tool_names,
                deny_tools=self.deny_tools,
            )
            if out:
                await self.bus.io_print(f"[resident:{self.name}] {out}")
            if commit_cursor is not None:
                # 无 workflow 时没有独立的验收阶段，只能以「LLM 正常返回」作为
                # 提交依据；这是 legacy 路径的已知弱化点，Phase 5 的 workflow
                # 路径用 check_latex 验收取代它。
                self.last_cursor = commit_cursor
        except Exception as e:
            self.bus.io_status("state", f"[resident:{self.name}] 本轮异常：{type(e).__name__}: {e}")
        finally:
            self._trim_messages()

    # ------------------------------------------------------------------
    # Phase 5: LectureNoteTaking 工作流
    # ------------------------------------------------------------------
    async def _tick_workflow(self):
        """按 LectureNoteTaking 工作流处理一次唤醒。

        关键不变量：self.last_cursor 只在 commit_cursor 阶段（验收通过）推进。
        任一前置阶段失败 → StageAbort → 游标保持不动 → 下次唤醒重放同一段转写。
        """
        # ① observe（host 侧）：读增量 + 记录「本轮要提交到的游标」
        try:
            new_text, current_cursor = self._observe_transcript()
        except Exception as e:
            self.bus.io_status("state", f"[resident:{self.name}] 读取 Listener 转写失败：{type(e).__name__}: {e}")
            return

        if not new_text:
            # 无新增转写：不建任务、不推进游标
            self.bus.io_status("state", f"[resident:{self.name}] 暂无新增转写")
            return

        task = new_task(f"整理课堂笔记 → {NOTES_RELPATH}", holder=self.principal_id)
        ctx = TaskContext(
            task_id=task.id,
            goal=f"把 Listener 新增转写整理进 {NOTES_RELPATH}，并保证 LaTeX 语法检查通过",
            holder=self.principal_id,
            artifacts=[ArtifactRef(path=NOTES_RELPATH, kind="latex")],
        )
        parent = LegacyPolicyAdapter(self.gateway).to_passport(
            self.principal_id, tool_names=self.tool_names
        )

        async def _run_stage(stage, passport, task_obj, prev):
            # 每阶段经 gateway 授权执行（真正的执行边界是 gateway.execute）
            if stage.name == "observe":
                return new_text

            if stage.name == "write_notes":
                self.messages.append(
                    {"role": "user", "content": self._compose_write_prompt(new_text, ctx)}
                )
                try:
                    out = await self.agent.run_agent(
                        self.client, self.messages, self.model,
                        gateway=self.gateway, passport=passport,
                    )
                finally:
                    self._trim_messages()
                if out:
                    await self.bus.io_print(f"[resident:{self.name}] {out}")
                return out or ""

            if stage.name == "verify_latex":
                # 独立复跑 check_latex（不信任 LLM 自述），未通过则中止工作流
                result = await self.gateway.execute(
                    passport, self.bus.submit, "check_latex", filename=NOTES_FILENAME
                )
                text = str(result.content)
                if result.title != "Done" or _LATEX_OK_MARK not in text:
                    self.bus.io_status("state", f"[resident:{self.name}] check_latex 未通过，游标不推进")
                    raise StageAbort("verify_latex", f"check_latex 未通过：{text[:200]}")
                return text

            if stage.name == "commit_cursor":
                # 纯控制阶段：到此说明验收已通过，提交游标
                self.last_cursor = current_cursor
                self.bus.io_status(
                    "state",
                    f"[resident:{self.name}] 笔记已更新并验收通过，游标提交至 {current_cursor}",
                )
                return f"committed cursor={current_cursor}"

            return ""

        try:
            await self.workflow.run(self.gateway, parent, task, runner=_run_stage)
        except StageAbort as e:
            self.bus.io_status(
                "state",
                f"[resident:{self.name}] 本轮中止（游标保持 {self.last_cursor}）：{e.reason}",
            )
        except Exception as e:
            self.bus.io_status("state", f"[resident:{self.name}] 本轮异常：{type(e).__name__}: {e}")

    def _observe_transcript(self) -> tuple[str, int]:
        """读取 Listener 增量转写。返回 (new_text, current_cursor)；
        无新增时 new_text 为空字符串。游标不在此推进（由 commit 阶段提交）。"""
        provider = self.transcript_provider
        if provider is None:
            return "", self.last_cursor
        current_cursor = int(provider.get_listen_cursor())
        new_text = provider.get_listen_result(
            include_timestamps=False, since_index=self.last_cursor
        )
        if not new_text or new_text.startswith("（暂无") or new_text.startswith("（无新增"):
            return "", current_cursor
        return new_text, current_cursor

    def _compose_write_prompt(self, new_text: str, ctx: TaskContext) -> str:
        return (
            "【Listener 新增转写】\n"
            f"{new_text}\n\n"
            f"{ctx.brief()}\n\n"
            f"请把以上新增内容整理进课堂笔记，更新 {NOTES_RELPATH}，"
            "排版遵守 view_theorem_style，最后调用 check_latex 检查直到通过。"
        )

    def _build_trigger(self) -> tuple[str, int | None]:
        """legacy（无 workflow）路径的触发文本构造。"""
        if self.transcript_provider is not None:
            try:
                new_text, current_cursor = self._observe_transcript()
                if new_text:
                    return (
                        (
                            "【Listener 新增转写】\n"
                            f"{new_text}\n\n"
                            f"请把以上新增内容整理进课堂笔记，更新 {NOTES_RELPATH}，"
                            "排版遵守 view_theorem_style，最后调用 check_latex 检查。"
                        ),
                        current_cursor,
                    )
                return "【定时唤醒】暂无新增转写。可检查笔记文件是否需要继续完善。", None
            except Exception as e:
                return f"【定时唤醒】读取 Listener 转写失败：{type(e).__name__}: {e}", None
        return f"【定时唤醒】请继续处理你的常驻任务（{self.agent_type}）。", None

    def _trim_messages(self):
        # 与 Listener 自动截断同理：只保留 system + 最近 (max_messages-1) 条。
        if len(self.messages) > self.max_messages:
            self.messages = [self.messages[0]] + self.messages[-(self.max_messages - 1):]

    def poke(self, text: str):
        self.messages.append({"role": "user", "content": text})
        self.wake_event.set()

    async def stop(self):
        self.stop_event.set()
        self.wake_event.set()
        if self.task is not None:
            try:
                await self.task
            except Exception as e:
                print(f"[resident:{self.name}] 停止时异常：{type(e).__name__}: {e}")


class ResidentManager:
    """常驻 Agent 管理器：创建/停止/查看常驻 Agent。"""

    def __init__(
        self,
        bus,
        client,
        model: str,
        agent_specs: dict,
        transcript_provider=None,
        gateway: CapabilityGateway | None = None,
        workflows: dict | None = None,
    ):
        self.bus = bus
        self.client = client
        self.model = model
        self.agent_specs = agent_specs
        self.transcript_provider = transcript_provider
        self.gateway = gateway
        # agent_type -> Workflow；未登记的类型走 legacy 直接对话路径
        self.workflows = dict(workflows or {})
        self.residents: dict[str, ResidentAgent] = {}

    async def start(self, agent_type: str, interval_sec: float = 30.0) -> str:
        agent_type = agent_type.strip()
        if agent_type not in self.agent_specs:
            return f"启动失败：未知 Agent 类型 {agent_type!r}；可用：{', '.join(self.agent_specs)}"
        if agent_type in self.residents:
            ra = self.residents[agent_type]
            if ra.task is not None and not ra.task.done():
                return f"常驻 Agent {agent_type!r} 已在运行。"
        prompt, tool_names = self.agent_specs[agent_type]
        ra = ResidentAgent(
            name=agent_type,
            agent_type=agent_type,
            bus=self.bus,
            client=self.client,
            model=self.model,
            prompt=prompt,
            tool_names=tool_names,
            interval_sec=interval_sec,
            transcript_provider=self.transcript_provider if agent_type == "notetaker" else None,
            gateway=self.gateway,
            workflow=self.workflows.get(agent_type),
        )
        ra.task = asyncio.create_task(ra.run())  # 必须在事件循环内创建
        self.residents[agent_type] = ra
        started_with = "workflow" if ra.workflow is not None else "legacy"
        return (
            f"常驻 Agent {agent_type!r} 已启动（interval={ra.interval_sec:.0f}s, 模式={started_with}）。"
        )

    async def stop(self, name: str) -> str:
        name = name.strip()
        ra = self.residents.get(name)
        if ra is None:
            return f"停止失败：常驻 Agent {name!r} 不存在。"
        await ra.stop()
        self.residents.pop(name, None)
        return f"常驻 Agent {name!r} 已停止。"

    def list_status(self) -> str:
        if not self.residents:
            return "（无常驻 Agent）"
        lines = []
        for name, ra in self.residents.items():
            running = ra.task is not None and not ra.task.done()
            lines.append(
                f"{name}  agent_type={ra.agent_type}  running={running}  interval={ra.interval_sec:.0f}s"
            )
        return "\n".join(lines)

    def poke(self, name: str, text: str) -> str:
        name = name.strip()
        ra = self.residents.get(name)
        if ra is None:
            return f"唤醒失败：常驻 Agent {name!r} 不存在。"
        if ra.task is None or ra.task.done():
            return f"唤醒失败：常驻 Agent {name!r} 未在运行。"
        ra.poke(text)
        return f"已唤醒常驻 Agent {name!r}。"


def register_resident_tools(tools, manager: ResidentManager) -> None:
    """把常驻 Agent 管理能力注册为工具，供 Super 调用（Phase 3）。"""

    async def start_resident_agent(
        agent_type: Annotated[str, "要启动的常驻 Agent 类型"],
        interval_sec: Annotated[float, "唤醒间隔秒数（默认 30）"] = 30.0,
    ) -> str:
        """启动一个常驻 Agent（如 notetaker 持续笔记）。"""
        return await manager.start(agent_type, interval_sec)

    async def stop_resident_agent(
        name: Annotated[str, "要停止的常驻 Agent 名称"],
    ) -> str:
        """停止一个常驻 Agent。"""
        return await manager.stop(name)

    def resident_status() -> str:
        """查看当前所有常驻 Agent 的运行状态。"""
        return manager.list_status()

    tools.add_tool(start_resident_agent, time_out=10)
    tools.add_tool(stop_resident_agent, time_out=60)
    tools.add_tool(resident_status, time_out=5)
    return None
