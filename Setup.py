"""Setup.py：一键安装项目依赖并下载本地模型。

用法：
    python Setup.py                # 安装全部 Python 依赖 + 下载 BGE 嵌入模型
    python Setup.py --with-ocr     # 额外预下载 Pix2Text OCR 模型（较慢）
    python Setup.py --skip-models  # 只安装 Python 依赖，不下载模型
    python Setup.py --mirror <url> # 指定 pip 镜像源

镜像策略：
    - pip 默认使用清华镜像，失败自动尝试阿里云、中科大镜像；
    - HuggingFace 模型默认通过 hf-mirror.com 中文镜像下载；
    - 也可设置环境变量 HF_ENDPOINT 覆盖 HuggingFace 镜像。
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BGE_DIR = ROOT / "models" / "bge-base-zh-v1.5"

PYPI_MIRRORS = [
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://mirrors.aliyun.com/pypi/simple/",
    "https://pypi.mirrors.ustc.edu.cn/simple/",
]
HF_MIRROR = "https://hf-mirror.com"

PACKAGES = [
    "openai>=1.0",
    "pydantic>=2.0",
    "pydantic-core>=2.0",
    "annotated-types>=0.5",
    "tiktoken>=0.5",
    "chromadb>=0.4",
    "sentence-transformers>=2.2",
    "pix2text",
    "PyMuPDF>=1.20",
    "Pillow>=9.0",
    "onnxruntime>=1.15",
    "huggingface_hub>=0.20",
    "faster-whisper>=1.0",
    "sounddevice>=0.4",
    "numpy",
]


def run(cmd: list[str], description: str = "", env: dict | None = None) -> int:
    print(f"\n===== {description} =====")
    print("命令：", " ".join(str(x) for x in cmd))
    full_env = os.environ.copy()
    full_env.setdefault("HF_ENDPOINT", HF_MIRROR)
    if env:
        full_env.update(env)
    try:
        proc = subprocess.run(cmd, cwd=ROOT, env=full_env)
        return proc.returncode
    except FileNotFoundError as e:
        print(f"命令未找到：{e}")
        return 127


def install_packages(mirror: str | None = None) -> None:
    mirrors = [mirror] if mirror else PYPI_MIRRORS
    for i, m in enumerate(mirrors, 1):
        print(f"\n===== 尝试 pip 镜像 [{i}/{len(mirrors)}]：{m} =====")
        cmd = [
            sys.executable, "-m", "pip", "install",
            "-i", m,
            "--timeout", "120",
            *PACKAGES,
        ]
        code = run(cmd, description="安装 Python 依赖")
        if code == 0:
            print("依赖安装完成。")
            return
        print(f"镜像 {m} 安装失败（退出码 {code}），尝试下一个镜像...")
    sys.exit("所有 pip 镜像均安装失败，请检查网络或手动执行 pip install。")


def _run_python(code: str, description: str) -> None:
    env = {"HF_ENDPOINT": HF_MIRROR}
    ret = run([sys.executable, "-c", code], description=description, env=env)
    if ret != 0:
        print(f"{description}失败（退出码 {ret}）。可稍后重试，或检查 HF_ENDPOINT 网络。")
        sys.exit(ret)


def download_bge_model() -> None:
    """下载 BGE 中文嵌入模型到 ./models/bge-base-zh-v1.5，供 Saver 使用。"""
    BGE_DIR.mkdir(parents=True, exist_ok=True)
    code = (
        "from huggingface_hub import snapshot_download\n"
        f"snapshot_download(repo_id='BAAI/bge-base-zh-v1.5', local_dir={str(BGE_DIR)!r})\n"
        "print('BGE 嵌入模型已下载至', " + repr(str(BGE_DIR)) + ")\n"
    )
    _run_python(code, "下载 BGE 嵌入模型（BAAI/bge-base-zh-v1.5）")


def _download_modelscope_model(repo_id: str, model_dir: Path) -> None:
    """通过 ModelScope 公开 API 分文件下载模型到本地目录（无需安装 modelscope 包）。"""
    model_dir.mkdir(parents=True, exist_ok=True)
    files_url = (
        "https://modelscope.cn/api/v1/models/"
        f"{urllib.parse.quote(repo_id, safe='/')}/repo/files?Revision=master"
    )
    print(f"获取 ModelScope 文件列表：{files_url}")
    req = urllib.request.Request(files_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    files = data.get("Data", {}).get("Files", [])
    if not files:
        print("ModelScope 未返回文件列表，可能模型不存在或 API 变动。")
        return

    for f in files:
        name = f.get("Name")
        if not name:
            continue
        dest = model_dir / name
        if dest.exists() and dest.stat().st_size == int(f.get("Size") or 0):
            print(f"跳过已存在文件：{name}")
            continue
        file_url = (
            "https://modelscope.cn/api/v1/models/"
            f"{urllib.parse.quote(repo_id, safe='/')}/repo?Revision=master&"
            f"FilePath={urllib.parse.quote(name, safe='')}"
        )
        print(f"下载 {name} <- {file_url}")
        req2 = urllib.request.Request(file_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req2, timeout=120) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as out:
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if total and done % (20 * 1024 * 1024) < len(chunk):
                        print(f"  {name}: {done / 1024 / 1024:.1f}MB / {total / 1024 / 1024:.1f}MB")
        print(f"完成：{name}")
    print(f"ModelScope 模型已下载至：{model_dir}")


def download_listener_model(size: str = "medium", source: str = "modelscope") -> None:
    """下载 Faster-Whisper 模型到 ./models/faster-whisper-<size>，供 Listener 使用。"""
    model_dir = ROOT / "models" / f"faster-whisper-{size}"
    model_dir.mkdir(parents=True, exist_ok=True)
    if source == "hf":
        repo_id = f"Systran/faster-whisper-{size}"
        code = (
            "from huggingface_hub import snapshot_download\n"
            f"snapshot_download(repo_id={repo_id!r}, local_dir={str(model_dir)!r})\n"
            "print('Faster-Whisper 模型已下载至', " + repr(str(model_dir)) + ")\n"
        )
        _run_python(code, f"下载 Faster-Whisper 模型（{repo_id}，hf-mirror）")
    else:
        repo_id = f"pengzhendong/faster-whisper-{size}"
        _download_modelscope_model(repo_id, model_dir)


def preload_pix2text_models() -> None:
    """预下载 Pix2Text OCR 模型。首次运行会从 HuggingFace 镜像拉取，耗时较长。"""
    code = (
        "import os\n"
        f"os.environ.setdefault('HF_ENDPOINT', {HF_MIRROR!r})\n"
        "from pix2text import Pix2Text\n"
        "p = Pix2Text(device='cpu', use_fast=True, providers=['CPUExecutionProvider'])\n"
        "print('Pix2Text OCR 模型已就绪。')\n"
    )
    _run_python(code, "预下载 Pix2Text OCR 模型")


def verify_imports() -> None:
    code = (
        "import openai, pydantic, tiktoken, chromadb, sentence_transformers, fitz, PIL, onnxruntime, pix2text, faster_whisper, sounddevice\n"
        "print('核心依赖导入验证通过。')\n"
    )
    _run_python(code, "验证核心依赖导入")


def main() -> None:
    parser = argparse.ArgumentParser(description="AuTo_LaTeX 系统一键安装脚本")
    parser.add_argument("--with-ocr", action="store_true", help="额外预下载 Pix2Text OCR 模型（较慢）")
    parser.add_argument("--with-listener", action="store_true", help="预下载 Listener 的 Faster-Whisper 模型")
    parser.add_argument("--listener-size", default=os.environ.get("LISTENER_MODEL_SIZE", "medium"),
                        help="Faster-Whisper 模型规模：tiny/base/small/medium/large-v3（默认 small）")
    parser.add_argument("--listener-source", choices=["modelscope", "hf"],
                        default=os.environ.get("LISTENER_DOWNLOAD_SOURCE", "modelscope"),
                        help="Listener 模型下载源：modelscope（默认，国内更稳）/ hf（hf-mirror）")
    parser.add_argument("--skip-models", action="store_true", help="只安装 Python 依赖，不下载任何模型")
    parser.add_argument("--skip-packages", action="store_true", help="跳过 pip 依赖安装，只下载模型")
    parser.add_argument("--mirror", default=None, help="指定 pip 镜像源（默认清华镜像，失败自动切换）")
    parser.add_argument("--no-verify", action="store_true", help="跳过依赖导入验证")
    args = parser.parse_args()

    if args.skip_packages:
        print("已跳过 pip 依赖安装。请确保 faster-whisper / sounddevice 等依赖已安装。\n")
    else:
        install_packages(args.mirror)

    if args.skip_models:
        print("已跳过模型下载。Saver 需要 BGE 模型；Pix2Text/Listener 模型将在首次使用时自动下载（已走镜像）。")
    else:
        download_bge_model()
        if args.with_listener:
            download_listener_model(args.listener_size, args.listener_source)
        else:
            print(
                "\n提示：Listener 的 Faster-Whisper 模型将在首次转写时自动下载（默认走 hf-mirror.com）。\n"
                "如网络不通，请运行：python Setup.py --with-listener --listener-source modelscope\n"
                "（ModelScope 国内源，文件直接下载到 ./models/faster-whisper-<size>）。"
            )
        if args.with_ocr:
            preload_pix2text_models()
        else:
            print(
                "\n提示：Pix2Text OCR 模型将在首次 OCR 时自动下载（已默认走 hf-mirror.com 镜像）。\n"
                "如需现在预下载，可重新运行：python Setup.py --with-ocr"
            )

    if not args.no_verify:
        verify_imports()

    print("\nSetup 完成。")
    print(f"- BGE 嵌入模型目录：{BGE_DIR}")
    if not args.skip_models and args.with_listener:
        print(f"- Faster-Whisper 模型目录：{ROOT / 'models' / f'faster-whisper-{args.listener_size}'}")
        print(f"- Listener 模型下载源：{args.listener_source}")
    print("- 可通过环境变量 BGE_MODEL_DIR 覆盖嵌入模型路径；LISTENER_MODEL_DIR/LISTENER_MODEL_SIZE 覆盖 Listener 模型。")
    print("- Visal.py 已默认 HF_ENDPOINT=https://hf-mirror.com。")

if __name__ == "__main__":
    main()
