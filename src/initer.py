"""创建应用的长生命周期能力对象。

重模型只在标准模式内导入和实例化。这样 ``init(light=True)`` 不仅不会加载
模型权重，也不会因为模块顶层 import 而导入 sentence-transformers、Pix2Text
或 Faster-Whisper 的运行时。
"""

import asyncio


async def init(light: bool = False) -> dict:
    """初始化能力对象；light 模式保留 Plan 与 SQLite 用户偏好。"""
    from plan import Plan
    from config import require
    from persistence import PreferenceStore, TaskStore

    data = {
        "agent_memory": None,
        "agent_visal": None,
        "agent_listener": None,
        "agent_plan": await asyncio.to_thread(Plan),
        "agent_preferences": PreferenceStore(require("runtime.persistence_database")),
        "agent_tasks": TaskStore(require("runtime.persistence_database")),
        "light": bool(light),
    }
    if light:
        return data

    # 延迟导入是轻量模式不触发重型依赖导入的关键；不要移回模块顶层。
    from Saver import Saver
    from Visal import Visal
    from listener import Listener

    data["agent_memory"] = await asyncio.to_thread(Saver)
    data["agent_visal"] = await asyncio.to_thread(Visal)
    data["agent_listener"] = await asyncio.to_thread(Listener)
    return data
