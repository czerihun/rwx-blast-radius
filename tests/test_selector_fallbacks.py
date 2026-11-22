import unittest

from selector import Selector, parse_patch, remap_bits


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.selector = Selector(
            {
                "src": {},
                "names": {},
                "inst": {},
                "dialect": {},
                "all": 0b111,
                "idents": set(),
                "import_tests": 0,
                "fix": {"tests/fixtures/example.sql": [0b001, 0b010, 0]},
                "tests": {},
                "by_func": {},
                "by_class": {},
                "by_file": {},
            }
        )

    def test_unindexed_or_added_files_select_all(self):
        for path, status in (
            ("sqlglot/new.py", "added"),
            ("sqlglot/missing.py", "modified"),
            ("sqlglot/data.json", "modified"),
            ("tests/fixtures/new.sql", "added"),
            ("tests/fixtures/missing.sql", "modified"),
            ("tests/fixtures/example.sql", "renamed"),
            ("tests/new_test.py", "added"),
            ("tests/missing_test.py", "modified"),
        ):
            with self.subTest(path=path):
                file = {"old": path, "new": path, "status": status, "groups": [], "binary": False}
                self.assertEqual(self.selector.select([file]), 0b111)

    def test_unobserved_fixture_selects_all(self):
        self.selector.idx["fix"]["tests/fixtures/unobserved.sql"] = [0, 0, 0]
        file = {
            "old": "tests/fixtures/unobserved.sql",
            "new": "tests/fixtures/unobserved.sql",
            "status": "modified",
            "groups": [],
            "binary": False,
        }
        self.assertEqual(self.selector.select([file]), 0b111)

    def test_binary_python_patch_selects_all(self):
        self.selector.src["sqlglot/example.py"] = {}
        self.selector.idx["tests"]["tests/test_example.py"] = {
            "importers": [], "relative": False, "n": 0,
        }
        for path, binary_marker in (
            ("sqlglot/example.py", "GIT binary patch"),
            ("tests/test_example.py", "Binary files a/tests/test_example.py and b/tests/test_example.py differ"),
            ("sqlglotc/setup.py", "GIT binary patch"),
        ):
            with self.subTest(path=path):
                patch = f"diff --git a/{path} b/{path}\n{binary_marker}\n"
                self.assertEqual(self.selector.select(parse_patch(patch)), 0b111)

    def test_binary_rename_to_python_selects_all(self):
        patch = """\
diff --git a/docs/data.bin b/tests/test_example.py
rename from docs/data.bin
rename to tests/test_example.py
GIT binary patch
"""
        self.assertEqual(self.selector.select(parse_patch(patch)), 0b111)

    def test_module_docstring_change_selects_module_users(self):
        record = {
            "n": 1,
            "lmap": [0, 1],
            "regs": [None, [2, 1, 1, 0, (), "", 2, ()]],
            "touch": 0b101,
        }
        self.assertEqual(self.selector.line_sel("sqlglot/example.py", record, 1), 0b101)

    def test_sql_comment_change_selects_fixture_readers(self):
        patch = """\
diff --git a/tests/fixtures/example.sql b/tests/fixtures/example.sql
--- a/tests/fixtures/example.sql
+++ b/tests/fixtures/example.sql
@@ -1 +1 @@
-SELECT 1 -- previous
+SELECT 1 -- updated
"""
        self.assertEqual(self.selector.select(parse_patch(patch)), 0b011)

    def test_comment_only_sql_fixture_change_selects_readers(self):
        patch = """\
diff --git a/tests/fixtures/example.sql b/tests/fixtures/example.sql
--- a/tests/fixtures/example.sql
+++ b/tests/fixtures/example.sql
@@ -1 +1 @@
--- previous
+-- updated
"""
        self.assertEqual(self.selector.select(parse_patch(patch)), 0b011)

    def test_unmapped_tests_selected_even_with_no_indexed_hits(self):
        self.assertEqual(remap_bits(0, [2, None, 0], 0b010), 0b010)
        self.assertEqual(remap_bits(0b101, [2, None, 0], 0b010), 0b111)


if __name__ == "__main__":
    unittest.main()
