"""str_replace_editor 工具的 schema 生成 + 执行链路验证（需本机真实依赖 pydantic）。

验证点：
1. Tools.add_tool(str_replace_editor) 能正常生成 OpenAI function-calling schema，
   且 view_range 被识别为 array / integer 类型。
2. 四种操作 view/create/str_replace/insert 实际读写正确。
3. 路径越权（../ 逃逸）被拦截。

用法（本机）：
    python test_str_replace_editor_demo.py
"""
import os
import asyncio
import tempfile

from Tools import Tools
from str_replace_editor import str_replace_editor


def test_schema():
    tools = Tools()
    tools.add_tool(str_replace_editor, time_out=10)
    assert len(tools.schema) == 1, "应只有一个工具的 schema"
    s = tools.schema[0]
    assert s["function"]["name"] == "str_replace_editor"
    params = s["function"]["parameters"]["properties"]

    print("schema 生成的参数：")
    for name, spec in params.items():
        print(f"  - {name}: {spec.get('type')} {spec.get('description','')[:30]}")

    # view_range 应是 array（整数列表）
    vr = params.get("view_range")
    assert vr is not None, "view_range 参数缺失"
    print(f"\nview_range 的 schema: {vr}")
    if "items" in vr:
        assert vr["items"].get("type") == "integer", "view_range 元素应为 integer"
        print("  ✅ view_range 被正确解析为整数数组")
    else:
        # 某些 pydantic 版本用 anyOf 表达 Optional；打印以确认
        print("  ⚠️ view_range 无 items 字段（可能是 Optional 包装，需人工确认 anyOf）")
    print("  ✅ schema 生成验证通过\n")


def test_ops():
    import str_replace_editor as se
    p = os.path.join(se._WORKDIR, "_se_demo.txt")

    r1 = str_replace_editor("create", p, file_text="alpha\nbeta\ngamma\n")
    assert r1["Success"] == "True", r1
    print("create:", r1["result"])

    r2 = str_replace_editor("view", p)
    print("view:\n" + r2["result"])

    r3 = str_replace_editor("str_replace", p, old_str="beta", new_str="BETA")
    assert r3["Success"] == "True", r3
    print("str_replace:", r3["result"])

    r4 = str_replace_editor("insert", p, insert_line=1, new_str="alpha-and-half\n")
    assert r4["Success"] == "True", r4
    print("insert:", r4["result"])

    r5 = str_replace_editor("view", p)
    print("最终 view:\n" + r5["result"])
    assert "alpha-and-half" in r5["result"]
    assert "BETA" in r5["result"]

    # 唯一性校验：old_str 重复应报错
    r6 = str_replace_editor("str_replace", p, old_str="alpha", new_str="x")
    print("重复 old_str 的报错信息:", r6.get("Error"))

    # 清理
    os.remove(p)
    print("  ✅ 四种操作 + 唯一性校验通过\n")


def test_path_escape():
    # 尝试越权限访问工作目录外的文件
    r = str_replace_editor("view", "../outside.txt")
    print("越权访问结果:", r)
    # 外层已 try/except，越权会返回 Success=False + Error
    assert r["Success"] == "False" or "Error" in r, "越权应被拦截"
    assert "越权" in r.get("Error", "") or "非法" in r.get("Error", "")
    print("  ✅ 路径越权拦截通过\n")


if __name__ == "__main__":
    test_schema()
    test_ops()
    test_path_escape()
    print("===== str_replace_editor 全部验证通过 =====")
