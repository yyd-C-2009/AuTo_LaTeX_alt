"""集中读取项目根目录的 settings.json 与 runtime.json。"""

import json
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "settings.json"
RUNTIME_CONFIG_PATH = PROJECT_ROOT / "runtime.json"

# 这些配置项描述的是项目资源，而不是用户通过 /cd 选择的工作目录。
# 统一转为绝对路径，避免切换目录后误建空数据库/缓存或找不到模型。
_PROJECT_PATH_KEYS = {
    "paths.latex_output",
    "paths.tikz_output",
    "paths.ocr_cache",
    "paths.imports",
    "paths.memory_database",
    "paths.embedding_model",
    "listener.model_dir",
    "runtime.persistence_database",
}


def _load() -> dict[str, Any]:
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as fh:
            value = json.load(fh)
    except FileNotFoundError as exc:
        raise RuntimeError(f"缺少配置文件：{CONFIG_PATH}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"配置文件不是合法 JSON：{CONFIG_PATH}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("settings.json 根节点必须是对象")
    return value


SETTINGS = _load()
_settings_path = CONFIG_PATH


def _load_runtime() -> dict[str, Any]:
    global CONFIG_PATH
    original = CONFIG_PATH
    CONFIG_PATH = RUNTIME_CONFIG_PATH
    try:
        return _load()
    finally:
        CONFIG_PATH = original


RUNTIME_SETTINGS = _load_runtime()


def get(path: str, default: Any = None) -> Any:
    """以 a.b.c 读取嵌套设置；缺失时返回明确的默认值。"""
    source_path = path
    value: Any = RUNTIME_SETTINGS if source_path.startswith("runtime.") else SETTINGS
    if source_path.startswith("runtime."):
        path = path.removeprefix("runtime.")
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    if source_path in _PROJECT_PATH_KEYS and isinstance(value, str):
        candidate = Path(value)
        return str(candidate if candidate.is_absolute() else PROJECT_ROOT / candidate)
    return value


def require(path: str) -> Any:
    value = get(path)
    if value is None:
        raise RuntimeError(f"settings.json 缺少必填配置：{path}")
    return value


# 保留旧导入名，避免业务模块散落配置解析逻辑。
MODEL = require("llm.model")
BASE_URL = require("llm.base_url")
KEY_ID = require("llm.api_key_env")
OUT_DIR = require("paths.latex_output")
TIKZ_DIR = require("paths.tikz_output")
