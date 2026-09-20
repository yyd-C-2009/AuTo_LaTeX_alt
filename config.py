"""集中读取项目配置；运行设置保存在同目录的 settings.json。"""

import json
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).with_name("settings.json")


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


def get(path: str, default: Any = None) -> Any:
    """以 a.b.c 读取嵌套设置；缺失时返回明确的默认值。"""
    value: Any = SETTINGS
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
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
