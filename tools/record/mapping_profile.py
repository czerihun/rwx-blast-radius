"""Record test-specific reads of baseline string-keyed dictionary values."""
import ast
import builtins
import importlib.machinery
import json
import os
from pathlib import Path
import tracemalloc

import pytest

_root = Path(os.environ["MAPPING_PROFILE_SRC"]).resolve()
_out = Path(os.environ["MAPPING_PROFILE_OUT"])
_tests = json.loads(Path(os.environ["MAPPING_PROFILE_TESTS"]).read_text())["tests"]
_numbers = {test["id"]: test["number"] for test in _tests}
_current = 0
_completed = set()
_reads = {}
_whole = {}
_sites = set()
_import_reads = set()


class _TrackedDict(dict):
    def __init__(self, value, site):
        super().__init__(value)
        self._site = site

    def _read(self, key):
        if not _current:
            _import_reads.add(self._site)
        if _current and not tracemalloc.is_tracing() and isinstance(key, str):
            entry = (self._site, key)
            _reads[entry] = _reads.get(entry, 0) | _current

    def _read_all(self):
        if not _current:
            _import_reads.add(self._site)
        if _current and not tracemalloc.is_tracing():
            _whole[self._site] = _whole.get(self._site, 0) | _current

    def __getitem__(self, key):
        self._read(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        self._read(key)
        return super().get(key, default)

    def items(self):
        self._read_all()
        return super().items()

    def values(self):
        self._read_all()
        return super().values()

    def copy(self):
        self._read_all()
        return super().copy()


def _tracked(value, site):
    _sites.add(site)
    return _TrackedDict(value, site)


def pytest_configure(config):
    import concurrent.futures
    from record.covplugin import _SyncPool

    concurrent.futures.ProcessPoolExecutor = _SyncPool
    builtins.__mapping_profile_dict__ = _tracked
    original = importlib.machinery.SourceFileLoader.source_to_code

    def source_to_code(loader, data, path, *, _optimize=-1):
        relative = os.path.relpath(path, _root)
        if not relative.startswith("sqlglot/") or not relative.endswith(".py"):
            return original(loader, data, path, _optimize=_optimize)
        tree = ast.parse(data, filename=path)

        class Instrument(ast.NodeTransformer):
            def __init__(self):
                self.function_depth = 0

            def visit_FunctionDef(self, node):
                self.function_depth += 1
                node = self.generic_visit(node)
                self.function_depth -= 1
                return node

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Assign(self, node):
                node = self.generic_visit(node)
                if self.function_depth or len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                    return node
                if not isinstance(node.value, ast.Dict) or not node.value.keys:
                    return node
                keys = node.value.keys
                if not any(isinstance(key, ast.Constant) and isinstance(key.value, str) for key in keys):
                    return node
                if any(key is not None and not (
                    isinstance(key, ast.Constant) and isinstance(key.value, str)
                ) for key in keys):
                    return node
                site = f"{relative}:{node.lineno}:{node.targets[0].id}"
                node.value = ast.copy_location(
                    ast.Call(func=ast.Name(id="__mapping_profile_dict__", ctx=ast.Load()),
                             args=[node.value, ast.Constant(site)], keywords=[]),
                    node.value,
                )
                return node

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
        "sites": sorted(_sites),
        "import_reads": sorted(_import_reads),
        "reads": [[site, key, str(bits)] for (site, key), bits in _reads.items()],
        "whole": {site: str(bits) for site, bits in _whole.items()},
    }))
