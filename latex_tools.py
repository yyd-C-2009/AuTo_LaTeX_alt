'''LaTeX / TikZ 工具: 写文件、语法检查、TikZ 编译验证, 以及 example.tex 规范查看。

check_latex 与 check_tikz 都 shell out 到 pdflatex, 分别写 latex_output/ 与 tikz_output/。
产物路径与返回文本是契约: resident.py 的 verify_latex 阶段依赖 check_latex 成功时返回的
「语法检查通过」前缀 (见 resident._LATEX_OK_MARK) —— 改返回文案会静默破坏常驻笔记的游标门禁。
'''

import os
import shutil
import subprocess
from typing import Annotated

from config import OUT_DIR, TIKZ_DIR


def view_theorem_style(
    brief: Annotated[bool, "可选：True 只返回定理环境与 label/ref 规范要点；False（默认）返回 example.tex 全文"] = False,
) -> str:
    """查看 example.tex 中约定的 LaTeX 定理环境与引用命令规范（导言区宏包、定理声明、label/ref 样例），供 MathWrite/PassageWrite 编写定理环境前查阅。"""
    if brief:
        return (
            "example.tex 定理环境规范要点：\n"
            "1. 导言区：\\documentclass{book}；宏包 inputenc / xeCJK / amsmath,amsthm,amssymb / graphicx / "
            "booktabs / enumitem / hyperref / bm / darkmode。\n"
            "2. 定理环境声明：\n"
            "\\newtheorem{theorem}{Theorem}[section]\n"
            "\\newtheorem{lemma}[theorem]{Lemma}\n"
            "\\newtheorem{proposition}[theorem]{Proposition}\n"
            "\\newtheorem{corollary}[theorem]{Corollary}\n"
            "\\newtheorem{definition}[theorem]{Definition}\n"
            "\\newtheorem{remark}{Remark}[section]\n"
            "\\newtheorem{Example}{Example}[section]\n"
            "\\newenvironment{solution}{\\begin{proof}[Solution]}{\\end{proof}}\n"
            "3. 标签命令：\\theolabel{key} / \\lemmlabel{key} / \\proplabel{key} / \\corolabel{key} / "
            "\\deflabel{key} / \\exaplabel{key}（生成带前缀的 \\label）。\n"
            "4. 引用命令：\\theoref{theo:key} / \\lemmref{lem:key} / \\propref{prop:key} / "
            "\\cororef{coro:key} / \\defref{def:key} / \\exapref{exap:key}；ref 参数是完整标签。\n"
            "5. proof 不编号；solution 环境等价于 proof 的 Solution 标题；行间公式用 \\label 编号、\\eqref 引用。"
        )
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "example.tex")
    if not os.path.isfile(path):
        return f"错误: 规范文件不存在 {path}"
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return f"读取 example.tex 失败: {type(e).__name__}: {e}"
    return "以下是 example.tex 全文（LaTeX 定理环境与引用命令的规范来源）：\n\n" + content.strip()


def write_latex(content: Annotated[str, "要写入的LaTeX内容"], filename: Annotated[str, "文件名(可省略)"] = "output.tex") -> str:
    '''将LaTeX内容写入 latex_output/ 目录下的 .tex 文件, 返回保存路径'''
    name = os.path.basename(filename) or "output.tex"
    if not name.endswith(".tex"):
        name += ".tex"
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def check_tikz(code: Annotated[str, "待验证的 TikZ 绘图代码 (仅 tikzpicture 内容) "], filename: Annotated[str, "文件名(可省略)"] = "figure") -> str:
    '''编译 TikZ 代码验证其能否成功: 成功返回 PDF 路径, 失败返回编译日志'''
    exe = shutil.which("pdflatex")
    if not exe:
        return "错误: 找不到 pdflatex, 请确认 LaTeX 已安装且在 PATH 中"
    os.makedirs(TIKZ_DIR, exist_ok=True)
    name = os.path.splitext(os.path.basename(filename) or "figure")[0]
    tex = (
        "\\documentclass[border=2pt]{standalone}\n"
        "\\usepackage{tikz}\n"
        "\\usepackage{amsmath,amssymb}\n"
        "\\begin{document}\n"
        f"{code}\n"
        "\\end{document}\n"
    )
    with open(os.path.join(TIKZ_DIR, name + ".tex"), "w", encoding="utf-8") as f:
        f.write(tex)
    try:
        proc = subprocess.run(
            [exe, "-interaction=nonstopmode", "-halt-on-error", name + ".tex"],
            cwd=TIKZ_DIR, capture_output=True, text=True, timeout=60, encoding="utf-8"
        )
    except subprocess.TimeoutExpired:
        return "编译超时(>60s)"
    if proc.returncode == 0:
        return "编译成功 (PDF: " + os.path.join(TIKZ_DIR, name + ".pdf") + ")"
    log = proc.stdout or ""
    return "编译失败: \n" + "\n".join(log.splitlines()[-15:])


def check_latex(filename: Annotated[str, "要检查语法的 .tex 文件名 (位于 latex_output/ 下) "] = "output.tex") -> str:
    '''只做 LaTeX 语法检查, 不生成 PDF: 使用 pdflatex -draftmode 编译 latex_output/ 下的 .tex 文件。
    成功返回「语法检查通过」, 失败返回编译日志末尾 (可据此定位错误行) 。'''
    exe = shutil.which("pdflatex")
    if not exe:
        return "错误: 找不到 pdflatex, 请确认 LaTeX 已安装且在 PATH 中"
    name = os.path.basename(filename) or "output.tex"
    if not name.endswith(".tex"):
        name += ".tex"
    tex_path = os.path.join(OUT_DIR, name)
    if not os.path.isfile(tex_path):
        return f"错误: 文件不存在 {tex_path}, 请先用 write_latex 写入后再检查"
    try:
        proc = subprocess.run(
            [exe, "-draftmode", "-interaction=nonstopmode", "-halt-on-error", name],
            cwd=OUT_DIR, capture_output=True, text=True, timeout=60, encoding="utf-8"
        )
    except subprocess.TimeoutExpired:
        return "语法检查超时(>60s)"
    if proc.returncode == 0:
        return "语法检查通过 (未发现 LaTeX 语法错误) "
    log = proc.stdout or ""
    return "语法检查失败: \n" + "\n".join(log.splitlines()[-20:])
