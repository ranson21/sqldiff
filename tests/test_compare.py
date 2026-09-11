"""Unit tests for the SQL builders. These need no database.

The integration test at the bottom runs only when SQLDIFF_TEST_DSN is set.
"""

import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqldiff import compare as C


class TestProjection(unittest.TestCase):
    def test_plain_columns_are_quoted(self):
        proj = C.projection([("region", "text"), ("revenue", "numeric(10,2)")])
        self.assertEqual(proj, '"region", "revenue"')

    def test_reserved_and_mixed_case_names_survive(self):
        proj = C.projection([("Order", "text")])
        self.assertEqual(proj, '"Order"')

    def test_embedded_quote_is_escaped(self):
        proj = C.projection([('we"ird', "text")])
        self.assertEqual(proj, '"we""ird"')

    def test_json_is_cast_to_jsonb(self):
        # json has no equality operator; without this cast EXCEPT ALL errors out.
        proj = C.projection([("payload", "json")])
        self.assertEqual(proj, '"payload"::jsonb AS "payload"')

    def test_xml_is_cast_to_text(self):
        proj = C.projection([("doc", "xml")])
        self.assertEqual(proj, '"doc"::text AS "doc"')

    def test_rounding_applies_only_to_inexact_types(self):
        proj = C.projection(
            [("amount", "double precision"), ("id", "integer")], round_digits=2)
        self.assertEqual(proj, 'round("amount"::numeric, 2) AS "amount", "id"')

    def test_rounding_is_off_by_default(self):
        proj = C.projection([("amount", "double precision")])
        self.assertEqual(proj, '"amount"')

    def test_excluded_columns_are_dropped_case_insensitively(self):
        proj = C.projection([("a", "text"), ("RunAt", "timestamp")], exclude=["runat"])
        self.assertEqual(proj, '"a"')

    def test_excluding_everything_is_an_error(self):
        with self.assertRaises(ValueError):
            C.projection([("a", "text")], exclude=["a"])


class TestBaseType(unittest.TestCase):
    def test_parameterised_types_are_reduced(self):
        self.assertEqual(C.base_type("numeric(10,2)"), "numeric")
        self.assertEqual(C.base_type("character varying(8)"), "character varying")
        self.assertEqual(C.base_type("text"), "text")


class TestCompareColumns(unittest.TestCase):
    def test_identical_shapes_pass(self):
        cols = [("region", "text"), ("revenue", "numeric(10,2)")]
        self.assertEqual(C.compare_columns(cols, cols), [])

    def test_count_mismatch_short_circuits(self):
        problems = C.compare_columns([("a", "text")], [("a", "text"), ("b", "text")])
        self.assertEqual(len(problems), 1)
        self.assertIn("column count differs", problems[0])

    def test_renamed_column_is_flagged(self):
        problems = C.compare_columns([("a", "text")], [("b", "text")])
        self.assertTrue(any("name differs" in p for p in problems))

    def test_renamed_column_can_be_tolerated(self):
        problems = C.compare_columns([("a", "text")], [("b", "text")], ignore_names=True)
        self.assertEqual(problems, [])

    def test_type_mismatch_is_flagged_even_when_ignoring_names(self):
        problems = C.compare_columns(
            [("a", "text")], [("b", "integer")], ignore_names=True)
        self.assertTrue(any("type differs" in p for p in problems))

    def test_parameterised_type_widths_do_not_count_as_mismatch(self):
        problems = C.compare_columns(
            [("a", "numeric(10,2)")], [("a", "numeric(12,2)")])
        self.assertEqual(problems, [])


class TestSqlShape(unittest.TestCase):
    def test_diff_uses_except_all_in_both_directions(self):
        sql = C.diff_counts_sql('"a"')
        self.assertEqual(sql.count("EXCEPT ALL"), 2)
        self.assertNotIn("EXCEPT\n", sql)

    def test_trailing_semicolon_is_stripped_before_wrapping(self):
        self.assertTrue(C.create_view("v", "SELECT 1;\n").endswith("SELECT 1"))

    def test_sample_sql_selects_the_right_side(self):
        self.assertIn("FROM only_b", C.sample_sql('"a"', "before", 5))
        self.assertIn("FROM only_a", C.sample_sql('"a"', "after", 5))


DSN = os.environ.get("SQLDIFF_TEST_DSN")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


@unittest.skipUnless(DSN, "set SQLDIFF_TEST_DSN to run integration tests")
class TestAgainstRealDatabase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run(["psql", DSN, "-q", "-v", "ON_ERROR_STOP=1",
                        "-f", os.path.join(ROOT, "fixtures", "schema.sql")], check=True)

    def run_cli(self, after):
        return subprocess.run(
            [sys.executable, "-m", "sqldiff.cli",
             "--dsn", DSN,
             "--before", os.path.join(ROOT, "fixtures", "before.sql"),
             "--after", os.path.join(ROOT, "fixtures", after)],
            cwd=ROOT, capture_output=True, text=True)

    def test_equivalent_rewrite_reports_identical(self):
        proc = self.run_cli("after_equivalent.sql")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("IDENTICAL", proc.stdout)

    def test_fanout_is_caught(self):
        # The whole reason the tool exists: plain EXCEPT would pass this.
        proc = self.run_cli("after_fanout.sql")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("DIFFERENT", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
