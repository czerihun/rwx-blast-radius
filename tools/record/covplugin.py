import os, sys, json, time
os.environ["COVERAGE_CORE"] = "ctrace"
import coverage
import pytest

_state = {"cur": None, "phase": None, "opens": {}, "dur": {}, "outcome": {}, "dialects": {}, "inst": {}, "wrapped": False}

def _audit(event, args):
    if event == "open":
        p = args[0]
        if isinstance(p, str) and "fixtures" in p:
            key = _state["cur"] or "<import>"
            _state["opens"].setdefault(key, {}).setdefault(_state["phase"] or "import", set()).add(os.path.abspath(p))

def _wrap_dialect():
    if _state["wrapped"]:
        return
    mod = sys.modules.get("sqlglot.dialects.dialect")
    if mod is None:
        return
    meta = mod._Dialect
    orig = meta.__call__
    def call(cls, *a, **k):
        if _state["cur"]:
            _state["dialects"].setdefault(_state["cur"], set()).add(cls.__module__ + "." + cls.__qualname__)
        return orig(cls, *a, **k)
    meta.__call__ = call
    # record which classes get instantiated by each test (Expr, Parser, Generator, Tokenizer)
    def track(base):
        def new(cls, *a, **k):
            cur = _state["cur"]
            if cur:
                _state["inst"].setdefault(cur, set()).add(cls.__module__ + "." + cls.__qualname__)
            return object.__new__(cls)
        base.__new__ = new
    import sqlglot.expressions.core as ec, sqlglot.parser as sp, sqlglot.generator as sg, sqlglot.tokens as st
    for base in (ec.Expr, sp.Parser, sg.Generator, st._TokenizerBase):
        track(base)
    _state["wrapped"] = True

class _SyncPool:
    """Run ProcessPoolExecutor work in-process so coverage sees it (spawned workers are invisible)."""
    def __init__(self, *a, **k):
        init = k.get("initializer")
        if init:
            init(*k.get("initargs", ()))
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def submit(self, fn, *a, **k):
        import concurrent.futures as cf
        import pickle
        fut = cf.Future()
        try:
            # cross the same pickle boundary a worker process would (sqlglot's serde runs there)
            a, k = pickle.loads(pickle.dumps((a, k)))
            fut.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
        except BaseException as ex:  # noqa
            fut.set_exception(ex)
        return fut
    def shutdown(self, *a, **k):
        pass

def pytest_configure(config):
    import concurrent.futures as cf
    cf.ProcessPoolExecutor = _SyncPool
    sys.addaudithook(_audit)
    cov = coverage.Coverage(data_file="/out/cov.db", source=["sqlglot"], branch=False, config_file=False)
    cov.start()
    _state["cov"] = cov

def pytest_collection_finish(session):
    _wrap_dialect()

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    cov = _state["cov"]
    cov.switch_context(item.nodeid)
    _state["cur"] = item.nodeid
    t = time.perf_counter()
    c = time.process_time()
    yield
    _state["dur"][item.nodeid] = (time.perf_counter() - t, time.process_time() - c)
    _state["cur"] = None
    _state["phase"] = None
    cov.switch_context("")

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _state["phase"] = "setup"; _wrap_dialect()
    yield
    _state["phase"] = "call"

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    _state["phase"] = "teardown"
    yield

def pytest_runtest_logreport(report):
    if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
        o = _state["outcome"].get(report.nodeid)
        if o != "failed":
            _state["outcome"][report.nodeid] = report.outcome

def pytest_sessionfinish(session, exitstatus):
    cov = _state["cov"]
    cov.stop()
    cov.save()
    json.dump({"opens": {k: {p: sorted(v) for p, v in d.items()} for k, d in _state["opens"].items()},
               "dialects": {k: sorted(v) for k, v in _state["dialects"].items()},
               "inst": {k: sorted(v) for k, v in _state["inst"].items()},
               "dur": _state["dur"], "outcome": _state["outcome"]}, open("/out/meta.json", "w"))
