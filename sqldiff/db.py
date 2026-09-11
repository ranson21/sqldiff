"""Database session backends.

Two backends, selected automatically:

* ``psycopg`` (v3 or v2) when it is importable -- typed results, one connection.
* the ``psql`` client driven as a subprocess -- a zero-dependency fallback for
  locked-down machines where PyPI is unavailable.

Both run every statement in ONE session, which matters: the comparison builds
temp views and later statements have to still see them.

Results are normalised to strings on both backends. That is display-only and
deliberate -- every correctness-bearing comparison happens server-side, so
client-side type fidelity never affects a verdict.
"""

from __future__ import annotations

import csv
import io
import os
import subprocess
import tempfile

MARKER = "###SQLDIFF-SECTION###"


class SessionError(RuntimeError):
    pass


class Result:
    """One statement's output: a header and its rows, both as strings."""

    def __init__(self, header=None, rows=None):
        self.header = header or []
        self.rows = rows or []

    def scalar_row(self):
        if not self.rows:
            raise SessionError("expected a row, got none")
        return self.rows[0]

    def __repr__(self):
        return "Result(header=%r, rows=%d)" % (self.header, len(self.rows))


class PsqlSession:
    """Runs a batch of statements through a single ``psql`` invocation."""

    def __init__(self, dsn=None, psql="psql"):
        self.dsn = dsn
        self.psql = psql

    def run(self, statements):
        script = io.StringIO()
        for stmt in statements:
            script.write("\\echo %s\n" % MARKER)
            script.write(stmt.rstrip().rstrip(";") + ";\n")

        with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as fh:
            fh.write(script.getvalue())
            path = fh.name

        cmd = [self.psql, "-v", "ON_ERROR_STOP=1", "-q", "--csv", "-f", path]
        if self.dsn:
            cmd.insert(1, self.dsn)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
        finally:
            os.unlink(path)

        if proc.returncode != 0:
            raise SessionError(proc.stderr.strip() or "psql exited %d" % proc.returncode)

        return self._parse(proc.stdout, len(statements))

    @staticmethod
    def _parse(stdout, expected):
        chunks = stdout.split(MARKER)[1:]
        results = []
        for chunk in chunks:
            lines = [ln for ln in chunk.splitlines() if ln.strip()]
            if not lines:
                results.append(Result())
                continue
            parsed = list(csv.reader(lines))
            results.append(Result(header=parsed[0], rows=parsed[1:]))
        while len(results) < expected:
            results.append(Result())
        return results


class PsycopgSession:
    """Runs the same batch over a single psycopg connection."""

    def __init__(self, dsn=None, driver=None):
        self.dsn = dsn
        self.driver = driver

    def run(self, statements):
        conn = self.driver.connect(self.dsn) if self.dsn else self.driver.connect("")
        results = []
        try:
            with conn.cursor() as cur:
                for stmt in statements:
                    cur.execute(stmt)
                    if cur.description is None:
                        results.append(Result())
                        continue
                    header = [d[0] for d in cur.description]
                    rows = [["" if v is None else str(v) for v in row] for row in cur.fetchall()]
                    results.append(Result(header=header, rows=rows))
        finally:
            conn.rollback()
            conn.close()
        return results


def open_session(dsn=None, force_backend=None):
    """Pick a backend. Honours ``force_backend`` of 'psql' or 'psycopg'."""
    if force_backend == "psql":
        return PsqlSession(dsn)
    if force_backend != "psql":
        for name in ("psycopg", "psycopg2"):
            try:
                driver = __import__(name)
            except ImportError:
                continue
            return PsycopgSession(dsn, driver)
    if force_backend == "psycopg":
        raise SessionError("psycopg requested but neither psycopg nor psycopg2 is importable")
    return PsqlSession(dsn)
