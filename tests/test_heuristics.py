import unittest

from heuristics import (fresh_copy_change, identity_change, mapping_change, mapping_entry_change,
                        ordering_change, prune_identity, prune_mapping, prune_ordering)


class IdentityHeuristicTests(unittest.TestCase):
    def test_operator_classification(self):
        cases = [
            ("if item is None:", "if item == None:", "Is"),
            ("return obj is not marker", "return obj != marker", "IsNot"),
            ("return x is None", "return y == None", None),
            ('return "is" is x', 'return "is" == x', "Is"),
            ("return x is None", "return x == 0", None),
            ("return x is None", "return x is None", None),
        ]
        for before, after, expected in cases:
            with self.subTest(before=before, after=after):
                self.assertEqual(identity_change(before, after), expected)

    def test_modes_retain_observed_differences(self):
        files = [{"old": "sqlglot/example.py", "status": "modified", "binary": False,
                  "groups": [([3], ["return x == None"], 2, ["return x is None"])]}]
        profile = {"sites": {"sqlglot/example.py": {3: [(4, "Is", 0b0111, 0b0010, 0b0100)]}}}
        self.assertEqual(prune_identity(files, 0b1111, profile, "observed"), 0b1110)
        self.assertEqual(prune_identity(files, 0b1111, profile, "optimistic"), 0b0110)
        self.assertEqual(prune_identity(files, 0b1111, profile, "type-optimistic"), 0b0010)

    def test_unmatched_and_multifile_patches_are_retained(self):
        f = {"old": "sqlglot/example.py", "status": "modified", "binary": False,
             "groups": [([3], ["return y == None"], 2, ["return x is None"])]}
        profile = {"sites": {"sqlglot/example.py": {3: [(4, "Is", 1, 0, 0)]}}}
        self.assertEqual(prune_identity([f], 3, profile, "optimistic"), 3)
        self.assertEqual(prune_identity([f, f], 3, profile, "optimistic"), 3)

    def test_ordered_comparison_retains_observed_boundary_values(self):
        self.assertEqual(ordering_change("if version < (1, 2):", "if version <= (1, 2):"), "Lt")
        self.assertIsNone(ordering_change("if version < (1, 2):", "if version <= (1, 3):"))
        f = {"old": "sqlglot/example.py", "status": "modified", "binary": False,
             "groups": [([3], ["if version <= (1, 2):"], 2, ["if version < (1, 2):"])]}
        profile = {"sites": {"sqlglot/example.py": {3: [(3, "Lt", 0b0011, 0b0010, 0)]}}}
        self.assertEqual(prune_ordering([f], 0b1111, profile, "observed"), 0b1110)
        self.assertEqual(prune_ordering([f], 0b1111, profile, "optimistic"), 0b0010)

    def test_copy_heuristic_requires_fresh_argument_and_only_flag_change(self):
        self.assertTrue(fresh_copy_change("x = f(build(), copy=False)", "x = f(build(), copy=True)"))
        self.assertFalse(fresh_copy_change("x = f(existing, copy=False)", "x = f(existing, copy=True)"))
        self.assertFalse(fresh_copy_change("x = f(build(), copy=False)", "x = g(build(), copy=True)"))


class MappingHeuristicTests(unittest.TestCase):
    def test_literal_mapping_classification(self):
        self.assertEqual(mapping_change("M = {'a': (1, 2), 'b': 3}",
                                        "M = {'a': (1, 4), 'b': 3}"), ("M", "a"))
        self.assertIsNone(mapping_change("M = {'a': 1}", "M = {'b': 1}"))
        self.assertEqual(mapping_change("M = {'a': x}", "M = {'a': 2}"), ("M", "a"))
        self.assertEqual(mapping_change("M = {**base, 'a': make()}",
                                        "M = {**base, 'a': other()}"), ("M", "a"))
        self.assertIsNone(mapping_change("M = {'a': 1}", "N = {'a': 2}"))
        self.assertIsNone(mapping_change("M = {'a': 1, 'a': 2}", "M = {'a': 1, 'a': 3}"))

    def test_mapping_modes_preserve_observed_key_and_whole_reads(self):
        f = {"old": "sqlglot/example.py", "status": "modified", "binary": False,
             "groups": [([3], ["M = {'a': 2, 'b': 0}"], 2,
                         ["M = {'a': 1, 'b': 0}"])]}
        profile = {"sites": {"sqlglot/example.py": {3: {
            "M": {"keys": {"a": 0b0001, "b": 0b0010}, "whole": 0b0100, "import_read": True}
        }}}}
        self.assertEqual(prune_mapping([f], 0b1111, profile, "observed"), 0b1101)
        self.assertEqual(prune_mapping([f], 0b1111, profile, "optimistic"), 0b0101)
        self.assertEqual(prune_mapping([f], 0b1111, profile, "import-guarded"), 0b1111)

    def test_multiline_literal_entry(self):
        f = {"old": "sqlglot/example.py", "status": "modified", "binary": False,
             "groups": [([5], ['"a": True,'], 4, ['"a": False,'])]}
        profile = {"sites": {"sqlglot/example.py": {3: {
            "M": {"keys": {"a": 0b0001, "b": 0b0010}, "whole": 0, "import_read": False}
        }}}, "entries": {"sqlglot/example.py": {5: [(3, "M", "a")]}}}
        self.assertEqual(prune_mapping([f], 0b1111, profile, "optimistic"), 0b0001)
        self.assertEqual(prune_mapping([f], 0b1111, profile, "import-guarded"), 0b0001)

    def test_non_dictionary_expressions_are_not_mapping_entries(self):
        self.assertIsNone(mapping_entry_change("value", "other"))
        self.assertIsNone(mapping_entry_change("'key': 1", "other"))


if __name__ == "__main__":
    unittest.main()
