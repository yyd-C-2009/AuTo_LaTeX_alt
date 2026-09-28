"""兼容旧启动命令；实际入口位于 src/super.py。"""

import runpy
import sys
from pathlib import Path

if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    sys.path.insert(0, str(root / "src"))
    runpy.run_path(str(root / "src" / "super.py"), run_name="__main__")
