"""Tests for the `validation` expression language.

Run with `python3 -m unittest discover tests` from the repository root.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wrappers.expression import ExpressionError, compile_expression  # noqa: E402


LIST_VARS = ("files", "dirs", "count")
LOG_VARS = ("log", "lines")


def check(source, variables, allowed=LIST_VARS):
    """Compile `source` and evaluate it against `variables`."""
    return compile_expression(source, allowed, "Test: 'validation'").evaluate(variables)


class MembershipTests(unittest.TestCase):
    def test_membership(self):
        self.assertTrue(check("'a.log' in {files}", {"files": ["a.log", "b.log"]}))
        self.assertFalse(check("'c.log' in {files}", {"files": ["a.log"]}))

    def test_not_in(self):
        self.assertTrue(check("'BOOT.CFG' not in {files}", {"files": ["a.log"]}))

    def test_len_and_boolean(self):
        variables = {"files": ["a.log", "b.log"], "dirs": []}
        self.assertTrue(check("len({files}) == 2 and len({dirs}) == 0", variables))

    def test_comparison(self):
        self.assertTrue(check("{count} >= 192", {"count": 200}))
        self.assertFalse(check("{count} >= 192", {"count": 3}))

    def test_comprehension_over_method(self):
        variables = {"files": ["a.log", "b.txt"]}
        self.assertTrue(check("any(f.endswith('.log') for f in {files})", variables))
        self.assertFalse(check("all(f.endswith('.log') for f in {files})", variables))

    def test_list_literal(self):
        self.assertTrue(check("{count} in [1, 2, 3]", {"count": 2}))


class RegexTests(unittest.TestCase):
    def test_matches(self):
        self.assertTrue(check("matches({log}, 'unix=[0-9]+')", {"log": "x unix=17 y"}, LOG_VARS))

    def test_regex_quantifier_braces_survive(self):
        """A `{4}` quantifier is a string literal, not a variable reference."""
        expression = "matches({log}, 'unix=[0-9]{4}')"
        self.assertTrue(check(expression, {"log": "unix=1760"}, LOG_VARS))
        self.assertFalse(check(expression, {"log": "unix=17"}, LOG_VARS))

    def test_brace_inside_string_literal_is_not_a_variable(self):
        """A device logging JSON may legitimately contain `{count}` as text."""
        self.assertTrue(check("'{count}' in {log}", {"log": 'tpl {count} here'}, LOG_VARS))

    def test_invalid_regex_is_reported(self):
        with self.assertRaises(ExpressionError):
            check("matches({log}, '[')", {"log": "x"}, LOG_VARS)


class RawStringTests(unittest.TestCase):
    def test_raw_string_pattern_works(self):
        self.assertTrue(check('matches({log}, r"unix=\\S+")', {"log": "unix=abc"}, LOG_VARS))

    def test_unescaped_backslash_is_rejected_with_guidance(self):
        with self.assertRaisesRegex(ExpressionError, "raw string"):
            compile_expression('matches({log}, "unix=\\S+")', LOG_VARS, "Test: 'validation'")


class SimpleComparisonTests(unittest.TestCase):
    """The shape !MqttExpect needs to keep deciding a count before its window ends."""

    def test_simple_comparison_detected(self):
        expression = compile_expression("{count} == 192", LIST_VARS, "Test")
        self.assertEqual(expression.as_simple_comparison(), ("count", "==", 192))

    def test_compound_expression_is_not_simple(self):
        expression = compile_expression("{count} == 3 and len({files}) > 0", LIST_VARS, "Test")
        self.assertIsNone(expression.as_simple_comparison())

    def test_call_on_the_left_is_not_simple(self):
        expression = compile_expression("len({files}) >= 3", LIST_VARS, "Test")
        self.assertIsNone(expression.as_simple_comparison())


class MatchingTests(unittest.TestCase):
    """`in` is exact membership; a glob needs matching()."""

    DIRS = {"dirs": [".Trash-1000", "session_54", "session_55", "logs"]}
    VARS = ("dirs", "files", "count")

    def test_a_glob_literal_is_not_membership(self):
        """The trap: 'session_*' in {dirs} reads as a pattern and is not one."""
        self.assertFalse(check("'session_*' in {dirs}", self.DIRS, self.VARS))

    def test_matching_finds_them(self):
        self.assertTrue(check("matching('session_*', {dirs})", self.DIRS, self.VARS))

    def test_empty_result_is_falsey(self):
        self.assertFalse(check("matching('nothing_*', {dirs})", self.DIRS, self.VARS))

    def test_matching_composes_with_len(self):
        self.assertTrue(check("len(matching('session_*', {dirs})) == 2", self.DIRS, self.VARS))

    def test_matching_is_case_sensitive_like_the_path_globs(self):
        self.assertFalse(check("matching('SESSION_*', {dirs})", self.DIRS, self.VARS))

    def test_question_mark_wildcard(self):
        self.assertTrue(check("len(matching('session_5?', {dirs})) == 2", self.DIRS, self.VARS))


class RejectionTests(unittest.TestCase):
    def test_bare_variable_name_rejected(self):
        with self.assertRaisesRegex(ExpressionError, r"write '\{files\}'"):
            compile_expression("'a' in files", LIST_VARS, "Test: 'validation'")

    def test_unknown_variable_rejected(self):
        with self.assertRaisesRegex(ExpressionError, "unknown variable"):
            compile_expression("'a' in {fils}", LIST_VARS, "Test: 'validation'")

    def test_unknown_name_rejected(self):
        with self.assertRaisesRegex(ExpressionError, "unknown name 'os'"):
            compile_expression("os in {files}", LIST_VARS, "Test: 'validation'")

    def test_dunder_attribute_rejected(self):
        with self.assertRaisesRegex(ExpressionError, "not allowed"):
            compile_expression("{files}.__class__ == 1", LIST_VARS, "Test: 'validation'")

    def test_subclasses_escape_rejected(self):
        source = "len(''.__class__.__mro__[1].__subclasses__()) > 0 and len({files}) > 0"
        with self.assertRaises(ExpressionError):
            compile_expression(source, LIST_VARS, "Test: 'validation'")

    def test_lambda_rejected(self):
        with self.assertRaises(ExpressionError):
            compile_expression("(lambda: len({files}))() == 0", LIST_VARS, "Test: 'validation'")

    def test_walrus_rejected(self):
        with self.assertRaises(ExpressionError):
            compile_expression("(n := len({files})) > 0", LIST_VARS, "Test: 'validation'")

    def test_underscore_variable_rejected(self):
        with self.assertRaisesRegex(ExpressionError, "underscore"):
            compile_expression("{_v_files} == 1", LIST_VARS, "Test: 'validation'")

    def test_unclosed_brace_rejected(self):
        with self.assertRaisesRegex(ExpressionError, "unclosed brace"):
            compile_expression("len({files) == 1", LIST_VARS, "Test: 'validation'")

    def test_stray_brace_rejected(self):
        with self.assertRaisesRegex(ExpressionError, "not a variable"):
            compile_expression("{count} == { }", LIST_VARS, "Test: 'validation'")

    def test_set_literal_rejected_with_guidance(self):
        with self.assertRaisesRegex(ExpressionError, "not a variable"):
            compile_expression("{count} in {1, 2}", LIST_VARS, "Test: 'validation'")

    def test_syntax_error_reported(self):
        with self.assertRaises(ExpressionError):
            compile_expression("len({files}) ==", LIST_VARS, "Test: 'validation'")


class MigrationTests(unittest.TestCase):
    def test_old_regex_syntax_rejected(self):
        with self.assertRaisesRegex(ExpressionError, "pre-expression syntax"):
            compile_expression("Journal cleared", LOG_VARS, "DutCli: 'validation'")

    def test_old_regex_with_metacharacters_rejected(self):
        with self.assertRaisesRegex(ExpressionError, "pre-expression syntax"):
            compile_expression(r"state:\s*connected", LOG_VARS, "DutCli: 'validation'")

    def test_old_count_syntax_gets_the_specific_message(self):
        """`count == 192` parses, so the bare-name error is more use than the migration one."""
        with self.assertRaisesRegex(ExpressionError, r"write '\{count\}'"):
            compile_expression("count == 192", ("count",), "MqttExpect: 'validation'")

    def test_hint_is_included(self):
        with self.assertRaisesRegex(ExpressionError, "matches"):
            compile_expression("x", LOG_VARS, "Test", hint='Try matches({log}, "x").')


class ReportingTests(unittest.TestCase):
    def test_describe_lists_only_used_variables(self):
        expression = compile_expression("len({files}) == 2", LIST_VARS, "Test: 'validation'")
        described = expression.describe({"files": ["a", "b"], "dirs": ["x"], "count": 2})
        self.assertEqual(described, "files=['a', 'b']")

    def test_describe_truncates_long_values(self):
        expression = compile_expression("len({log}) > 0", LOG_VARS, "Test: 'validation'")
        described = expression.describe({"log": "x" * 5000})
        self.assertIn("chars)", described)
        self.assertLess(len(described), 400)

    def test_missing_variable_is_a_wrapper_bug(self):
        expression = compile_expression("len({files}) == 0", LIST_VARS, "Test: 'validation'")
        with self.assertRaisesRegex(ExpressionError, "bug in the wrapper"):
            expression.evaluate({})


if __name__ == "__main__":
    unittest.main()
