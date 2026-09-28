"""在受限子进程中运行计算脚本；不允许文件、网络或进程访问。"""

import ast
import json
import subprocess
import sys
from pathlib import Path

MAX_CODE = 12_000
MAX_OUTPUT = 20_000
TIMEOUT_SECONDS = 5
_CHILD_FLAG = "--sandbox-child"


class SandboxError(ValueError):
    pass


def _validate(source: str) -> None:
    if not isinstance(source, str) or not source.strip():
        raise SandboxError("请提供非空 Python 脚本。")
    if len(source) > MAX_CODE:
        raise SandboxError(f"脚本不能超过 {MAX_CODE} 个字符。")
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        raise SandboxError(f"Python 语法错误：第 {exc.lineno} 行，{exc.msg}") from exc
    if sum(1 for _ in ast.walk(tree)) > 1_500:
        raise SandboxError("脚本结构过大，请拆成更小的计算。")
    _Validator().visit(tree)
    try:
        compile(tree, "<calculation>", "exec")
    except SyntaxError as exc:
        raise SandboxError(f"Python 语法错误：第 {exc.lineno} 行，{exc.msg}") from exc


class _Validator(ast.NodeVisitor):
    _allowed = (
        ast.Module, ast.Expr, ast.Assign, ast.AnnAssign, ast.AugAssign,
        ast.Name, ast.Load, ast.Store, ast.Constant, ast.BinOp, ast.UnaryOp,
        ast.Compare, ast.BoolOp, ast.If, ast.IfExp, ast.For, ast.Break,
        ast.Continue, ast.Pass, ast.FunctionDef, ast.arguments, ast.arg,
        ast.Return, ast.Call, ast.keyword, ast.List, ast.Tuple, ast.Set,
        ast.Dict, ast.Subscript, ast.Slice, ast.ListComp, ast.SetComp,
        ast.DictComp, ast.GeneratorExp, ast.comprehension, ast.JoinedStr,
        ast.FormattedValue, ast.Import, ast.ImportFrom, ast.And, ast.Or,
        ast.Not, ast.UAdd, ast.USub, ast.Invert, ast.Add, ast.Sub, ast.Mult,
        ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.MatMult, ast.LShift,
        ast.RShift, ast.BitOr, ast.BitXor, ast.BitAnd, ast.Eq, ast.NotEq,
        ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is, ast.IsNot, ast.In,
        ast.NotIn, ast.Attribute,
    )
    _modules = {"math", "statistics", "decimal", "fractions"}
    _module_members = {
        "math": {name for name in __import__("math").__dict__
                 if not name.startswith("_")} - {"factorial", "comb", "perm", "prod"},
        "statistics": {"mean", "fmean", "median", "median_low", "median_high",
                       "mode", "multimode", "quantiles", "variance", "stdev",
                       "pvariance", "pstdev", "harmonic_mean", "geometric_mean",
                       "NormalDist", "correlation", "linear_regression"},
        "decimal": {"Decimal", "DecimalException", "InvalidOperation", "ROUND_HALF_EVEN",
                    "ROUND_HALF_UP", "ROUND_DOWN", "ROUND_UP"},
        "fractions": {"Fraction"},
    }
    _methods = {
        "append", "extend", "insert", "remove", "pop", "clear", "copy",
        "count", "index", "sort", "reverse", "add", "discard", "update",
        "union", "intersection", "difference", "issubset", "issuperset",
        "get", "keys", "values", "items", "setdefault", "split", "rsplit",
        "join", "strip", "lstrip", "rstrip", "replace", "lower", "upper",
        "casefold", "startswith", "endswith", "find", "rfind", "count",
        "capitalize", "title", "is_integer", "as_integer_ratio", "quantize",
        "sqrt", "exp", "ln", "log10", "normalize", "limit_denominator",
        "numerator", "denominator", "inv_cdf", "cdf", "pdf", "zscore",
    }
    _forbidden_calls = {
        "open", "eval", "exec", "compile", "input", "breakpoint", "help",
        "globals", "locals", "vars", "dir", "getattr", "setattr", "delattr",
        "__import__", "memoryview", "classmethod", "staticmethod", "type",
        "object", "super",
    }

    def __init__(self):
        self.imported_names = {}

    def generic_visit(self, node):
        if not isinstance(node, self._allowed):
            raise SandboxError(f"脚本包含不支持的语法：{type(node).__name__}。")
        super().generic_visit(node)

    def visit_Name(self, node):
        if node.id.startswith("__"):
            raise SandboxError("脚本不能访问 Python 内部名称。")

    def visit_Import(self, node):
        for item in node.names:
            if item.name not in self._modules:
                raise SandboxError(f"禁止导入 {item.name}；只开放 math、statistics、decimal、fractions。")
            self.imported_names[item.asname or item.name] = item.name

    def visit_ImportFrom(self, node):
        if node.level or node.module not in self._modules:
            raise SandboxError("只允许从 math、statistics、decimal、fractions 导入。")
        for item in node.names:
            if item.name == "*" or item.name not in self._module_members[node.module]:
                raise SandboxError(f"不允许从 {node.module} 导入 {item.name}。")
            self.imported_names[item.asname or item.name] = node.module

    def visit_Attribute(self, node):
        if node.attr.startswith("_"):
            raise SandboxError("脚本不能访问以下划线开头的属性。")
        if isinstance(node.value, ast.Name) and node.value.id in self.imported_names:
            module = self.imported_names[node.value.id]
            if node.attr not in self._module_members[module]:
                raise SandboxError(f"不允许访问 {module}.{node.attr}。")
        elif node.attr not in self._methods:
            raise SandboxError(f"不允许访问属性 {node.attr}。")
        self.generic_visit(node)

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id in self._forbidden_calls:
            raise SandboxError(f"禁止调用 {node.func.id}。")
        self.generic_visit(node)

    def visit_AugAssign(self, node):
        if not isinstance(node.target, ast.Name):
            raise SandboxError("复合赋值只支持变量，例如 total += value。")
        self.generic_visit(node)


def _child() -> None:
    """子进程入口；用户脚本只看见白名单内置函数和数值模块。"""
    import builtins
    import contextlib
    import io
    import math
    import operator
    import statistics
    import decimal
    import fractions

    request = json.loads(sys.stdin.read())
    source = request["code"]
    try:
        _validate(source)
        tree = ast.parse(source, mode="exec")

        binary = {
            "Add": operator.add, "Sub": operator.sub, "Mult": operator.mul,
            "Div": operator.truediv, "FloorDiv": operator.floordiv,
            "Mod": operator.mod, "Pow": operator.pow, "MatMult": operator.matmul,
            "LShift": operator.lshift, "RShift": operator.rshift,
            "BitOr": operator.or_, "BitXor": operator.xor, "BitAnd": operator.and_,
        }

        def bounded(value):
            if type(value) is int and value.bit_length() > 100_000:
                raise SandboxError("计算结果过大（整数超过 100,000 bit）。")
            if type(value) in (str, bytes, list, tuple, dict, set) and len(value) > 100_000:
                raise SandboxError("计算结果过大（集合或文本超过 100,000 项）。")
            return value

        def bit_cost(value):
            if type(value) is int:
                return value.bit_length()
            if isinstance(value, fractions.Fraction):
                return value.numerator.bit_length() + value.denominator.bit_length()
            return 0

        def estimate(value, seen=None):
            seen = seen or set()
            identity = id(value)
            if identity in seen:
                return 0
            seen.add(identity)
            if type(value) is str:
                return len(value) * 2 + 64
            if type(value) is bytes:
                return len(value) + 64
            if type(value) is int:
                return max(32, (value.bit_length() + 7) // 8)
            if isinstance(value, decimal.Decimal):
                return 64 + len(value.as_tuple().digits)
            if isinstance(value, fractions.Fraction):
                return estimate(value.numerator, seen) + estimate(value.denominator, seen)
            if type(value) in (list, tuple, set):
                return 64 + sum(estimate(item, seen) for item in value)
            if type(value) is dict:
                return 128 + sum(estimate(k, seen) + estimate(v, seen) for k, v in value.items())
            return 32

        def collect(values, kind="list"):
            result = []
            size = 64
            for value in values:
                if len(result) >= 10_000:
                    raise SandboxError("一次集合计算最多生成 10,000 项。")
                size += estimate(value)
                if size > 16_000_000:
                    raise SandboxError("集合计算结果超过 16 MB。")
                result.append(value)
            if kind == "set":
                return set(result)
            if kind == "dict":
                output = {}
                for key, value in result:
                    output[key] = value
                return output
            return result

        collection_sizes = {}

        def safe_method(target, method, *args):
            if type(target) not in (list, dict, set):
                raise SandboxError(f"不允许对 {type(target).__name__} 调用 {method}。")
            current = collection_sizes.setdefault(id(target), estimate(target))
            extra = sum(estimate(arg) for arg in args)
            if current + extra > 16_000_000 or len(target) >= 10_000:
                raise SandboxError("可变集合超过 16 MB 或 10,000 项上限。")
            result = getattr(target, method)(*args)
            collection_sizes[id(target)] = current + extra
            return result

        def safe_binary(name, left, right):
            if name == "Pow" and type(right) is int:
                bits = bit_cost(left)
                if bits and bits * abs(right) > 100_000:
                    raise SandboxError("幂运算结果过大。")
            if name in ("Add", "Sub", "Mult", "Div") and (
                isinstance(left, fractions.Fraction) or isinstance(right, fractions.Fraction)
            ):
                work = bit_cost(left) + bit_cost(right)
                if name in ("Add", "Sub"):
                    work *= 2
                if work > 100_000:
                    raise SandboxError("分数运算结果过大。")
            if name == "Mult":
                seq, count = (left, right) if type(left) in (str, bytes, list, tuple) else (right, left)
                if type(seq) in (str, bytes, list, tuple) and type(count) is int and len(seq) * count > 100_000:
                    raise SandboxError("重复结果过大。")
                if type(seq) in (list, tuple) and type(count) is int and estimate(seq) * count > 16_000_000:
                    raise SandboxError("重复集合超过 16 MB 上限。")
                if type(left) is int and type(right) is int and left.bit_length() + right.bit_length() > 100_000:
                    raise SandboxError("乘法结果过大。")
            if name == "Add" and type(left) in (list, tuple) and type(right) is type(left):
                if estimate(left) + estimate(right) > 16_000_000:
                    raise SandboxError("集合相加结果超过 16 MB 上限。")
            return bounded(binary[name](left, right))

        class SafeOperators(ast.NodeTransformer):
            def visit_ListComp(self, node):
                self.generic_visit(node)
                generator = ast.GeneratorExp(node.elt, node.generators)
                return ast.copy_location(ast.Call(
                    ast.Name("__collect", ast.Load()), [generator], []), node)

            def visit_SetComp(self, node):
                self.generic_visit(node)
                generator = ast.GeneratorExp(node.elt, node.generators)
                return ast.copy_location(ast.Call(
                    ast.Name("__collect", ast.Load()), [generator, ast.Constant("set")], []), node)

            def visit_DictComp(self, node):
                self.generic_visit(node)
                pair = ast.Tuple([node.key, node.value], ast.Load())
                generator = ast.GeneratorExp(pair, node.generators)
                return ast.copy_location(ast.Call(
                    ast.Name("__collect", ast.Load()), [generator, ast.Constant("dict")], []), node)

            def visit_Call(self, node):
                self.generic_visit(node)
                if isinstance(node.func, ast.Attribute) and node.func.attr in {
                    "append", "extend", "insert", "add", "update", "setdefault",
                }:
                    return ast.copy_location(ast.Call(
                        ast.Name("__safe_method", ast.Load()),
                        [node.func.value, ast.Constant(node.func.attr), *node.args], node.keywords), node)
                return node

            def visit_BinOp(self, node):
                self.generic_visit(node)
                return ast.copy_location(ast.Call(
                    ast.Name("__safe_binary", ast.Load()),
                    [ast.Constant(type(node.op).__name__), node.left, node.right], []), node)

            def visit_AugAssign(self, node):
                name = node.target.id
                return ast.copy_location(ast.Assign(
                    [ast.Name(name, ast.Store())],
                    ast.Call(ast.Name("__safe_binary", ast.Load()),
                             [ast.Constant(type(node.op).__name__), ast.Name(name, ast.Load()), node.value], [])), node)

        tree = ast.fix_missing_locations(SafeOperators().visit(tree))

        def safe_range(*args):
            result = range(*args)
            if len(result) > 10_000:
                raise SandboxError("range 最多允许 10,000 次迭代。")
            return result

        def safe_pow(base, exponent, modulo=None):
            if type(exponent) is int and bit_cost(base) * abs(exponent) > 100_000:
                raise SandboxError("幂运算结果过大。")
            return bounded(pow(base, exponent) if modulo is None else pow(base, exponent, modulo))

        def safe_list(values=()):
            return collect(values, "list")

        def safe_tuple(values=()):
            return tuple(collect(values, "list"))

        def safe_set(values=()):
            return collect(values, "set")

        def safe_dict(values=(), **kwargs):
            pairs = values.items() if isinstance(values, dict) else values
            result = collect(pairs, "dict")
            for key, value in kwargs.items():
                result[key] = value
                if len(result) > 10_000 or estimate(result) > 16_000_000:
                    raise SandboxError("字典结果超过 16 MB 或 10,000 项上限。")
            return result

        def safe_sorted(values, *, key=None, reverse=False):
            return sorted(collect(values, "list"), key=key, reverse=reverse)

        output = io.StringIO()

        def safe_print(*values, sep=" ", end="\n"):
            if not isinstance(sep, str) or not isinstance(end, str):
                raise SandboxError("print 的 sep 和 end 必须是文本。")
            rendered = sep.join(str(value) for value in values) + end
            if output.tell() + len(rendered) > MAX_OUTPUT:
                raise SandboxError(f"输出不能超过 {MAX_OUTPUT} 个字符。")
            output.write(rendered)

        modules = {"math": math, "statistics": statistics, "decimal": decimal, "fractions": fractions}

        def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
            if level or name not in modules:
                raise SandboxError(f"禁止导入 {name}。")
            return modules[name]

        safe_builtins = {
            name: getattr(builtins, name) for name in (
                "abs", "all", "any", "bool", "complex", "dict", "enumerate",
                "float", "int", "len", "max", "min", "round",
                "str", "sum", "zip", "divmod",
            )
        }
        safe_builtins.update({"print": safe_print, "range": safe_range, "pow": safe_pow,
                              "list": safe_list, "tuple": safe_tuple, "set": safe_set,
                              "dict": safe_dict, "sorted": safe_sorted,
                              "__import__": safe_import})
        namespace = {"__builtins__": safe_builtins, "__safe_binary": safe_binary,
                     "__collect": collect, "__safe_method": safe_method}
        ticks = 0

        def budget(frame, event, arg):
            nonlocal ticks
            if event == "line":
                ticks += 1
                if ticks > 100_000:
                    raise SandboxError("脚本执行步骤超过上限。")
            return budget

        sys.setrecursionlimit(500)
        sys.settrace(budget)
        try:
            with contextlib.redirect_stdout(output):
                exec(compile(tree, "<calculation>", "exec"), namespace, namespace)
        finally:
            sys.settrace(None)
        result = {"ok": True, "output": output.getvalue()}
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:500]}
    sys.__stdout__.write(json.dumps(result, ensure_ascii=False))


def run_python(code: str) -> str:
    """在隔离子进程中运行受限 Python 计算脚本，并返回实际输出。"""
    try:
        _validate(code)
    except SandboxError as exc:
        return f"拒绝运行：{exc}"
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-S", str(Path(__file__).resolve()), _CHILD_FLAG],
            input=json.dumps({"code": code}, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=Path(__file__).resolve().parent,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"计算超时：脚本运行超过 {TIMEOUT_SECONDS} 秒。"
    if result.returncode:
        detail = result.stderr.strip()[:500] or f"子进程退出码 {result.returncode}"
        return f"计算进程失败：{detail}"
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return "计算进程返回了无法解析的结果。"
    if payload.get("ok"):
        return payload.get("output", "") or "脚本运行完成，没有输出。"
    return f"计算未完成：{payload.get('error', '未知错误')}"


if __name__ == "__main__" and sys.argv[-1:] == [_CHILD_FLAG]:
    _child()
