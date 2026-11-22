import ast
import unittest

from tools.mapping_probe import mapping_change


class MappingProbeTests(unittest.TestCase):
    def test_detects_single_literal_mapping_value_change(self):
        old = ast.parse("class C:\n    options = {'a': (16, 2), 'b': True}\n")
        new = ast.parse("class C:\n    options = {'a': (17, 2), 'b': True}\n")
        self.assertEqual(mapping_change(old, new), {"name": "options", "key": "a", "line": 2})

    def test_rejects_other_changes_and_dynamic_values(self):
        old = ast.parse("class C:\n    options = {'a': 16}\n    flag = True\n")
        with_other_edit = ast.parse("class C:\n    options = {'a': 17}\n    flag = False\n")
        dynamic = ast.parse("class C:\n    options = {'a': f()}\n    flag = True\n")
        self.assertIsNone(mapping_change(old, with_other_edit))
        self.assertIsNone(mapping_change(old, dynamic))


if __name__ == "__main__":
    unittest.main()
