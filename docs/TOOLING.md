# Companion tooling

Notes on tools that pair with `sqldiff` for diagnosing code and, specifically,
for **cutting how many tokens an AI assistant has to read**.

## The principle

Assistant credits are consumed overwhelmingly by *input*. A 1000-line SQL query
is roughly 12k tokens; a verbose Java class with its imports and call sites is
easily 20-40k. An agent loop re-sends that context every single turn, so the
cost is not the answer you get back — it is the reading, paid for repeatedly.

So the rule is: **never paste a file, paste a digest.**

Every tool below exists to produce that digest deterministically. A parser that
extracts a CTE dependency graph costs nothing, cannot hallucinate, and is
*correct*. Spend the assistant's context on the judgement call that actually
needs judgement, and let ordinary tools establish the facts.

Practically, that means a three-step loop:

1. **Extract** — deterministic tools turn a big codebase into a small factual summary.
2. **Decide** — the assistant sees the summary plus your question. Two thousand tokens, not forty.
3. **Verify** — deterministic tools prove the change was correct. `sqldiff` is this step for SQL.

Steps 1 and 3 are free and exact. Only step 2 costs anything.

---

## SQL

### sqlglot — the single highest-value install

```
pip install sqlglot
```

A SQL parser and transpiler. For a long query it replaces most of what you
would otherwise ask an assistant to do by reading the whole thing:

```python
from sqlglot import parse_one, diff
from sqlglot.lineage import lineage
from sqlglot.optimizer import optimize

ast = parse_one(open("big.sql").read(), dialect="postgres")

# Which tables does this actually touch?
print({t.name for t in ast.find_all(exp.Table)})

# Where does one output column really come from, through every CTE?
print(lineage("revenue", sql, dialect="postgres"))

# Structural diff between two versions -- an edit list, not 2000 lines of text
print(diff(parse_one(before), parse_one(after)))

# Qualify every column and expand SELECT * -- makes a messy query legible
print(optimize(ast, schema=my_schema).sql(pretty=True))
```

`diff()` is the one to reach for when you want an assistant's opinion on a
rewrite: hand it the structural edit list instead of both full queries, and the
input drops by an order of magnitude.

`lineage()` answers "what feeds this column" exactly, which is the question you
would otherwise burn a large context window asking someone to trace by hand.

### sqlfluff — lint and auto-fix

```
pip install sqlfluff
sqlfluff fix --dialect postgres big.sql
```

Formatting and style are mechanical. Never spend credits on them.

### Query plans

```sql
EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT ...;
```

Feed the JSON to **PEV2** for a visual plan. Note that
`explain.dalibo.com` and pgMustard are *hosted* — the plan, including your
table and column names, leaves the machine. PEV2 can be run as a local static
page instead; prefer that for anything sensitive.

Server-side, `pg_stat_statements` finds your expensive queries without any
model in the loop, and `auto_explain` captures plans for slow queries as they
happen. `hypopg` lets you test whether an index would help *without creating
it*.

---

## Java

### OpenRewrite — deterministic mass refactoring

```xml
<plugin>
  <groupId>org.openrewrite.maven</groupId>
  <artifactId>rewrite-maven-plugin</artifactId>
</plugin>
```

```
mvn rewrite:run -Drewrite.activeRecipes=org.openrewrite.java.cleanup.Cleanup
```

AST-based recipes for framework migrations, API changes, dead code, and
formatting across an entire codebase. This is the correct tool for the
mechanical majority of "clean up this messy code" — it is exact, reviewable as
a diff, and costs nothing per file.

### PMD's CPD — find the duplication

```
pmd cpd --minimum-tokens 100 --dir src/ --format text
```

Copy-paste detection. On a messy codebase this usually explains most of the
mess in one report, and it tells you *where* refactoring pays off before you
ask anyone's opinion.

### Static analysis

- **Error Prone** — compile-time bug detection, catches real defects during the build
- **SpotBugs** + **find-sec-bugs** — bytecode analysis
- **ArchUnit** — assert architectural rules as ordinary JUnit tests
- **jdeps** — ships with the JDK; dependency graphs with zero install

### jqassistant — query your codebase

Scans a Java project into a Neo4j graph so you can ask structural questions in
Cypher: what calls this, what is unreachable, which packages are cyclic. For
understanding an unfamiliar messy codebase, one Cypher query replaces an awful
lot of reading.

---

## Cross-cutting

### ripgrep — targeted extraction

```
rg -n -C3 'processPayment' src/
```

The most-used token-reduction tool there is. Extract the relevant hunks with
three lines of context rather than attaching whole files. Already installed here.

### ast-grep — structural search and replace

```
npm install -g @ast-grep/cli     # or: cargo install ast-grep
sg run -p 'if ($A != null) { $$$B }' -l java
```

Matches code by *shape* rather than regex, and rewrites it safely. Handles the
"same pattern, 200 occurrences" job that would otherwise be an expensive and
error-prone assistant loop. (Not currently installed — note that plain `sg` on
Debian/Ubuntu is the unrelated setgid utility.)

### difftastic — diffs that show meaning

```
cargo install difftastic
GIT_EXTERNAL_DIFF=difft git diff
```

Structural diff. Ignores reformatting and shows what actually changed, which
makes diffs both easier to review and far smaller to paste.

### universal-ctags — a symbol index

```
apt install universal-ctags && ctags -R .
```

Jump straight to a definition rather than reading a file to find it.

### semgrep — codified patterns with autofix

```
pip install semgrep
semgrep --config ./rules/ --autofix
```

Once you have identified a bug pattern, write it as a rule and it is found and
fixed everywhere, forever, for free. Keep rules local — `semgrep ci` with an app
token reports findings to their cloud.

---

## Locked-down machines

If PyPI, npm, or crates.io are blocked, the order of value is roughly:

| Tool | Install route | Works offline once installed |
|---|---|---|
| `jdeps`, `javap` | ships with the JDK | yes |
| ripgrep, universal-ctags | OS package manager | yes |
| OpenRewrite, PMD, SpotBugs | Maven/Gradle dependency | yes, if your build already resolves artifacts |
| sqlglot, sqlfluff, semgrep | pip | yes |
| ast-grep, difftastic | npm / cargo | yes |

Anything resolved through an existing Maven or Gradle build is usually the
easiest thing to get approved, since it is already a dependency-fetch path the
environment permits.

## What leaves the machine

Almost nothing here talks to the network after install. The exceptions worth
knowing:

- hosted EXPLAIN visualisers (`explain.dalibo.com`, pgMustard) — the plan is uploaded
- `semgrep ci` with an app token — findings are uploaded; plain `semgrep --config` is local
- SonarCloud — hosted; SonarQube Community runs locally

Everything else — sqlglot, OpenRewrite, PMD, ripgrep, ast-grep, difftastic,
ctags, jqassistant, `sqldiff` itself — is entirely local.
