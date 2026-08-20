"""plan.py：日程/计划管理工具（Plan）。

设计约定：
    - 与 Saver / Visal / Listener 一致：Plan 是一个长生命周期对象，在 initer.init() 中
      创建一次并常驻，通过 register_common_tools 注册为全局工具，供 Super（及直接对话的
      listener/math 等专家身份）调用。
    - 所有日程内容统一存放在项目根目录的 plan/ 文件夹下：
        plan/YYYYMMDD.md   —— 每日日程（按当天日期命名，缺当天文件自动创建）
        plan/general.md    —— 长期事务（跨天/长期待办，常驻）
    - 提供接口函数：
        add_today_plan(content, date)   添加某天日程（默认今天，日期格式 YYYYMMDD 或 YYYY-MM-DD）
        add_general_plan(content)       添加长期事务到 general.md
        view_plan(date)                 查看某天日程（默认今天），支持 YYYYMMDD / YYYY-MM-DD
        view_general_plan()             查看长期事务
    - 全部函数为同步阻塞、返回 str，交给 Tools.async_execute 的 to_thread 分支调度；
      文件写入/读取均带 try/except 兜底，避免文件异常炸掉 Agent 任务。
"""

import os
from datetime import date
from typing import Annotated

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PLAN_DIR = os.path.join(BASE_DIR, "plan")


def _norm_date(raw: str | None) -> str:
    """把用户输入归一成 YYYYMMDD 字符串。

    支持格式：
        None/空            -> 今天
        20260820           -> 20260820
        2026-08-20         -> 20260820
        2026/08/20         -> 20260820
    无法解析时返回空字符串（调用方据此报错）。
    """
    if not raw:
        return date.today().strftime("%Y%m%d")
    s = str(raw).strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    # 只保留 8 位日期，丢弃可能附带的时分秒等
    digits = digits[:8]
    if len(digits) != 8:
        return ""
    # 粗校验：年 1900-2099，月 01-12，日 01-31
    try:
        y, m, d = int(digits[:4]), int(digits[4:6]), int(digits[6:8])
        date(y, m, d)  # 非法日期会抛 ValueError
    except ValueError:
        return ""
    return digits


def _read_file(path: str) -> str:
    if not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _append_file(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)


class Plan:
    """日程/计划管理：每日日程写入 plan/YYYYMMDD.md，长期事务写入 plan/general.md。"""

    def __init__(self, plan_dir: str | None = None):
        self.plan_dir = plan_dir or PLAN_DIR
        os.makedirs(self.plan_dir, exist_ok=True)
        print(f"----------Plan loaded（目录: {self.plan_dir}）----------")

    def _day_path(self, date_str: str) -> str:
        return os.path.join(self.plan_dir, f"{date_str}.md")

    # ---------- 公共工具：每日日程 ----------

    def add_today_plan(
        self,
        content: Annotated[str, "要添加的日程内容（一段文字或一个待办条目）"],
        date: Annotated[str, "日期，格式 YYYYMMDD 或 YYYY-MM-DD；留空表示今天"] = "",
    ) -> str:
        """把一条日程/待办追加到指定日期（默认今天）的 plan/YYYYMMDD.md 文件。"""
        ds = _norm_date(date)
        if not ds:
            return f"错误: 无法解析日期「{date}」，请使用 YYYYMMDD 或 YYYY-MM-DD 格式。"
        text = (content or "").strip()
        if not text:
            return "错误: 日程内容为空，未写入任何内容。"

        path = self._day_path(ds)
        try:
            if not os.path.isfile(path):
                _append_file(path, _day_template(ds))
            _append_file(path, f"- {text}\n")
            return f"已添加到 {ds} 的日程（{os.path.basename(path)}）。"
        except Exception as e:
            return f"写入日程失败：{type(e).__name__}: {e}"

    def view_plan(
        self,
        date: Annotated[str, "日期，格式 YYYYMMDD 或 YYYY-MM-DD；留空表示今天"] = "",
    ) -> str:
        """查看指定日期（默认今天）的日程内容，即 plan/YYYYMMDD.md 全文。"""
        ds = _norm_date(date)
        if not ds:
            return f"错误: 无法解析日期「{date}」，请使用 YYYYMMDD 或 YYYY-MM-DD 格式。"
        path = self._day_path(ds)
        try:
            content = _read_file(path)
            if not content:
                return f"（{ds} 暂无日程记录，可先调用 add_today_plan 添加）"
            return content
        except Exception as e:
            return f"读取日程失败：{type(e).__name__}: {e}"

    # ---------- 公共工具：长期事务 ----------

    def add_general_plan(
        self,
        content: Annotated[str, "要添加的长期事务/待办内容（一段文字）"],
    ) -> str:
        """把一条长期事务追加到 plan/general.md（跨天的长期待办/目标）。"""
        text = (content or "").strip()
        if not text:
            return "错误: 长期事务内容为空，未写入任何内容。"
        path = os.path.join(self.plan_dir, "general.md")
        try:
            if not os.path.isfile(path):
                _append_file(path, _general_template())
            _append_file(path, f"- {text}\n")
            return f"已添加到长期事务 general.md。"
        except Exception as e:
            return f"写入长期事务失败：{type(e).__name__}: {e}"

    def view_general_plan(
        self,
    ) -> str:
        """查看 plan/general.md 中的全部长期事务。"""
        path = os.path.join(self.plan_dir, "general.md")
        try:
            content = _read_file(path)
            if not content:
                return "（暂无长期事务，可先调用 add_general_plan 添加）"
            return content
        except Exception as e:
            return f"读取长期事务失败：{type(e).__name__}: {e}"


# ---------- 模板 ----------

def _day_template(date_str: str) -> str:
    y = int(date_str[:4]); m = int(date_str[4:6]); d = int(date_str[6:8])
    return (
        f"# 日程记录 - {y:04d}-{m:02d}-{d:02d}\n\n"
        "\n"
        "## 今日日程安排\n\n"
        "\n"
        "## 备注\n\n"
        "- 本文件用于记录当日日程安排事件，不写入记忆库。\n\n"
    )


def _general_template() -> str:
    return (
        "# 长期事务（general）\n\n"
        "- 本文件用于记录跨天/长期待办与目标，不写入记忆库。\n\n"
    )


if __name__ == "__main__":
    # 简单自测：命令行用法
    p = Plan()
    print(p.add_today_plan("开会", date="2026-08-20"))
    print(p.add_general_plan("完成毕业论文初稿"))
    print(p.view_plan(date="2026-08-20"))
    print(p.view_general_plan())
