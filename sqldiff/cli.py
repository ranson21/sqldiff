"""Command line entry point.

Exit codes are meant to be useful in CI:

* 0 -- the two queries returned identical result sets
* 1 -- they differ
* 2 -- the comparison could not be run (bad SQL, connection failure, mismatched
       column shape)
"""

from __future__ import annotations

import argparse
import json
import sys

from . import compare as C
from .db import SessionError, open_session

EXIT_SAME, EXIT_DIFFERENT, EXIT_ERROR = 0, 1, 2


def read_sql(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="sqldiff",
        description="Run two SQL queries and prove they return the same rows.",
    )
    p.add_argument("--before", required=True, metavar="PATH", help="path to the original query")
    p.add_argument("--after", required=True, metavar="PATH", help="path to the refactored query")
    p.add_argument("--dsn", default=None,
                   help="Postgres connection string. Defaults to standard PG* environment variables.")
    p.add_argument("--exclude", action="append", default=[], metavar="COLUMN",
                   help="ignore this column (repeatable); for volatile values like timestamps")
    p.add_argument("--round", dest="round_digits", type=int, default=None, metavar="N",
                   help="round float/numeric columns to N decimal places before comparing")
    p.add_argument("--ignore-column-names", action="store_true",
                   help="compare positionally, tolerating renamed columns")
    p.add_argument("--sample", type=int, default=5, metavar="N",
                   help="how many differing rows to show per side (default 5)")
    p.add_argument("--backend", choices=("auto", "psql", "psycopg"), default="auto",
                   help="force a database backend (default: auto-detect)")
    p.add_argument("--json", action="store_true", help="emit machine-readable output")
    return p.parse_args(argv)


def run(args):
    session = open_session(args.dsn, None if args.backend == "auto" else args.backend)

    before_sql = read_sql(args.before)
    after_sql = read_sql(args.after)

    # Round one: define both views and read back their column shapes. The temp
    # views do not survive the session, so round two redefines them -- cheap,
    # since a view definition materialises nothing.
    plan = C.build_plan(before_sql, after_sql)
    results = session.run(plan)
    before_cols = [(r[0], r[1]) for r in results[2].rows]
    after_cols = [(r[0], r[1]) for r in results[3].rows]

    if not before_cols or not after_cols:
        raise SessionError("could not read column metadata; is each file a single SELECT?")

    problems = C.compare_columns(before_cols, after_cols, args.ignore_column_names)
    if problems:
        return {"status": "shape_mismatch", "problems": problems,
                "before_columns": before_cols, "after_columns": after_cols}

    proj = C.projection(before_cols, args.exclude, args.round_digits)

    plan2 = [
        C.create_view(C.BEFORE_VIEW, before_sql),
        C.create_view(C.AFTER_VIEW, after_sql),
        C.counts_sql(),
        C.diff_counts_sql(proj),
        C.sample_sql(proj, "before", args.sample),
        C.sample_sql(proj, "after", args.sample),
    ]
    r = session.run(plan2)

    before_rows, after_rows = (int(v) for v in r[2].scalar_row())
    missing, extra = (int(v) for v in r[3].scalar_row())

    return {
        "status": "same" if (missing == 0 and extra == 0) else "different",
        "before_rows": before_rows,
        "after_rows": after_rows,
        "missing_from_after": missing,
        "extra_in_after": extra,
        "compared_columns": [name for name, _ in before_cols
                             if name.lower() not in {e.lower() for e in args.exclude}],
        "sample_missing": {"header": r[4].header, "rows": r[4].rows},
        "sample_extra": {"header": r[5].header, "rows": r[5].rows},
    }


def render_table(header, rows):
    if not rows:
        return "    (none)"
    widths = [len(h) for h in header]
    for row in rows:
        for i, cell in enumerate(row[:len(widths)]):
            widths[i] = max(widths[i], len(cell))
    out = ["    " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(header))]
    out.append("    " + "  ".join("-" * w for w in widths))
    for row in rows:
        out.append("    " + "  ".join(
            (cell if i < len(widths) else cell).ljust(widths[i] if i < len(widths) else 0)
            for i, cell in enumerate(row)))
    return "\n".join(out)


def render(report):
    if report["status"] == "shape_mismatch":
        lines = ["SHAPE MISMATCH -- the queries do not return the same columns", ""]
        lines += ["  * " + p for p in report["problems"]]
        lines.append("")
        lines.append("  Row comparison was not attempted.")
        return "\n".join(lines)

    same = report["status"] == "same"
    lines = []
    lines.append("IDENTICAL" if same else "DIFFERENT")
    lines.append("")
    lines.append("  before: %d rows" % report["before_rows"])
    lines.append("  after:  %d rows" % report["after_rows"])
    lines.append("  columns compared: %d" % len(report["compared_columns"]))

    if same:
        lines.append("")
        lines.append("  Both directions of EXCEPT ALL are empty: same rows, same multiplicities.")
        return "\n".join(lines)

    lines.append("")
    lines.append("  rows in before but not after: %d" % report["missing_from_after"])
    lines.append("  rows in after but not before: %d" % report["extra_in_after"])

    if report["before_rows"] and report["after_rows"] % max(report["before_rows"], 1) == 0 \
            and report["after_rows"] > report["before_rows"]:
        factor = report["after_rows"] // report["before_rows"]
        lines.append("")
        lines.append("  NOTE: after has exactly %dx the rows of before -- this is the"
                     " signature of join fan-out." % factor)

    lines.append("")
    lines.append("  Sample rows present only in BEFORE:")
    lines.append(render_table(report["sample_missing"]["header"], report["sample_missing"]["rows"]))
    lines.append("")
    lines.append("  Sample rows present only in AFTER:")
    lines.append(render_table(report["sample_extra"]["header"], report["sample_extra"]["rows"]))
    return "\n".join(lines)


def main(argv=None):
    args = parse_args(argv)
    try:
        report = run(args)
    except (SessionError, ValueError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return EXIT_ERROR

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render(report))

    if report["status"] == "shape_mismatch":
        return EXIT_ERROR
    return EXIT_SAME if report["status"] == "same" else EXIT_DIFFERENT


if __name__ == "__main__":
    sys.exit(main())
