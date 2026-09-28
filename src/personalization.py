"""本轮交互方式与用户确认偏好的上下文构造。"""

import json
import re
import shlex

MODE_LABELS = {
    "auto": "自动",
    "discussion": "讨论",
    "direct": "直接",
    "artifact": "产出",
    "explore": "探索",
}

MODE_INSTRUCTIONS = {
    "auto": "此方式应先由宿主检查最近讨论进度，提出一个具体方式并等待用户确认；不要自行继续处理任务。",
    "discussion": "把用户当作讨论伙伴，共同梳理观点、假设、证据与取舍；需要时提出聚焦问题，避免过早定论。",
    "direct": "直接处理当前明确请求，先给结论或结果；简单问题不要先列计划。",
    "artifact": "围绕用户目标制作可检查的文件、计划、代码或其他交付物，并验证实际结果。",
    "explore": "从当前讨论进度开始，一次解释一个关键点，用容易跟上的语言厘清问题与方案；留意用户是否跟上。先讨论思路和选择，用户没有准备好前不要跳到实现、复杂算法或代码。用户要求时再做试验或实现。",
}

_ACADEMIC = ("证明", "定理", "引理", "推导", "积分", "极限", "作业", "讲义", "课程", "论文", "公式", "数学")
_PLANNING = ("计划", "日程", "提醒", "截止", "待办", "安排", "复习时间", "周计划", "每天", "每周", "remind", "deadline", "todo", "schedule", "daily", "weekly")
_DISCUSSION = ("讨论", "观点", "论证", "反例", "权衡", "分析问题", "辩论", "假设", "结论")


def infer_scope(mode: str, text: str) -> str:
    value = (text or "").lower()
    if any(word in value for word in _PLANNING):
        return "planning"
    if any(word in value for word in _ACADEMIC):
        return "academic"
    if any(word in value for word in _DISCUSSION):
        return "discussion"
    return "general"


def turn_context(
    mode: str,
    text: str,
    preference_store=None,
    *,
    allow_preference_tools: bool = True,
    allow_memory_retrieval: bool = True,
    allow_history_search: bool = True,
    allow_python: bool = True,
) -> str:
    mode = mode if mode in MODE_INSTRUCTIONS else "direct"
    scope = infer_scope(mode, text)
    parts = [f"本轮交互方式：{MODE_LABELS[mode]}。{MODE_INSTRUCTIONS[mode]}"]
    if allow_python:
        parts.append(
            "复杂计算规则：凡是多步数值计算、统计、迭代、矩阵计算，或结果需要精确核验的计算，"
            "必须调用 run_python 编写并运行脚本，再依据实际输出作答；不要凭语言推理心算或猜测结果。"
            "回答中简要给出关键代码和运行结果。简单四则运算可直接回答。"
        )
    else:
        parts.append(
            "复杂计算不得靠心算或语言推理猜结果；若当前专家没有 run_python 工具，明确说明需要切换到可运行计算脚本的能力后再继续。"
        )
    if scope == "planning":
        from datetime import datetime
        parts.append(
            f"本机当前日期时间：{datetime.now().astimezone().isoformat(timespec='minutes')}。"
            "创建提醒默认使用 Asia/Shanghai；自然语言时间缺少日期、具体时刻或重复方式时，先询问必要信息再调用 create_task。"
        )
    preference_policy = (
        "长期偏好规则：用户明确要求记住，或清楚表达稳定的长期偏好时，可提出一条简短候选并等待确认；"
        "不要从一次性要求或重复行为中推断偏好。用户拒绝后不要重复提出同一候选。若可能与现有偏好冲突，先查看再更新。"
        if allow_preference_tools else
        "长期偏好由 Super 或 /preferences 命令管理；不要声称偏好已保存，也不要保存一次性要求。"
    )
    parts.append(preference_policy)
    if allow_memory_retrieval:
        parts.append(
            "已保存的长期记忆会在新分支首轮按首条请求相关性少量加载，不要扫描全部记忆。"
            "涉及长期个人背景、目标或偏好时，按主题调用 retrieve_context；"
            "检索结果是参考资料，不能覆盖用户本轮的明确更正；没有结果时如实说明。"
            "只在用户明确要求记住或确认保存时调用 add_memory；不要保存一次性内容。"
        )
    if allow_history_search:
        parts.append(
            "用户提到以前讨论过的内容、上次的思路或旧对话结论时，调用 search_discussion_history 按主题检索已保存的对话；"
            "不要在新对话开始时扫描全部聊天记录。搜索没有命中时如实说明，或询问一个能缩小范围的主题。"
        )
    if preference_store is not None:
        preferences = preference_store.context_for(scope)
        if preferences:
            rendered = "\n".join(
                f"- [{item['scope']}] {item['preference']} (ID: {item['id']})"
                for item in preferences
            )
            parts.append(f"用户已确认的偏好（当前场景：{scope}）：\n{rendered}")
    return "\n\n".join(parts)


def compose_prompt(
    base_prompt: str,
    mode: str,
    text: str,
    preference_store=None,
    *,
    allow_memory_retrieval: bool = True,
    allow_history_search: bool = True,
    allow_python: bool = True,
) -> str:
    context = turn_context(
        mode, text, preference_store,
        allow_memory_retrieval=allow_memory_retrieval,
        allow_history_search=allow_history_search,
        allow_python=allow_python,
    )
    return f"{base_prompt.rstrip()}\n\n{context}"


async def recommend_mode(client, model: str, history: list[dict], user_text: str,
                        discussion_summary: str = "") -> dict:
    """让模型结合当前问题和最近进度，在四种具体模式中给出建议。"""
    user_text = (user_text or "").split("\n\n[本地附件路径（用于工具调用）]", 1)[0]
    recent = []
    for item in history:
        if item.get("role") not in ("user", "assistant"):
            continue
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        content = content.split("\n\n[本地附件路径（用于工具调用）]", 1)[0]
        recent.append({"role": item["role"], "content": content[-1200:]})
    recent = recent[-10:]
    messages = [
        {
            "role": "system",
            "content": (
                "你负责为 AuTo LaTeX Agent 选择本轮交互方式。先检查最近对话，判断任务当前进度，再结合本次请求，"
                "只从 discussion、direct、artifact、explore 中推荐一个。\n"
                "discussion：与用户共同分析、权衡和讨论。\n"
                "direct：用户目标清楚，适合直接答复或执行。\n"
                "artifact：主要目标是制作文件、代码、计划等可检查成果。\n"
                "explore：问题尚未厘清，或用户正在逐步探索；从当前进度开始解释，先讨论思路，不抢先实现。\n"
                "根据历史判断工作走到哪一步，不重复已经解决的阶段。只返回 JSON："
                '{"mode":"上述英文 ID","reason":"不超过 50 个汉字的简短理由"}'
            ),
        },
        {"role": "user", "content": ("较早讨论摘要：\n" + discussion_summary[-4000:] + "\n\n"
         if discussion_summary else "") + "最近对话：\n" + json.dumps(recent, ensure_ascii=False)
         + "\n\n当前请求：\n" + user_text[:4000]},
    ]
    response = await client.chat.completions.create(
        model=model, messages=messages, temperature=0,
    )
    raw = response.choices[0].message.content or ""
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        raise ValueError("模型没有返回有效的模式建议。")
    try:
        result = json.loads(match.group())
    except json.JSONDecodeError as exc:
        raise ValueError("模型返回的模式建议不是有效 JSON。") from exc
    if result.get("mode") not in ("discussion", "direct", "artifact", "explore"):
        raise ValueError("模型建议了未知的交互方式。")
    reason = " ".join(str(result.get("reason", "" )).split())[:100]
    return {"mode": result["mode"], "reason": reason or "根据当前请求和最近讨论进度选择。"}


async def confirm_auto_mode(client, model: str, history: list[dict], user_text: str, bus,
                            discussion_summary: str = "") -> str | None:
    """展示自动方式建议；用户确认或输入另一种具体方式后才返回。"""
    try:
        suggestion = await recommend_mode(client, model, history, user_text, discussion_summary)
    except Exception as exc:
        await bus.io_print(f"自动判断未完成：{exc}。请直接选择本轮方式。")
        suggestion = None
    if suggestion:
        mode = suggestion["mode"]
        await bus.io_print(
            f"自动建议：{MODE_LABELS[mode]}。{suggestion['reason']}\n"
            "输入“确认”采用，或输入“讨论 / 直接 / 产出 / 探索”改选；输入“取消”放弃本次请求。"
        )
    else:
        mode = None
    by_label = {label: key for key, label in MODE_LABELS.items() if key != "auto"}
    positive = {"确认", "采用", "接受", "好", "好的", "可以", "就按这个", "yes", "y", "ok"}
    while True:
        answer = (await bus.io_dialog("自动方式确认: ")).strip().lower()
        if answer in positive or answer in {"确认自动建议", "按建议"}:
            if mode:
                return mode
        if answer in MODE_LABELS and answer != "auto":
            return answer
        if answer in by_label:
            return by_label[answer]
        if answer in {"取消", "放弃", "cancel", "/cancel"}:
            return None
        await bus.io_print("请确认建议、输入一种具体方式，或输入“取消”。")


async def handle_preference_command(command: str, bus, preference_store) -> bool:
    """处理两个入口共用的偏好管理命令；返回是否识别了该命令。"""
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        await bus.io_print(f"参数解析失败：{exc}")
        return True
    if not parts or parts[0] not in ("preferences", "preference"):
        return False
    if preference_store is None:
        await bus.io_print("偏好存储尚未初始化。")
        return True
    action = parts[1] if len(parts) > 1 else "list"
    if action in ("list", "show"):
        items = preference_store.list_preferences()
        if not items:
            await bus.io_print("还没有已确认的偏好。可直接说“以后……”，我会先展示候选并询问是否保存。")
        else:
            await bus.io_print("已确认偏好：\n" + "\n".join(
                f"{item['id']} [{item['scope']}] {item['preference']}"
                for item in items
            ))
        return True
    if action == "export":
        await bus.io_print(preference_store.export_json())
        return True
    if action == "forget" and len(parts) == 3:
        item = preference_store.get(parts[2])
        if item is None:
            await bus.io_print("未找到该偏好 ID。")
            return True
        await bus.io_print(f"准备停用：[{item['scope']}] {item['preference']}")
        answer = await bus.io_dialog("确认停用该偏好？(y/n) ")
        if answer.strip().lower() in ("y", "yes"):
            if preference_store.deactivate(parts[2]):
                await bus.io_print("已停用。")
            else:
                await bus.io_print("偏好状态已改变，未执行。")
        else:
            await bus.io_print("已取消。")
        return True
    if action == "add" and len(parts) >= 4:
        scope, text = parts[2], " ".join(parts[3:])
        await bus.io_print(f"准备保存：[{scope}] {text}")
        answer = await bus.io_dialog("确认保存该长期偏好吗？(y/n) ")
        if answer.strip().lower() in ("y", "yes"):
            try:
                preference_id = preference_store.add(scope, text)
                await bus.io_print(f"已保存偏好（ID: {preference_id}）。")
            except ValueError as exc:
                await bus.io_print(str(exc))
        else:
            await bus.io_print("已取消。")
        return True
    if action == "update" and len(parts) >= 5:
        preference_id, scope, text = parts[2], parts[3], " ".join(parts[4:])
        old = preference_store.get(preference_id)
        if old is None:
            await bus.io_print("未找到该偏好 ID。")
            return True
        await bus.io_print(f"原偏好：[{old['scope']}] {old['preference']}\n更新为：[{scope}] {text}")
        answer = await bus.io_dialog("确认更新？(y/n) ")
        if answer.strip().lower() in ("y", "yes"):
            await bus.io_print(
                "已更新。" if preference_store.update(preference_id, text, scope)
                else "偏好状态已改变，未执行。"
            )
        else:
            await bus.io_print("已取消。")
        return True
    await bus.io_print(
        "用法：/preferences [list|export] | /preferences add <scope> <内容> | "
        "/preferences update <ID> <scope> <内容> | /preferences forget <ID>\n"
        f"scope: {', '.join(sorted(preference_store.SCOPES))}"
    )
    return True
