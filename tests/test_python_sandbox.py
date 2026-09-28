import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from python_sandbox import run_python


class PythonSandboxTests(unittest.TestCase):
    def test_calculations_return_real_script_output(self):
        code = "import math\nvalues = [i * i for i in range(5)]\nprint(math.sqrt(81))\nprint(sum(values))"
        self.assertEqual(run_python(code), "9.0\n30\n")

    def test_file_and_network_imports_are_rejected(self):
        self.assertIn("拒绝运行：禁止导入 os", run_python("import os\nprint(os.getcwd())"))
        self.assertIn("拒绝运行：禁止导入 socket", run_python("import socket"))

    def test_runtime_limits_are_reported(self):
        result = run_python('values = ["x" * 10000 for _ in range(10000)]')
        self.assertIn("超过 16 MB", result)
        self.assertIn("输出不能超过", run_python("print('x' * 20000)"))
        huge_fraction = run_python("from fractions import Fraction\nprint(Fraction(2) ** 1000000000)")
        self.assertIn("幂运算结果过大", huge_fraction)


if __name__ == "__main__":
    unittest.main()
