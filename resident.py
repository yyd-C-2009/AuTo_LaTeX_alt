"""resident.py：常驻 Agent 运行时与 Super/终端集成。

支持：
    - 定时唤醒 + Listener 事件唤醒（Phase 1 + Phase 2）
    - 常驻 Agent 消息自动截断（保留 system + 最近 N 条）
    - Super 工具 start_resident_agent / stop_resident_agent / resident_status（Phase 3）
"""

import asyncio
from typing import Annotated

from Agent import Agent


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
        await self.bus.io_print(f"[resident:{self.name}] 已启动，interval={self.interval_sec:.0f}s")
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
            await self.bus.io_print(f"[resident:{self.name}] 已退出循环")

    async def _tick(self):
        trigger = self._build_trigger()
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
        except Exception as e:
            await self.bus.io_print(f"[resident:{self.name}] 本轮异常：{type(e).__name__}: {e}")
        finally:
            self._trim_messages()

    def _build_trigger(self) -> str:
        if self.transcript_provider is not None:
            try:
                cursor = int(self.transcript_provider.get_listen_cursor())
                new_text = self.transcript_provider.get_listen_result(
                    include_timestamps=False, since_index=self.last_cursor
                )
                self.last_cursor = cursor
                if new_text and not new_text.startswith("（暂无") and not new_text.startswith("（无新增"):
                    return (
                        "【Listener 新增转写】\n"
                        f"{new_text}\n\n"
                        "请把以上新增内容整理进课堂笔记，更新 latex_output/notes.tex，"
                        "排版遵守 view_theorem_style，最后调用 check_latex 检查。"
                    )
                return "【定时唤醒】暂无新增转写。可检查笔记文件是否需要继续完善。"
            except Exception as e:
                return f"【定时唤醒】读取 Listener 转写失败：{type(e).__name__}: {e}"
        return f"【定时唤醒】请继续处理你的常驻任务（{self.agent_type}）。"

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

    def __init__(self, bus, client, model: str, agent_specs: dict, transcript_provider=None):
        self.bus = bus
        self.client = client
        self.model = model
        self.agent_specs = agent_specs
        self.transcript_provider = transcript_provider
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
        )
        ra.task = asyncio.create_task(ra.run())  # 必须在事件循环内创建
        self.residents[agent_type] = ra
        return f"常驻 Agent {agent_type!r} 已启动（interval={ra.interval_sec:.0f}s）。"

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
