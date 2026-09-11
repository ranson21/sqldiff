# sqldiff

Prove that a refactored SQL query returns exactly the same rows as the original.

Built for the case where you have to restructure a long, load-bearing query and
need something stronger than reading it carefully twice.

## The problem it solves

The tempting check is `EXCEPT`:

```sql
SELECT * FROM old EXCEPT SELECT * FROM new;  -- empty, so we're fine?
```

You are not fine. `EXCEPT` deduplicates. A refactor that adds a join and makes
every row come back three times produces an **empty** `EXCEPT` in both
directions. Join fan-out is the single most common way a large query rewrite
goes wrong, and the obvious check is blind to it.

`sqldiff` uses `EXCEPT ALL`, which preserves multiplicity, in both directions.

Here it is catching a real fan-out where the row count, the grouping, and the
`DISTINCT` count all still look correct:

```
DIFFERENT

  before: 2 rows
  after:  2 rows

  Sample rows present only in BEFORE:
    region  order_count  revenue
    north   3            225.00
  Sample rows present only in AFTER:
    region  order_count  revenue
    north   3            450.00
```

## Install

No dependencies. Python 3.8+, and either `psql` on your PATH or `psycopg`
installed — it detects which is available and uses it.

```
git clone <your-fork> && cd sqldiff
python3 -m sqldiff.cli --help
```

## Use

```
python3 -m sqldiff.cli \
    --before path/to/original.sql \
    --after  path/to/refactored.sql \
    --dsn "postgresql://user@host/db"
```

Omit `--dsn` to use the standard `PGHOST` / `PGUSER` / `PGDATABASE` environment
variables.

Useful flags:

| Flag | Why |
|---|---|
| `--exclude COLUMN` | Ignore a volatile column, e.g. a `generated_at` timestamp. Repeatable. |
| `--round N` | Round float/numeric columns before comparing, so FP noise isn't a diff. |
| `--ignore-column-names` | Compare positionally when the refactor renamed columns. |
| `--sample N` | How many differing rows to show per side (default 5). |
| `--json` | Machine-readable output for CI. |

Exit codes: `0` identical, `1` different, `2` could not run.

## How it compares

1. Both queries are wrapped in temp views and introspected, so a mismatch in
   column count, order, or type is reported as its own failure rather than
   surfacing as a confusing type error.
2. Row counts on each side.
3. `EXCEPT ALL` in both directions, over a projection that casts `json` to
   `jsonb` and `xml` to `text` (neither has an equality operator usable by set
   operations) and optionally rounds inexact numerics.
4. Sample rows from each side of the difference.

All of the heavy lifting happens server-side, so this works on result sets far
too large to pull down. Client-side output is display-only.

## Handling sensitive queries

The repository contains no queries, no credentials, and no result data, and it
is designed so none can accidentally arrive:

- Queries are **paths passed at runtime**. The only SQL in the repo is the
  synthetic fixture set under `fixtures/`.
- Credentials come from the environment or `--dsn`. `.env*` is gitignored.
- Output goes to gitignored `out/`.

If you are cloning this somewhere that the code you compare must not leave,
make the clone push-proof so it is not a matter of remembering:

```
git remote set-url --push origin no_push
```

Git will then refuse any push from that clone.

## Limitations

- **PostgreSQL only.** Oracle (`MINUS`, no `MINUS ALL`) and SQL Server
  (`EXCEPT`, no `EXCEPT ALL`) need a `GROUP BY ... HAVING count(*)` emulation
  that isn't written yet.
- **Row order is not compared.** `EXCEPT ALL` is a set operation. If your query
  has a meaningful `ORDER BY`, this will not detect a reordering.
- **One `SELECT` per file.** The query is wrapped in `CREATE TEMP VIEW`, so DDL,
  multiple statements, and psql backslash commands won't work.
- Both queries run in full. On an expensive query, that is two expensive runs.

## Tests

```
python3 -m unittest discover -s tests
```

Unit tests need no database. To also run the integration tests, point
`SQLDIFF_TEST_DSN` at a throwaway Postgres:

```
docker run -d --name sqldiff-test -e POSTGRES_PASSWORD=test \
    -e POSTGRES_DB=sqldiff -p 55432:5432 postgres:16-alpine
SQLDIFF_TEST_DSN="postgresql://postgres:test@127.0.0.1:55432/sqldiff" \
    python3 -m unittest discover -s tests
```
