"""Tests for the structural digest.

These need sqlglot but no database.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import sqlglot  # noqa: F401
    HAVE_SQLGLOT = True
except ImportError:
    HAVE_SQLGLOT = False

if HAVE_SQLGLOT:
    from sqldiff import digest as D

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MESSY = os.path.join(ROOT, "fixtures", "messy_report.sql")


@unittest.skipUnless(HAVE_SQLGLOT, "sqlglot not installed")
class TestStructure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = open(MESSY, encoding="utf-8").read()
        cls.ast = __import__("sqlglot").parse_one(cls.sql, dialect="postgres")

    def test_cte_names_are_found(self):
        self.assertIn("regional_rollup", D.cte_names(self.ast))

    def test_cte_names_are_excluded_from_base_tables(self):
        # A CTE reference parses as a Table; counting it as a base table would
        # misreport what the query actually reads from.
        tables = D.base_tables(self.ast)
        self.assertIn("customers", tables)
        self.assertNotIn("regional_rollup", tables)
        self.assertNotIn("combined", tables)

    def test_cte_dependencies_are_resolved(self):
        graph = dict(D.cte_graph(self.ast))
        self.assertEqual(graph["regional_rollup"], ["combined"])
        self.assertIn("order_base", graph["combined"])

    def test_equi_joins_report_their_keys(self):
        joins = {name: (keys, equi) for _, name, keys, equi in D.join_summary(self.ast)}
        keys, equi = joins["products"]
        self.assertTrue(equi)
        self.assertIn("p.sku = oi.sku", keys)

    def test_non_equi_join_is_marked(self):
        joins = {name: equi for _, name, _, equi in D.join_summary(self.ast)}
        self.assertFalse(joins["manager_rollup"])

    def test_output_columns_come_from_the_outer_select(self):
        cols = D.output_columns(self.ast)
        self.assertIn("line_to_gross_ratio", cols)
        self.assertNotIn("ticket_count", cols)


@unittest.skipUnless(HAVE_SQLGLOT, "sqlglot not installed")
class TestRiskFlags(unittest.TestCase):
    def flags_for(self, sql):
        return " ".join(D.risk_flags(__import__("sqlglot").parse_one(sql, dialect="postgres")))

    def test_inequality_join_is_flagged(self):
        f = self.flags_for("SELECT a.x FROM a JOIN b ON a.v > b.v")
        self.assertIn("inequality", f)

    def test_equi_join_alone_is_not_flagged_as_inequality(self):
        f = self.flags_for("SELECT a.x FROM a JOIN b ON a.id = b.a_id")
        self.assertNotIn("inequality", f)

    def test_aggregate_over_join_is_flagged(self):
        f = self.flags_for("SELECT sum(a.v) FROM a JOIN b ON a.id = b.a_id")
        self.assertIn("summing duplicates", f)

    def test_aggregate_without_join_is_not_flagged(self):
        f = self.flags_for("SELECT sum(v) FROM a")
        self.assertNotIn("summing duplicates", f)

    def test_not_in_is_flagged_for_null_semantics(self):
        f = self.flags_for("SELECT x FROM a WHERE x NOT IN (SELECT y FROM b)")
        self.assertIn("NOT IN", f)

    def test_select_star_is_flagged(self):
        self.assertIn("SELECT *", self.flags_for("SELECT * FROM a"))


@unittest.skipUnless(HAVE_SQLGLOT, "sqlglot not installed")
class TestDigestOutput(unittest.TestCase):
    def test_digest_is_substantially_smaller_than_the_query(self):
        sql = open(MESSY, encoding="utf-8").read()
        out = D.build_digest(sql)
        self.assertLess(D.estimate_tokens(out), D.estimate_tokens(sql))

    def test_wide_select_lists_are_truncated(self):
        cols = ", ".join("c%d AS name%d" % (i, i) for i in range(200))
        out = D.build_digest("SELECT %s FROM t" % cols)
        self.assertIn("and %d more" % (200 - D.MAX_COLUMNS_SHOWN), out)

    def test_lineage_traces_through_ctes(self):
        sql = ("WITH a AS (SELECT o.total AS t FROM orders o) "
               "SELECT sum(t) AS revenue FROM a")
        out = D.build_digest(sql, lineage_for=["revenue"])
        self.assertIn("## Lineage: revenue", out)

    def test_structural_diff_reports_an_added_join(self):
        before = "SELECT c.id FROM customers c"
        after = "SELECT c.id FROM customers c JOIN orders o ON o.customer_id = c.id"
        out = D.structural_diff(before, after)
        self.assertIn("Insert", out)
        self.assertIn("structural edits", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
