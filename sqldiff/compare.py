"""Differential comparison of two SQL queries.

The queries are wrapped in temp views, introspected for their column lists, and
then compared server-side in three layers:

1. row counts
2. multiplicity-preserving symmetric difference (``EXCEPT ALL`` both ways)
3. sample rows from each side of the difference, for diagnosis

Layer 2 is the point of the whole tool. Plain ``EXCEPT`` deduplicates, so it
happily passes a refactor whose joins now fan out and return every row three
times -- the exact failure mode a large query rewrite is most exposed to.
``EXCEPT ALL`` preserves multiplicity and catches it.

Two Postgres-specific details are handled for you:

* ``json`` has no equality operator and would make ``EXCEPT ALL`` fail outright;
  it is cast to ``jsonb``, which also makes the comparison key-order independent.
  ``xml`` is likewise cast to ``text``.
* floating point columns can be rounded via ``round_digits`` so that noise below
  the significant digits does not read as a behavioural change.
"""

from __future__ import annotations

BEFORE_VIEW = "_sqldiff_before"
AFTER_VIEW = "_sqldiff_after"

# Types with no usable equality operator for set operations, and what to cast to.
CAST_FOR_COMPARISON = {"json": "jsonb", "xml": "text"}

# Types worth rounding when the caller asks for a float tolerance.
INEXACT_TYPES = {"double precision", "real", "numeric"}


def quote_ident(name):
    return '"' + name.replace('"', '""') + '"'


def strip_trailing_semicolon(sql):
    return sql.strip().rstrip(";").strip()


def create_view(view, sql):
    return "CREATE TEMP VIEW %s AS %s" % (view, strip_trailing_semicolon(sql))


def introspect(view):
    """Column name, type and position for a temp view, in declaration order."""
    return (
        "SELECT a.attname, format_type(a.atttypid, a.atttypmod) AS type "
        "FROM pg_attribute a "
        "WHERE a.attrelid = '%s'::regclass AND a.attnum > 0 AND NOT a.attisdropped "
        "ORDER BY a.attnum" % view
    )


def base_type(formatted):
    """'numeric(10,2)' -> 'numeric', 'character varying(8)' -> 'character varying'."""
    return formatted.split("(")[0].strip()


def projection(columns, exclude=(), round_digits=None):
    """Build the select list used on both sides of the comparison.

    ``columns`` is a list of (name, formatted_type). Excluded columns are
    dropped; json/xml are cast to something comparable; inexact numerics are
    rounded when a tolerance was requested.
    """
    excluded = {c.lower() for c in exclude}
    parts = []
    for name, formatted in columns:
        if name.lower() in excluded:
            continue
        ident = quote_ident(name)
        btype = base_type(formatted)
        if btype in CAST_FOR_COMPARISON:
            expr = "%s::%s" % (ident, CAST_FOR_COMPARISON[btype])
        elif round_digits is not None and btype in INEXACT_TYPES:
            expr = "round(%s::numeric, %d)" % (ident, round_digits)
        else:
            expr = ident
        parts.append("%s AS %s" % (expr, ident) if expr != ident else ident)
    if not parts:
        raise ValueError("every column was excluded; nothing left to compare")
    return ", ".join(parts)


def counts_sql():
    return (
        "SELECT (SELECT count(*) FROM %s) AS before_rows, "
        "(SELECT count(*) FROM %s) AS after_rows" % (BEFORE_VIEW, AFTER_VIEW)
    )


def _diff_cte(proj):
    return (
        "WITH b AS (SELECT %s FROM %s), "
        "a AS (SELECT %s FROM %s), "
        "only_b AS (SELECT * FROM b EXCEPT ALL SELECT * FROM a), "
        "only_a AS (SELECT * FROM a EXCEPT ALL SELECT * FROM b) "
        % (proj, BEFORE_VIEW, proj, AFTER_VIEW)
    )


def diff_counts_sql(proj):
    return _diff_cte(proj) + (
        "SELECT (SELECT count(*) FROM only_b) AS missing_from_after, "
        "(SELECT count(*) FROM only_a) AS extra_in_after"
    )


def sample_sql(proj, side, limit):
    """Sample rows present on one side of the difference only."""
    cte = "only_b" if side == "before" else "only_a"
    return _diff_cte(proj) + "SELECT * FROM %s LIMIT %d" % (cte, limit)


def compare_columns(before_cols, after_cols, ignore_names=False):
    """Structural check before any row comparison.

    ``EXCEPT ALL`` matches columns positionally, so a column count or type
    mismatch has to be reported as its own failure -- otherwise the database
    raises a type error and the result looks like a tool bug rather than a real
    difference between the queries.
    """
    problems = []
    if len(before_cols) != len(after_cols):
        problems.append(
            "column count differs: before has %d, after has %d"
            % (len(before_cols), len(after_cols))
        )
        return problems

    for i, ((bname, btype), (aname, atype)) in enumerate(zip(before_cols, after_cols), 1):
        if not ignore_names and bname != aname:
            problems.append("column %d name differs: %r vs %r" % (i, bname, aname))
        if base_type(btype) != base_type(atype):
            problems.append(
                "column %d (%s) type differs: %s vs %s" % (i, bname, btype, atype)
            )
    return problems


def build_plan(before_sql, after_sql):
    """Statements 0-3: create both views, then introspect both."""
    return [
        create_view(BEFORE_VIEW, before_sql),
        create_view(AFTER_VIEW, after_sql),
        introspect(BEFORE_VIEW),
        introspect(AFTER_VIEW),
    ]
