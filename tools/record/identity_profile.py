"""Record baseline identity-comparison outcomes per test, without patch inputs."""
import ast
import builtins
import importlib.machinery
import json
import os
import operator
from pathlib import Path
import tracemalloc

import pytest

_root = Path(os.environ["IDENTITY_PROFILE_SRC"]).resolve()
_out = Path(os.environ["IDENTITY_PROFILE_OUT"])
_test_ids = json.loads(Path(os.environ["IDENTITY_PROFILE_TESTS"]).read_text())["tests"]
_numbers = {test["id"]: test["number"] for test in _test_ids}
_current = 0
_results = {}
_completed = set()
_pure_types = {type(None), bool, int, float, complex, str, bytes}
_type_purity = {}
_ordering = {"Lt": (operator.lt, operator.le), "LtE": (operator.le, operator.lt),
             "Gt": (operator.gt, operator.ge), "GtE": (operator.ge, operator.gt)}


def _pure(value):
    kind = type(value)
    if kind not in _type_purity:
        _type_purity[kind] = kind in _pure_types or any(
            kind.__eq__ is base.__eq__ and kind.__ne__ is base.__ne__
            for base in (object, int, float, complex, str, bytes)
        )
    return _type_purity[kind]


def _compare(left, right, key, negate):
    result = left is not right if negate else left is right
    if not _current or tracemalloc.is_tracing():
        return result
    seen, different, unknown = _results.get(key, (0, 0, 0))
    old_different, old_unknown = different, unknown
    if not (different | unknown) & _current:
        if not (_pure(left) and _pure(right)):
            unknown |= _current
        elif result != (left != right if negate else left == right):
            different |= _current
    if not seen & _current or different != old_different or unknown != old_unknown:
        _results[key] = (seen | _current, different, unknown)
    return result


def _ordered_pure(value):
    if type(value) is tuple:
        return all(_ordered_pure(item) for item in value)
    return type(value) in (bool, int, float, str, bytes)


def _compare_order(left, right, key, operation):
    original, alternative = _ordering[operation]
    result = original(left, right)
    if not _current or tracemalloc.is_tracing():
        return result
    seen, different, unknown = _results.get(key, (0, 0, 0))
    old_different, old_unknown = different, unknown
    if not (different | unknown) & _current:
        if not (_ordered_pure(left) and _ordered_pure(right)):
            unknown |= _current
        elif result != alternative(left, right):
            different |= _current
    if not seen & _current or different != old_different or unknown != old_unknown:
        _results[key] = (seen | _current, different, unknown)
    return result


def _literal_operand(node):
    return isinstance(node, ast.Constant) or (
        isinstance(node, ast.Tuple) and all(_literal_operand(item) for item in node.elts)
    )


def pytest_configure(config):
    import concurrent.futures
    from record.covplugin import _SyncPool

    concurrent.futures.ProcessPoolExecutor = _SyncPool
    builtins.__identity_profile_compare__ = _compare
    builtins.__ordering_profile_compare__ = _compare_order
    original = importlib.machinery.SourceFileLoader.source_to_code

    def source_to_code(loader, data, path, *, _optimize=-1):
        relative = os.path.relpath(path, _root)
        if not relative.startswith("sqlglot/") or not relative.endswith(".py"):
            return original(loader, data, path, _optimize=_optimize)
        tree = ast.parse(data, filename=path)

        class Instrument(ast.NodeTransformer):
            def visit_Compare(self, node):
                node = self.generic_visit(node)
                if len(node.ops) != 1:
                    return node
                operation = type(node.ops[0]).__name__
                key = f"{relative}:{node.lineno}:{node.col_offset}:{operation}"
                if isinstance(node.ops[0], (ast.Is, ast.IsNot)):
                    function = "__identity_profile_compare__"
                    argument = isinstance(node.ops[0], ast.IsNot)
                elif operation in _ordering and (
                    _literal_operand(node.left) or _literal_operand(node.comparators[0])
                ):
                    function = "__ordering_profile_compare__"
                    argument = operation
                else:
                    return node
                return ast.copy_location(
                    ast.Call(
                        func=ast.Name(id=function, ctx=ast.Load()),
                        args=[node.left, node.comparators[0], ast.Constant(key),
                              ast.Constant(argument)],
                        keywords=[],
                    ),
                    node,
                )

        return compile(ast.fix_missing_locations(Instrument().visit(tree)), path, "exec",
                       dont_inherit=True, optimize=_optimize)

    importlib.machinery.SourceFileLoader.source_to_code = source_to_code


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    global _current
    number = _numbers.get(item.nodeid)
    _current = 0 if number is None else 1 << number
    yield
    if number is not None:
        _completed.add(number)
    _current = 0


def pytest_sessionfinish(session, exitstatus):
    worker = os.environ.get("PYTEST_XDIST_WORKER", "controller")
    _out.mkdir(parents=True, exist_ok=True)
    (_out / f"{worker}.json").write_text(json.dumps({
        "exit": int(exitstatus),
        "completed": sorted(_completed),
        "results": {key: [str(value) for value in bits] for key, bits in _results.items()},
    }))
