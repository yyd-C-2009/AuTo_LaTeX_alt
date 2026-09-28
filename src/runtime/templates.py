"""受限工作流模板；Super 只能选择模板并填入具体任务。"""

TEMPLATES = {
    "ocr_to_latex": [
        {"expert": "math", "task": "分析并校验 OCR 结果：{goal}"},
        {"expert": "mathwrite", "task": "根据上一阶段结果写入 LaTeX：{goal}"},
        {"expert": "mathwrite", "task": "验收并检查 LaTeX 编译结果：{goal}"},
    ],
    "latex_document": [
        {"expert": "passagewrite", "task": "规划并撰写文档：{goal}"},
        {"expert": "mathwrite", "task": "验收并检查 LaTeX 文档：{goal}"},
    ],
    "lecture_notes": [
        {"expert": "notetaker", "task": "整理新增课堂转写：{goal}"},
        {"expert": "mathwrite", "task": "验收并检查课堂笔记 LaTeX：{goal}"},
    ],
}


def list_templates() -> str:
    return "可用工作流模板：" + "、".join(sorted(TEMPLATES))


def instantiate(template: str, goal: str) -> list[dict]:
    if template not in TEMPLATES:
        raise ValueError(f"未知工作流模板 {template!r}；{list_templates()}")
    return [{**step, "task": step["task"].format(goal=goal)} for step in TEMPLATES[template]]
