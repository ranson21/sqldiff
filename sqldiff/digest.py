"""Summarise a large SQL query into something small enough to paste.

The economics this exists for: an AI assistant charges you to *read*. A
1000-line query is roughly 12k tokens, and an agent loop re-sends it every
turn. Most questions you want to ask about that query -- what does it touch,
where does this column come from, what joins could be multiplying rows -- are
structural facts a parser can answer exactly, for free.

So parse the query, emit a compact structural digest, and spend the assistant's
context on the judgement call instead of the transcription.

Pairs with the row-level verification in ``sqldiff.cli``: digest to decide what
to change, ``sqldiff`` to prove the change was safe.
"""

from __future__ import annotations

import argparse
import sys

try:
    import sqlglot
    from sqlglot import exp
    from sqlglot.lineage import lineage as _lineage
except ImportError:  # pragma: no cover - dependency guidance
    sqlglot = None


AGGREGATES = (exp.Sum, exp.Avg, exp.Count, exp.Min, exp.Max)

# Report queries routinely select hundreds of derived columns; listing them
# all costs tokens without adding understanding.
MAX_COLUMNS_SHOWN = 40


def estimate_tokens(text):
    """Rough token count. Four characters per token is the usual approximation
    and is close enough to size a prompt; it is not tokeniser-exact."""
    return max(1, len(text) // 4)


def cte_names(ast):
    return [c.alias for c in ast.find_all(exp.CTE)]


def base_tables(ast):
    """Real tables, with CTE self-references filtered out."""
    ctes = set(cte_names(ast))
    return sorted({t.name for t in ast.find_all(exp.Table)} - ctes)


def cte_graph(ast):
    """Which CTEs each CTE reads from, in declaration order."""
    names = set(cte_names(ast))
    graph = []
    for cte in ast.find_all(exp.CTE):
        refs = sorted({t.name for t in cte.this.find_all(exp.Table)} & names - {cte.alias})
        graph.append((cte.alias, refs))
    return graph


def join_summary(ast):
    """Every join, with its type and the columns it joins on.

    This is the part worth reading closely in a refactor: a join added to reach
    one extra column is the standard way a rewrite starts multiplying rows.
    """
    joins = []
    for join in ast.find_all(exp.Join):
        target = join.this
        if isinstance(target, exp.Table):
            name = target.name
        elif isinstance(target, exp.Subquery):
            name = target.alias or "<subquery>"
        else:
            name = target.sql()[:40]

        side = (join.side or "").upper()
        kind = (join.kind or "").upper()
        label = " ".join(p for p in (side, kind) if p) or "JOIN"

        on = join.args.get("on")
        keys, equi = [], False
        if on:
            for eq in on.find_all(exp.EQ):
                equi = True
                keys.append("%s = %s" % (eq.left.sql(), eq.right.sql()))
            if not equi:
                # A join with no equality condition is a range or cartesian
                # join. Show it in full -- it is the most expensive thing a
                # reader can miss.
                keys.append(on.sql())
        joins.append((label, name, keys, equi))
    return joins


def output_columns(ast):
    if not isinstance(ast, exp.Select):
        return []
    return [e.alias_or_name or e.sql()[:30] for e in ast.selects]


def filters(ast):
    """Top-level and CTE-level WHERE predicates, deduplicated."""
    seen, out = set(), []
    for where in ast.find_all(exp.Where):
        text = where.this.sql()
        if text not in seen:
            seen.add(text)
            out.append(text)
    return out


def risk_flags(ast):
    """Structural patterns worth a second look.

    These are heuristics, not proofs. They are here to tell you *where* to aim
    attention in a long query, and each one is a question rather than a verdict.
    """
    flags = []

    non_equi = [(label, name) for label, name, keys, equi in join_summary(ast)
                if keys and not equi]
    for label, name in non_equi:
        flags.append(
            "%s %s joins on an inequality, not an equality -- every left row can "
            "match many right rows" % (label, name))

    has_join = any(True for _ in ast.find_all(exp.Join))
    aggs = [a for a in ast.find_all(AGGREGATES)
            if not (isinstance(a, exp.Count) and a.args.get("distinct"))]
    if has_join and aggs:
        kinds = sorted({type(a).__name__.upper() for a in aggs})
        flags.append(
            "aggregates (%s) computed over joined rows -- if any join is 1:N, "
            "these are summing duplicates" % ", ".join(kinds))

    if any(isinstance(e, exp.Star) for s in ast.find_all(exp.Select) for e in s.selects):
        flags.append("SELECT * -- output shape depends on table definitions")

    distinct_selects = [s for s in ast.find_all(exp.Select) if s.args.get("distinct")]
    if distinct_selects and has_join:
        flags.append("DISTINCT alongside joins -- sometimes a patch over fan-out "
                     "rather than an intent")

    for sub in ast.find_all(exp.Subquery):
        if sub.find(exp.Column) and sub.parent and isinstance(sub.parent, exp.Where):
            flags.append("subquery in WHERE -- check for correlation and NULL semantics")
            break

    for node in ast.find_all(exp.Not):
        if node.find(exp.In):
            flags.append("NOT IN -- returns no rows if the subquery yields any NULL")
            break

    return flags


def build_digest(sql, dialect="postgres", lineage_for=()):
    ast = sqlglot.parse_one(sql, dialect=dialect)

    lines = []
    add = lines.append

    original_tokens = estimate_tokens(sql)
    add("# Query digest")
    add("")
    add("source: %d lines, ~%d tokens" % (len(sql.splitlines()), original_tokens))

    ctes = cte_names(ast)
    joins = join_summary(ast)
    add("structure: %d CTEs, %d joins, %d base tables"
        % (len(ctes), len(joins), len(base_tables(ast))))
    add("")

    add("## Base tables")
    for t in base_tables(ast):
        add("- %s" % t)
    add("")

    if ctes:
        add("## CTE dependencies")
        for name, refs in cte_graph(ast):
            add("- %s%s" % (name, (" <- " + ", ".join(refs)) if refs else " (reads base tables)"))
        add("")

    if joins:
        add("## Joins")
        for label, name, keys, equi in joins:
            mark = "" if equi or not keys else "   <-- no equality condition"
            add("- %s %s%s%s" % (label, name,
                                 ("  ON " + " AND ".join(keys)) if keys else "", mark))
        add("")

    cols = output_columns(ast)
    if cols:
        add("## Output columns (%d)" % len(cols))
        # A very wide SELECT is common in report queries and its full list is
        # rarely what you need; the count plus a sample carries the shape.
        shown = cols[:MAX_COLUMNS_SHOWN]
        add(", ".join(shown))
        if len(cols) > MAX_COLUMNS_SHOWN:
            add("... and %d more" % (len(cols) - MAX_COLUMNS_SHOWN))
        add("")

    preds = filters(ast)
    if preds:
        add("## Filters")
        for p in preds:
            add("- %s" % p)
        add("")

    for col in lineage_for:
        add("## Lineage: %s" % col)
        try:
            node = _lineage(col, sql, dialect=dialect)
            for line in _render_lineage(node):
                add(line)
        except Exception as exc:
            add("  (could not trace: %s)" % type(exc).__name__)
        add("")

    flags = risk_flags(ast)
    if flags:
        add("## Worth checking")
        for f in flags:
            add("- %s" % f)
        add("")

    digest = "\n".join(lines).rstrip() + "\n"
    saving = 100 - (estimate_tokens(digest) * 100 // max(original_tokens, 1))
    digest += "\n<!-- digest ~%d tokens vs ~%d for the query: %d%% smaller -->\n" % (
        estimate_tokens(digest), original_tokens, saving)
    return digest


def _render_lineage(node, depth=0, seen=None):
    seen = seen if seen is not None else set()
    if id(node) in seen or depth > 6:
        return []
    seen.add(id(node))
    out = ["  " * depth + "- " + (node.name or "?")]
    for child in node.downstream:
        out.extend(_render_lineage(child, depth + 1, seen))
    return out


def structural_diff(before_sql, after_sql, dialect="postgres"):
    """An edit list between two versions, instead of both full queries."""
    before = sqlglot.parse_one(before_sql, dialect=dialect)
    after = sqlglot.parse_one(after_sql, dialect=dialect)
    changes = sqlglot.diff(before, after)

    interesting = [c for c in changes if type(c).__name__ != "Keep"]
    lines = ["# Structural diff", "",
             "%d structural edits (%d nodes unchanged)"
             % (len(interesting), len(changes) - len(interesting)), ""]
    for c in interesting:
        kind = type(c).__name__
        node = getattr(c, "expression", None) or getattr(c, "target", None)
        text = node.sql()[:100] if node is not None else ""
        lines.append("- %-8s %s" % (kind, text))
    return "\n".join(lines).rstrip() + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="sqldiff-digest",
        description="Summarise a SQL query structurally, so an assistant reads "
                    "a digest instead of the whole thing.")
    p.add_argument("query", help="path to the SQL file")
    p.add_argument("--dialect", default="postgres")
    p.add_argument("--lineage", action="append", default=[], metavar="COLUMN",
                   help="trace where an output column comes from (repeatable)")
    p.add_argument("--diff", metavar="PATH",
                   help="structurally diff against another query instead of digesting")
    args = p.parse_args(argv)

    if sqlglot is None:
        print("error: sqlglot is required.\n  pip install sqlglot\n"
              "  (or: python3 -m venv .venv && .venv/bin/pip install sqlglot)",
              file=sys.stderr)
        return 2

    try:
        sql = open(args.query, encoding="utf-8").read()
        if args.diff:
            print(structural_diff(sql, open(args.diff, encoding="utf-8").read(), args.dialect))
        else:
            print(build_digest(sql, args.dialect, args.lineage))
    except OSError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    except Exception as exc:
        print("error: could not parse %s: %s" % (args.query, exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
