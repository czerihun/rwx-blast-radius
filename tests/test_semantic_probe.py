import ast
import unittest

from tools import semantic_probe
from tools.evaluate_semantic_probe import observed_matches


class SemanticProbeTests(unittest.TestCase):
    def test_detects_single_identity_to_equality_change(self):
        old = ast.parse("def f(x):\n    return x is None\n")
        new = ast.parse("def f(x):\n    return x == None\n")
        self.assertEqual(semantic_probe.operator_changes(old, new), [(2, 11, "Is")])

    def test_rejects_other_changes_and_chained_comparisons(self):
        old = ast.parse("def f(x):\n    return x is None\n")
        self.assertIsNone(semantic_probe.operator_changes(old, ast.parse("def f(x):\n    return x == 1\n")))
        chained = ast.parse("def f(x, y):\n    return x is None is y\n")
        modified = ast.parse("def f(x, y):\n    return x == None is y\n")
        self.assertIsNone(semantic_probe.operator_changes(chained, modified))

    def test_probe_does_not_invoke_custom_equality(self):
        class SideEffect:
            def __eq__(self, other):
                raise AssertionError("must not invoke arbitrary equality")

        semantic_probe._result = {}
        semantic_probe._current = "test"
        self.assertFalse(semantic_probe._compare(SideEffect(), None, "case", "Is"))
        self.assertEqual(semantic_probe._result["case"]["test"], [1, 0, 1])

    def test_probe_records_builtin_identity_equality_difference(self):
        semantic_probe._result = {}
        semantic_probe._current = "test"
        self.assertFalse(semantic_probe._compare(1, True, "case", "Is"))
        self.assertEqual(semantic_probe._result["case"]["test"], [1, 1, 0])

    def test_counterfactual_requires_observed_matching_comparisons(self):
        observations = {"match": [2, 0, 0], "different": [2, 1, 0],
                        "unknown": [2, 0, 1], "empty": [0, 0, 0]}
        self.assertEqual(observed_matches(
            ["match", "different", "unknown", "empty", "missing"], observations
        ), {"match"})


if __name__ == "__main__":
    unittest.main()
