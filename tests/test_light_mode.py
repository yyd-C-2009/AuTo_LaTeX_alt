"""light 模式离线自检：不得导入重模型模块，也不得注册对应工具。"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from Tools import Tools
from event_bus import Bus
from initer import init
from runtime.gateway import CapabilityGateway
from tools_registry import build_super_tools, register_common_tools
from experts import experts_for_tools


HEAVY_MODULES = (
    "Saver",
    "Visal",
    "listener",
    "sentence_transformers",
    "chromadb",
    "pix2text",
    "faster_whisper",
)


def test_light_init_skips_heavy_modules():
    before = {name for name in HEAVY_MODULES if name in sys.modules}
    data = asyncio.run(init(light=True))
    after = {name for name in HEAVY_MODULES if name in sys.modules}

    assert after == before, f"light 初始化导入了重模块：{sorted(after - before)}"
    assert data["light"] is True
    assert data["agent_memory"] is None
    assert data["agent_visal"] is None
    assert data["agent_listener"] is None
    assert data["agent_plan"] is not None

    tools = Tools()
    register_common_tools(tools, data)
    names = set(tools.tool_list)
    assert not names.intersection({
        "add_memory", "retrieve_context", "delete_memory", "replace_memory",
        "recognize_doc", "transcribe_audio", "start_listening",
        "stop_listening", "get_listen_result", "get_listen_cursor",
        "clear_listen_result",
    })
    assert {"write_latex", "check_latex", "view_plan", "run_python"}.issubset(names)

    specs = experts_for_tools(names, light=True)
    assert "listener" not in specs
    assert "notetaker" not in specs
    assert "math" in specs and "recognize_doc" not in specs["math"][1]
    assert "run_python" in specs["math"][1]

    bus = Bus(tools)
    gateway = CapabilityGateway()
    for name in tools.tool_list:
        gateway.register_tool(name, schema=tools.dict_schema[name])
    build_super_tools(bus, object(), [], gateway, expert_specs=specs)
    registered = set(tools.tool_list)
    assert "listener_expert" not in registered
    assert "notetaker_expert" not in registered
    assert {"math_expert", "mathwrite_expert", "passagewrite_expert", "draw_expert"}.issubset(registered)


if __name__ == "__main__":
    test_light_init_skips_heavy_modules()
    print("light mode self-check OK")
