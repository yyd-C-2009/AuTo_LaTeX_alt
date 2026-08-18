"""供 Agent 使用的文件编辑工具（str_replace_editor）。

提供四种无歧义、可验证的文件操作：
    view         : 查看文件内容（可选行号区间），带行号
    create       : 创建新文件（已存在则报错，避免误覆盖）
    str_replace  : 精确字符串替换（old_str 必须唯一匹配，否则报错）
    insert       : 在指定行号之后插入内容

设计约定（与项目一致）：
    - 所有操作返回 dict，含 'Success' 字段（'True'/'False'）
    - 路径限制在项目工作目录内，杜绝 Agent 通过 ../ 越权读写任意文件
    - 内部函数失败时抛 ValueError，由外层统一包装为 Success='False' + Error 描述
    - 全部为同步阻塞函数，交由 Tools.async_execute 的 to_thread 分支调度

用法：在 super.py 中 add_tool 注册（也可注册到其他 Agent 的工具子集）。
"""

import os
from typing import Annotated


# 默认工作根目录：本项目目录（可用环境变量覆盖，便于测试）
_WORKDIR = os.path.abspath(os.path.dirname(__file__))


def _resolve(path: str) -> str:
    """把用户路径解析为绝对路径，并校验不越出工作根目录。返回绝对路径。"""
    if not path:
        raise ValueError("path 不能为空")
    base = os.path.abspath(path)
    # 越权检查：规范化后必须仍在 _WORKDIR 之下（或等于 _WORKDIR 自身）
    try:
        common = os.path.commonpath([_WORKDIR, base])
    except ValueError:
        raise ValueError(f"非法路径: {path}")
    if common != _WORKDIR:
        raise ValueError(
            f"路径越权：只允许访问工作目录 {_WORKDIR} 内的文件，收到 {path}"
        )
    return base


def _view(path: str, view_range: list[int] | None = None) -> str:
    abs_path = _resolve(path)
    if not os.path.exists(abs_path):
        raise ValueError(f"文件不存在 {path}")
    if os.path.isdir(abs_path):
        # 目录：列出（最多 2 层）非隐藏条目
        entries = []
        for root, dirs, files in os.walk(abs_path):
            depth = root[len(abs_path):].count(os.sep)
            if depth >= 2:
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                if not f.startswith("."):
                    entries.append(os.path.relpath(os.path.join(root, f), _WORKDIR))
        return "目录内容：\n" + ("\n".join(entries) if entries else "（空）")

    with open(abs_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    total = len(lines)
    start = 1
    end = total
    if view_range:
        if len(view_range) != 2:
            raise ValueError("view_range 必须是 [起始行, 结束行]")
        start, end = view_range[0], view_range[1]
        if start < 1 or end < 1:
            raise ValueError("view_range 行号必须 >= 1")
        if start > end:
            raise ValueError("view_range 起始行不能大于结束行")
        if end > total:
            end = total

    # 带行号输出（与常见 str_replace_editor 保持一致）
    out_lines = []
    for i in range(start - 1, end):
        out_lines.append(f"{i + 1:6d}\t{lines[i].rstrip()}")
    return "\n".join(out_lines)


def _create(path: str, file_text: str) -> str:
    abs_path = _resolve(path)
    if os.path.exists(abs_path):
        raise ValueError("文件已存在，create 不会覆盖；如需修改请用 str_replace 或先删除")
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(file_text)
    return f"文件已创建：{path}"


def _str_replace(path: str, old_str: str, new_str: str) -> str:
    abs_path = _resolve(path)
    if not os.path.isfile(abs_path):
        raise ValueError(f"文件不存在 {path}")
    with open(abs_path, "r", encoding="utf-8") as f:
        content = f.read()
    cnt = content.count(old_str)
    if cnt == 0:
        raise ValueError("old_str 在文件中未找到，请用 view 确认当前内容后重试")
    if cnt > 1:
        raise ValueError(f"old_str 在文件中共出现 {cnt} 次，不唯一；请补充更多上下文使其唯一")
    new_content = content.replace(old_str, new_str, 1)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(new_content)
    return f"替换成功：{path}"


def _insert(path: str, insert_line: int, new_str: str) -> str:
    abs_path = _resolve(path)
    if not os.path.isfile(abs_path):
        raise ValueError(f"文件不存在 {path}")
    with open(abs_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    if insert_line < 0 or insert_line > len(lines):
        raise ValueError(f"insert_line 必须在 0..{len(lines)} 之间（0 表示文件开头之前）")
    lines.insert(insert_line, new_str if new_str.endswith("\n") else new_str + "\n")
    with open(abs_path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    return f"插入成功：{path} 第 {insert_line} 行之后"


def str_replace_editor(
    command: Annotated[str, "操作类型：view / create / str_replace / insert 之一"],
    path: Annotated[str, "要操作的文件路径（相对或绝对，限工作目录内）"],
    file_text: Annotated[str | None, "create 时的完整文件内容"] = None,
    old_str: Annotated[str | None, "str_replace 时要被替换的原字符串（必须唯一匹配）"] = None,
    new_str: Annotated[str | None, "str_replace/insert 时要写入的新字符串"] = None,
    insert_line: Annotated[int | None, "insert 时的行号（新内容插入到该行之后）"] = None,
    view_range: Annotated[list[int] | None, "view 时的行号区间 [起始, 结束]（含端点），省略则查看全文"] = None,
) -> dict:
    '''查看/创建/编辑工作目录内的文本文件：view 查看（带行号）、create 新建、str_replace 精确替换、insert 指定行插入。编辑前请先用 view 确认当前内容。'''
    try:
        if command == "view":
            return {"Success": "True", "result": _view(path, view_range)}
        if command == "create":
            if file_text is None:
                raise ValueError("create 需要提供 file_text")
            return {"Success": "True", "result": _create(path, file_text)}
        if command == "str_replace":
            if old_str is None:
                raise ValueError("str_replace 需要提供 old_str")
            return {"Success": "True", "result": _str_replace(path, old_str, new_str or "")}
        if command == "insert":
            if insert_line is None or new_str is None:
                raise ValueError("insert 需要提供 insert_line 和 new_str")
            return {"Success": "True", "result": _insert(path, insert_line, new_str)}
        raise ValueError(f"未知的 command: {command}（可选 view/create/str_replace/insert）")
    except Exception as e:
        return {"Success": "False", "Error": f"{type(e).__name__}: {e}"}


if __name__ == "__main__":
    # 简易自测（不依赖项目其他模块）
    p = os.path.join(_WORKDIR, "_se_test.txt")
    print(str_replace_editor("create", p, file_text="line1\nline2\nline3\n"))
    print(str_replace_editor("view", p))
    print(str_replace_editor("str_replace", p, old_str="line2", new_str="line2_changed"))
    print(str_replace_editor("view", p, view_range=[1, 2]))
    print(str_replace_editor("insert", p, insert_line=1, new_str="line1.5\n"))
    print(str_replace_editor("view", p))
    # 失败路径演示
    print(str_replace_editor("view", os.path.join(_WORKDIR, "nonexist.tex")))
    print(str_replace_editor("view", os.path.join(_WORKDIR, "..", "secret.txt")))
    os.remove(p)
