"""pytest plugin: write the node ids that fail (call/setup/teardown/collect) to $TRUTH_OUT."""
import json, os

_failed = set()
_collect_failed = set()

def pytest_runtest_logreport(report):
    if report.outcome == "failed":
        _failed.add(report.nodeid)

def pytest_collectreport(report):
    if report.outcome == "failed":
        _collect_failed.add(report.nodeid)

def pytest_sessionfinish(session, exitstatus):
    json.dump({"failed": sorted(_failed), "collect_failed": sorted(_collect_failed), "exit": int(exitstatus)},
              open(os.environ["TRUTH_OUT"], "w"))
