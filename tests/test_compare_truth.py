import unittest
from pathlib import Path
import tempfile
from unittest import mock

from tools.compare_truth import failed_numbers
from tools.truth import run_truth


class CompareTruthTests(unittest.TestCase):
    def setUp(self):
        self.tests = [
            {"id": "tests/test_a.py::TestA::test_one", "file": "tests/test_a.py", "number": 0},
            {"id": "tests/test_a.py::TestA::test_two", "file": "tests/test_a.py", "number": 1},
            {"id": "tests/sub/test_b.py::test_three", "file": "tests/sub/test_b.py", "number": 2},
        ]

    def test_collection_error_selects_every_test_in_file(self):
        result = {"exit": 2, "failed": [], "collect_failed": ["tests/test_a.py"]}
        self.assertEqual(failed_numbers(result, self.tests), {0, 1})

    def test_collection_error_for_directory_selects_descendants(self):
        result = {"exit": 2, "failed": [], "collect_failed": ["tests/sub"]}
        self.assertEqual(failed_numbers(result, self.tests), {2})

    def test_xdist_can_report_collection_errors_with_exit_one(self):
        result = {"exit": 1, "failed": [], "collect_failed": ["tests/test_a.py"]}
        self.assertEqual(failed_numbers(result, self.tests), {0, 1})

    def test_unknown_collection_error_is_invalid(self):
        result = {"exit": 2, "failed": [], "collect_failed": ["tests/missing.py"]}
        with self.assertRaises(ValueError):
            failed_numbers(result, self.tests)

    def test_collection_exit_without_error_is_invalid(self):
        with self.assertRaises(ValueError):
            failed_numbers({"exit": 2, "failed": [], "collect_failed": []}, self.tests)

    def test_inconsistent_exit_is_invalid(self):
        with self.assertRaises(ValueError):
            failed_numbers({"exit": 0, "failed": [self.tests[0]["id"]], "collect_failed": []}, self.tests)

    def test_existing_truth_is_not_silently_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            result = Path(directory) / "case.json"
            result.write_text("{}")
            with mock.patch.dict("os.environ", {"SQLGLOT_SRC": directory}), \
                 mock.patch("sys.argv", ["run_truth.py", directory, directory, "case"]):
                with self.assertRaises(FileExistsError):
                    run_truth.main()
            self.assertEqual(result.read_text(), "{}")


if __name__ == "__main__":
    unittest.main()
